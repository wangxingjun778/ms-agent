"""幂等、显式祖先链、会话调度、Custom 和精确控制。"""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from conftest import MODEL, FakeLLM, request, wait_for

from ms_agent.agent.api import AgentService
from ms_agent.agent.run_service import RunError, RunService
from ms_agent.session.run_store import FileRunStore

pytestmark = pytest.mark.asyncio


def command(action, interaction=None, **kwargs):
    body = {'command_id': str(uuid4()), 'action': action, **kwargs}
    if interaction:
        selected = next(a for a in interaction['actions'] if a['action'] == action)
        body.update(interaction_id=interaction['interaction_id'], action_id=selected['action_id'])
    return body


async def blocking(runs, identifier):
    async with asyncio.timeout(3):
        while True:
            view = await runs.get('u1', identifier)
            item = next((i for i in view['public_snapshot']['pending_interactions'] if i['blocking']), None)
            if item:
                return item
            await asyncio.sleep(0.002)


async def test_start_idempotent_conflict_and_replay(runtime):
    runs, llm = runtime
    body = request()
    results = await asyncio.gather(*(runs.start('u1', body) for _ in range(12)))
    assert sum(created for _, created in results) == 1
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'succeeded' and view['can_release_activity'] and len(llm.calls) == 1
    conflict = deepcopy(body)
    conflict['input_snapshot']['effective']['prompt'] = '不同输入'
    with pytest.raises(RunError, match='RUN_INPUT_CONFLICT'):
        await runs.start('u1', conflict)
    with pytest.raises(RunError, match='RUN_NOT_FOUND'):
        await runs.get('u2', body['run_uuid'])
    runs._event_config['batch_size'] = 2
    events = [e async for e in runs.events('u1', body['run_uuid'])]
    assert events[-1]['event_type'] == 'run.completed'
    assert all(e['schema_version'] == 1 for e in events)
    assert [e['agent_seq'] for e in events] == list(range(1, len(events) + 1))
    assert [e async for e in runs.events('u1', body['run_uuid'], 2)] == events[2:]
    assert view['snapshot_agent_seq'] == view['last_agent_seq'] == events[-1]['agent_seq']


async def test_cancel_before_start_and_command_conflict(runtime):
    runs, llm = runtime
    body = request()
    stop = command('stop')
    receipt = await runs.command('u1', body['run_uuid'], stop)
    assert receipt['command_status'] == 'applied'
    view, created = await runs.start('u1', body)
    assert view['status'] == 'cancelled' and not created and not llm.calls
    assert (await runs.command('u1', body['run_uuid'], stop))['command_status'] == 'applied'
    with pytest.raises(RunError, match='COMMAND_KEY_CONFLICT'):
        await runs.command('u1', body['run_uuid'], {**stop, 'stop_reason': 'DEADLINE'})


async def test_same_session_does_not_implicitly_load_history(runtime):
    runs, llm = runtime
    first = request(content='branch-a')
    await runs.start('u1', first)
    await wait_for(runs, first['run_uuid'])
    second = request(content='branch-b')
    second['input_snapshot']['context'] = [
        {
            'message_uuid': second['parent_message_uuid'],
            'role': 'assistant',
            'content': [{'type': 'text', 'text': 'selected-parent'}],
            'output_refs': [],
        }
    ]
    await runs.start('u1', second)
    await wait_for(runs, second['run_uuid'])
    assert llm.calls[-1][-2]['content'] == 'selected-parent'
    assert 'branch-a' not in str(llm.calls[-1])
    assert not (runs.store.root / 'users/u1/sessions').exists()


async def test_same_session_fifo_and_cancelled_predecessor(runtime):
    runs, llm = runtime
    llm.delay = 0.04
    a, b, c = [request(content=name) for name in ('a', 'b', 'c')]
    await runs.start('u1', a)
    await wait_for(runs, a['run_uuid'], 'running')
    await runs.start('u1', b)
    await runs.start('u1', c)
    await runs.command('u1', b['run_uuid'], command('stop'))
    await wait_for(runs, c['run_uuid'])
    assert [messages[-1]['content'] for messages in llm.calls] == ['a', 'c']


async def test_stop_running_and_first_reason_preserved(runtime):
    runs, llm = runtime
    llm.delay = 10
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'], 'running')
    await runs.command('u1', body['run_uuid'], command('stop', stop_reason='USER_STOP'))
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'cancelled' and view['can_release_activity']
    await runs.command('u1', body['run_uuid'], command('stop', stop_reason='DEADLINE'))
    state = await runs.store.read_state('u1', 'runs', body['run_uuid'])
    assert state['stop_reason'] == 'USER_STOP' and state['status'] == 'cancelled'


async def test_restart_does_not_repeat_run(tmp_path, config, settings):
    body = request()
    llm = FakeLLM(delay=10)
    store = FileRunStore(tmp_path)
    runs = RunService(
        store,
        AgentService(config, locks=store.locks, llm_client=llm),
        runtime_config=settings.runtime.model_dump(),
        event_config=settings.events.model_dump(),
    )
    await runs.initialize()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'], 'running')
    await runs.close()
    next_llm = FakeLLM()
    new_store = FileRunStore(tmp_path)
    second = RunService(
        new_store,
        AgentService(config, locks=new_store.locks, llm_client=next_llm),
        runtime_config=settings.runtime.model_dump(),
        event_config=settings.events.model_dump(),
    )
    await second.initialize()
    try:
        view, created = await second.start('u1', body)
        assert not created and view['status'] == 'failed' and not next_llm.calls
    finally:
        await second.close()


async def test_custom_requires_action_id_and_does_not_call_llm(runtime, monkeypatch):
    from ms_agent.tools.api_adapter import APIToolAdapter

    runs, llm = runtime
    calls = []

    async def transport(self, method, url, headers, json_body=None, timeout=None, context=None):
        if method == 'POST':
            calls.append(deepcopy(json_body))
            return {'task_id': 't1', 'task_status': 'PENDING'}
        return {'task_status': 'SUCCEED', 'output_images': ['https://example.test/result.png']}

    monkeypatch.setattr(APIToolAdapter, '_request', transport)
    body = request(custom=True)
    body['input_snapshot']['effective']['preferences']['seed'] = 42
    await runs.start('u1', body)
    interaction = await blocking(runs, body['run_uuid'])
    wrong = command('approve', interaction)
    wrong['action_id'] = 'foreign-action'
    assert (await runs.command('u1', body['run_uuid'], wrong))['command_status'] == 'rejected'
    approved = command('approve', interaction)
    await runs.command('u1', body['run_uuid'], approved)
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'succeeded' and not llm.calls
    assert calls == [{'model': MODEL, 'prompt': '你好', 'n': 1, 'seed': 42}]
    output = next(o for o in view['public_snapshot']['outputs'] if o['type'] == 'image')
    assert output['generation']['submitted']['seed'] == 42 and output['actual_generation'] is None
    assert (await runs.command('u1', body['run_uuid'], approved))['command_status'] == 'applied'
    assert not view['public_snapshot']['pending_interactions']


async def test_skip_tool_does_not_submit(runtime):
    runs, llm = runtime
    body = request(custom=True)
    await runs.start('u1', body)
    interaction = await blocking(runs, body['run_uuid'])
    await runs.command('u1', body['run_uuid'], command('skip', interaction))
    view = await wait_for(runs, body['run_uuid'])
    assert not llm.calls and not (await runs.store.read_state('u1', 'runs', body['run_uuid']))['external_tasks']
    assert view['can_release_activity']


async def test_nonblocking_intervention_is_exact_and_applied(runtime):
    runs, llm = runtime
    llm.delay = 0.05
    body = request()
    await runs.start('u1', body)
    async with asyncio.timeout(3):
        while True:
            view = await runs.get('u1', body['run_uuid'])
            active = next((i for i in view['public_snapshot']['pending_interactions'] if not i['blocking']), None)
            if active:
                break
            await asyncio.sleep(0.001)
    intervention = command('intervene', active, input={'text': '新的约束'})
    receipt = await runs.command('u1', body['run_uuid'], intervention)
    assert receipt['command_status'] == 'accepted'
    view = await wait_for(runs, body['run_uuid'])
    assert len(llm.calls) == 2 and any(m['content'] == '新的约束' for m in llm.calls[-1])
    assert (
        next(c for c in view['public_snapshot']['commands'] if c['command_id'] == intervention['command_id'])[
            'command_status'
        ]
        == 'applied'
    )


async def test_ask_input_continues_original_run(runtime):
    runs, llm = runtime
    llm.messages = [
        {
            'role': 'assistant',
            'tool_calls': [
                {'id': 'ask', 'function': {'name': 'ask_input', 'arguments': '{"question":"希望什么颜色？"}'}}
            ],
        },
        {'role': 'assistant', 'content': '已经明确需求。'},
    ]
    body = request()
    await runs.start('u1', body)
    interaction = await blocking(runs, body['run_uuid'])
    invalid = command('submit_input', interaction, input={'values': {'color': 'red'}})
    assert (await runs.command('u1', body['run_uuid'], invalid))['reason_code'] == 'INVALID_INTERACTION_INPUT'
    await runs.command('u1', body['run_uuid'], command('submit_input', interaction, input={'text': '红色'}))
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'succeeded' and len(llm.calls) == 2
    assert any(m.get('content') == '红色' for m in llm.calls[-1])
    assert not (await runs.store.read_state('u1', 'runs', body['run_uuid']))['external_tasks']


async def test_competing_approvals_and_public_receipts_same_watermark(runtime, monkeypatch):
    from ms_agent.agent.public_state import reduce_public
    from ms_agent.tools.api_adapter import APIToolAdapter

    runs, _ = runtime
    submitted = []

    async def transport(self, method, *args, **kwargs):
        if method == 'POST':
            submitted.append(True)
            return {'task_id': 'once', 'task_status': 'PENDING'}
        return {'task_status': 'SUCCEED', 'output_images': ['https://example.test/once.png']}

    monkeypatch.setattr(APIToolAdapter, '_request', transport)
    body = request(custom=True)
    await runs.start('u1', body)
    interaction = await blocking(runs, body['run_uuid'])
    outcomes = await asyncio.gather(
        *(runs.command('u1', body['run_uuid'], command('approve', interaction)) for _ in range(12))
    )
    assert sum(c['command_status'] in ('accepted', 'applied') for c in outcomes) == 1
    view = await wait_for(runs, body['run_uuid'])
    assert len(submitted) == 1
    for receipt in view['public_snapshot']['commands']:
        assert receipt['state_revision'] == view['state_revision']
        assert receipt['run_status'] == view['status']
        assert receipt['execution_closed'] == view['execution_closed']
    replay = None
    for event in [e async for e in runs.events('u1', body['run_uuid'])]:
        replay = reduce_public(replay, None, event)
    assert replay == view['public_snapshot']
