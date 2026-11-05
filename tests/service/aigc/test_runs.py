"""Run 幂等、控制、会话隔离和重启对账。"""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from conftest import FakeLLM, request, wait_for

from ms_agent.agent.api import AgentService
from ms_agent.agent.run_service import RunError, RunService
from ms_agent.session.run_store import FileRunStore
from ms_agent.tools.api_adapter import APIConfig, APIToolSpec, AuthConfig, OutputMapping
from ms_agent.tools.schema import ToolResult

pytestmark = pytest.mark.asyncio


def command(kind, **kwargs):
    return {'command_id': str(uuid4()), 'type': kind, 'interaction_id': None, 'payload': {}, **kwargs}


async def test_start_idempotent_conflict_and_replay(runtime):
    runs, llm = runtime
    body = request()
    outcomes = await asyncio.gather(*(runs.start('u1', body) for _ in range(12)))
    assert sum(created for _, created in outcomes) == 1
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'succeeded'
    assert len(llm.calls) == 1
    conflict = deepcopy(body)
    conflict['messages'][0]['content'] = '不同输入'
    with pytest.raises(RunError, match='RUN_INPUT_CONFLICT'):
        await runs.start('u1', conflict)
    with pytest.raises(RunError, match='RUN_NOT_FOUND'):
        await runs.get('other-user', body['run_uuid'])
    events = [event async for event in runs.events('u1', body['run_uuid'])]
    assert [event['agent_seq'] for event in events] == list(range(1, len(events) + 1))
    assert events[-1]['event_type'] == 'run_terminal'
    suffix = [event async for event in runs.events('u1', body['run_uuid'], 2)]
    assert suffix == events[2:]
    snapshot = await runs.store.read_snapshot('u1', 'runs', body['run_uuid'], 'input_snapshot.json')
    assert 'api_key' not in snapshot['config']['llm']


async def test_cancel_before_start_and_command_conflict(runtime):
    runs, llm = runtime
    body = request()
    stop = command('stop')
    receipt = await runs.command('u1', body['run_uuid'], stop)
    assert receipt['status'] == 'applied'
    assert receipt == await runs.command('u1', body['run_uuid'], stop)
    with pytest.raises(RunError, match='COMMAND_INPUT_CONFLICT'):
        await runs.command('u1', body['run_uuid'], {**stop, 'type': 'intervene', 'payload': {'content': 'x'}})
    view, _ = await runs.start('u1', body)
    assert view['status'] == 'cancelled'
    assert not llm.calls


async def test_rejected_unknown_command_does_not_cancel_late_start(runtime):
    runs, _ = runtime
    body = request()
    receipt = await runs.command('u1', body['run_uuid'], command('intervene', payload={'content': 'x'}))
    assert receipt['status'] == 'rejected'
    await runs.start('u1', body)
    assert (await wait_for(runs, body['run_uuid']))['status'] == 'succeeded'


async def test_same_session_serial_different_session_isolated(runtime):
    runs, llm = runtime
    llm.delay = 0.02
    bodies = [request(content=f'input-{index}') for index in range(3)]
    bodies.append(request(session_id='s2', content='独立会话'))
    for body in bodies:
        await runs.start('u1', body)
    await asyncio.gather(*(wait_for(runs, body['run_uuid']) for body in bodies))
    second = next(messages for messages in llm.calls if messages[-1]['content'] == 'input-1')
    assert any(message['content'] == 'input-0' for message in second)
    isolated = next(messages for messages in llm.calls if messages[-1]['content'] == '独立会话')
    assert not any('input-' in message['content'] for message in isolated)


async def test_stop_running_has_final_receipt(runtime):
    runs, llm = runtime
    llm.delay = 10
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'], 'running')
    stop = command('stop')
    receipt = await runs.command('u1', body['run_uuid'], stop)
    assert receipt['status'] == 'accepted'
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'cancelled'
    final_receipt = await runs.command('u1', body['run_uuid'], stop)
    assert final_receipt['status'] == 'applied'
    assert final_receipt['applied_agent_seq'] == view['last_agent_seq']
    assert (await runs.start('u1', body))[0]['status'] == 'cancelled'


async def test_restart_queries_without_rerunning(tmp_path, config):
    llm = FakeLLM(delay=10)
    store = FileRunStore(tmp_path)
    runs = RunService(store, AgentService(config, locks=store.locks, llm_client=llm))
    await runs.initialize()
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'], 'running')
    await runs.close()
    next_llm = FakeLLM()
    next_store = FileRunStore(tmp_path)
    second = RunService(next_store, AgentService(config, locks=next_store.locks, llm_client=next_llm))
    await second.initialize()
    try:
        view, created = await second.start('u1', body)
        assert view['status'] == 'interrupted' and not created and not next_llm.calls
        events = [event async for event in second.events('u1', body['run_uuid'])]
        assert events[-1]['data']['status'] == 'interrupted'
    finally:
        await second.close()


async def test_authorize_input_skip_and_expired_interaction(runtime, monkeypatch):
    runs, llm = runtime
    llm.messages = [
        {
            'role': 'assistant',
            'content': '',
            'tool_calls': [
                {'id': 'call-1', 'type': 'function', 'function': {'name': 'image_generation', 'arguments': '{}'}}
            ],
        },
        {'role': 'assistant', 'content': '图片完成'},
    ]
    registry = runs.agent._tool_registry
    spec = APIToolSpec(
        'image_generation',
        '图片',
        APIConfig(),
        {
            'type': 'object',
            'required': ['prompt'],
            'properties': {'prompt': {'type': 'string'}},
            'additionalProperties': False,
        },
        OutputMapping(),
        requires_approval=True,
    )
    registry.register(spec, 'http://unused.invalid', AuthConfig())
    calls = []

    async def execute(name, args, context):
        calls.append(args)
        return ToolResult.from_success(
            name, '["https://example.test/image.png"]', {'output_images': ['https://example.test/image.png']}
        )

    monkeypatch.setattr(registry, 'execute', execute)
    body = request()
    await runs.start('u1', body)
    waiting = await wait_for(runs, body['run_uuid'], 'waiting_input')
    interaction = waiting['pending_interaction']
    assert interaction['kind'] == 'input'
    invalid = command('input', interaction_id=interaction['interaction_id'], payload={'bad': 1})
    assert (await runs.command('u1', body['run_uuid'], invalid))['status'] == 'rejected'
    supplied = command('input', interaction_id=interaction['interaction_id'], payload={'prompt': '画一只猫'})
    await runs.command('u1', body['run_uuid'], supplied)
    async with asyncio.timeout(5):
        while True:
            waiting = await runs.get('u1', body['run_uuid'])
            pending = waiting.get('pending_interaction')
            if pending and pending['kind'] == 'approval':
                break
            await asyncio.sleep(0.01)
    assert not calls
    approval = command('approve', interaction_id=pending['interaction_id'])
    await runs.command('u1', body['run_uuid'], approval)
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'succeeded' and calls == [{'prompt': '画一只猫'}]
    assert len([a for a in view['artifacts'] if a['type'] == 'image']) == 1
    assert (await runs.command('u1', body['run_uuid'], approval))['status'] == 'applied'
    assert (await runs.command('u1', body['run_uuid'], command('approve', interaction_id=pending['interaction_id'])))[
        'status'
    ] == 'rejected'


async def test_skip_does_not_call_tool(runtime, monkeypatch):
    runs, llm = runtime
    llm.messages = [
        {
            'role': 'assistant',
            'content': '',
            'tool_calls': [{'id': 'call-1', 'function': {'name': 'image_generation', 'arguments': '{"prompt":"cat"}'}}],
        },
        {'role': 'assistant', 'content': '已跳过'},
    ]
    registry = runs.agent._tool_registry
    registry.register(
        APIToolSpec(
            'image_generation', '图片', APIConfig(), {'type': 'object'}, OutputMapping(), requires_approval=True
        ),
        'http://unused.invalid',
        AuthConfig(),
    )

    async def execute(*args):
        raise AssertionError('不应调用工具')

    monkeypatch.setattr(registry, 'execute', execute)
    body = request()
    await runs.start('u1', body)
    waiting = await wait_for(runs, body['run_uuid'], 'waiting_input')
    await runs.command(
        'u1', body['run_uuid'], command('skip', interaction_id=waiting['pending_interaction']['interaction_id'])
    )
    assert (await wait_for(runs, body['run_uuid']))['status'] == 'succeeded'
