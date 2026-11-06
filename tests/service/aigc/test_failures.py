"""外部副作用未知、停止门闩、原任务收尾与崩溃恢复。"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeLLM, request, wait_for
from test_runs import blocking, command

from ms_agent.agent.api import AgentService
from ms_agent.agent.run_service import RunError, RunService
from ms_agent.session.run_store import FileRunStore, ProtocolVersionUnsupported
from ms_agent.tools.api_adapter import APIToolAdapter

pytestmark = pytest.mark.asyncio


async def test_unknown_delivery_never_resubmitted(runtime, monkeypatch):
    runs, _ = runtime
    calls = []

    async def transport(self, method, *args, **kwargs):
        calls.append(method)
        raise asyncio.TimeoutError

    monkeypatch.setattr(APIToolAdapter, '_request', transport)
    body = request(custom=True)
    await runs.start('u1', body)
    await runs.command('u1', body['run_uuid'], command('approve', await blocking(runs, body['run_uuid'])))
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'interrupted' and not view['can_release_activity']
    assert view['settlement']['status'] == 'unknown' and calls == ['POST']
    assert not (await runs.start('u1', body))[1]
    retry = request(custom=True)
    retry['source_run_uuid'] = body['run_uuid']
    retry['input_snapshot'] = body['input_snapshot']
    with pytest.raises(RunError, match='SOURCE_RUN_NOT_RETRYABLE'):
        await runs.start('u1', retry)
    _, events = await runs.store.read('u1', 'runs', body['run_uuid'])
    assert not any(e['event_type'] == 'run.completed' for e in events)


async def test_stop_follows_known_task_and_preserves_output(runtime, monkeypatch):
    runs, llm = runtime
    polling = asyncio.Event()
    calls = []

    async def transport(self, method, *args, **kwargs):
        calls.append(method)
        if method == 'POST':
            return {'task_id': 'original', 'task_status': 'PENDING'}
        if calls.count('GET') == 1:
            polling.set()
            await asyncio.sleep(10)
        return {'task_status': 'SUCCEED', 'output_images': ['https://example.test/late.png']}

    monkeypatch.setattr(APIToolAdapter, '_request', transport)
    body = request(custom=True)
    await runs.start('u1', body)
    await runs.command('u1', body['run_uuid'], command('approve', await blocking(runs, body['run_uuid'])))
    await polling.wait()
    receipt = await runs.command('u1', body['run_uuid'], command('stop'))
    assert receipt['command_status'] == 'applied'
    async with asyncio.timeout(3):
        while True:
            view = await runs.get('u1', body['run_uuid'])
            if view['can_release_activity']:
                break
            await asyncio.sleep(0.002)
    assert view['status'] == 'cancelled' and view['settlement']['status'] == 'resolved'
    assert calls.count('POST') == 1 and calls.count('GET') == 2 and not llm.calls
    assert any(o['url'] == 'https://example.test/late.png' for o in view['public_snapshot']['outputs'])


async def test_settlement_budget_exhaustion_is_unknown(runtime, monkeypatch):
    runs, _ = runtime
    polling = asyncio.Event()

    async def transport(self, method, *args, **kwargs):
        if method == 'POST':
            return {'task_id': 'task', 'task_status': 'PENDING'}
        polling.set()
        await asyncio.sleep(0.01)
        return {'task_status': 'RUNNING'}

    monkeypatch.setattr(APIToolAdapter, '_request', transport)
    body = request(custom=True)
    await runs.start('u1', body)
    await runs.command('u1', body['run_uuid'], command('approve', await blocking(runs, body['run_uuid'])))
    await polling.wait()
    await runs.command('u1', body['run_uuid'], command('stop'))
    view = await wait_for(runs, body['run_uuid'], 'interrupted')
    assert not view['can_release_activity'] and view['settlement']['status'] == 'unknown'
    assert not runs._scheduler.active


async def test_deadline_in_queue_does_not_submit(runtime):
    runs, llm = runtime
    runs._scheduler.capacity = 1
    llm.delay = 10
    first = request()
    await runs.start('u1', first)
    await wait_for(runs, first['run_uuid'], 'running')
    second = request(session_id='s2', deadline=(datetime.now(timezone.utc) + timedelta(seconds=0.02)).isoformat())
    await runs.start('u1', second)
    view = await wait_for(runs, second['run_uuid'])
    assert view['status'] == 'failed' and view['error']['code'] == 'DEADLINE_EXCEEDED'
    assert view['can_release_activity'] and len(llm.calls) == 1


async def test_already_expired_run_persists_deadline_result(runtime):
    runs, llm = runtime
    body = request(deadline=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    await runs.start('u1', body)
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'failed' and not llm.calls
    assert not (await runs.start('u1', body))[1]


async def test_restart_only_queries_original_task(tmp_path, config, settings, monkeypatch):
    polling = asyncio.Event()
    complete = False
    posts = []

    async def transport(self, method, *args, **kwargs):
        if method == 'POST':
            posts.append(True)
            return {'task_id': 'original', 'task_status': 'PENDING'}
        polling.set()
        if not complete:
            await asyncio.sleep(10)
        return {'task_status': 'SUCCEED', 'output_images': ['https://example.test/recovered.png']}

    monkeypatch.setattr(APIToolAdapter, '_request', transport)

    def build():
        store = FileRunStore(tmp_path)
        return RunService(
            store,
            AgentService(config, locks=store.locks, llm_client=FakeLLM()),
            runtime_config=settings.runtime.model_dump(),
            event_config=settings.events.model_dump(),
        )

    runs = build()
    await runs.initialize()
    body = request(custom=True)
    await runs.start('u1', body)
    await runs.command('u1', body['run_uuid'], command('approve', await blocking(runs, body['run_uuid'])))
    await polling.wait()
    await runs.close()
    complete = True
    revived = build()
    await revived.initialize()
    try:
        async with asyncio.timeout(3):
            while not (await revived.get('u1', body['run_uuid']))['can_release_activity']:
                await asyncio.sleep(0.002)
        assert len(posts) == 1
        assert (await revived.start('u1', body))[1] is False
    finally:
        await revived.close()


@pytest.mark.parametrize('stored_version', [None, 2])
async def test_incompatible_journal_never_overwritten(tmp_path, stored_version):
    import json

    store = FileRunStore(tmp_path)
    directory = store.directory('u1', 'runs', 'old')
    directory.mkdir(parents=True)
    path = directory / 'journal.jsonl'
    payload = {'updates': {'status': 'queued'}, 'event': None}
    if stored_version is not None:
        payload['schema_version'] = stored_version
    content = json.dumps({'record_seq': 1, 'previous': '', 'payload': payload}) + '\n'
    path.write_text(content)
    with pytest.raises(ProtocolVersionUnsupported):
        await store.read('u1', 'runs', 'old')
    with pytest.raises(ProtocolVersionUnsupported):
        await store.commit('u1', 'runs', 'old', {'status': 'queued'})
    assert path.read_text() == content and store.healthy


async def test_stop_while_waiting_http_slot_is_known_unsent(runtime):
    runs, _ = runtime
    from ms_agent.utils.limiter import UpstreamLimiter

    limiter = runs.agent.limiters['submit'] = UpstreamLimiter(1)
    async with limiter:
        body = request(custom=True)
        await runs.start('u1', body)
        await runs.command('u1', body['run_uuid'], command('approve', await blocking(runs, body['run_uuid'])))
        async with asyncio.timeout(3):
            while not (await runs.store.read_state('u1', 'runs', body['run_uuid']))['external_tasks']:
                await asyncio.sleep(0.002)
        await runs.command('u1', body['run_uuid'], command('stop'))
        view = await wait_for(runs, body['run_uuid'])
        assert view['status'] == 'cancelled' and view['can_release_activity']
        assert view['settlement']['status'] == 'resolved'
        assert view['settlement']['tasks'][0]['task_id'] is None


async def test_remote_poll_does_not_hold_run_slot(runtime, monkeypatch):
    runs, llm = runtime
    runs._scheduler.capacity = 1
    polling = asyncio.Event()
    settlement_limiters = []

    async def transport(self, method, *args, **kwargs):
        if method == 'POST':
            return {'task_id': 'one', 'task_status': 'PENDING'}
        if not polling.is_set():
            polling.set()
            await asyncio.sleep(10)
        settlement_limiters.append(self.limiters['poll'])
        return {'task_status': 'SUCCEED', 'output_images': ['https://example.test/late.png']}

    monkeypatch.setattr(APIToolAdapter, '_request', transport)
    original = request(custom=True)
    await runs.start('u1', original)
    await runs.command('u1', original['run_uuid'], command('approve', await blocking(runs, original['run_uuid'])))
    await polling.wait()
    next_run = request(session_id='other')
    await runs.start('u1', next_run)
    assert (await wait_for(runs, next_run['run_uuid']))['status'] == 'succeeded' and len(llm.calls) == 1
    await runs.command('u1', original['run_uuid'], command('stop'))
    async with asyncio.timeout(3):
        while not (await runs.get('u1', original['run_uuid']))['can_release_activity']:
            await asyncio.sleep(0.002)
    assert settlement_limiters == [runs.agent.limiters['settlement']]
    assert runs.agent.limiters['poll'] is not runs.agent.limiters['settlement']


async def test_deadline_not_shadowed_by_user_command_id(runtime):
    runs, llm = runtime
    llm.delay = 10
    body = request(deadline=(datetime.now(timezone.utc) + timedelta(seconds=0.08)).isoformat())
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'], 'running')
    rejected = await runs.command(
        'u1',
        body['run_uuid'],
        {
            'command_id': 'deadline',
            'action': 'approve',
            'interaction_id': 'foreign',
            'action_id': 'foreign',
        },
    )
    assert rejected['command_status'] == 'rejected'
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'failed' and view['error']['code'] == 'DEADLINE_EXCEEDED'


@pytest.mark.parametrize('reason', ['USER_STOP', 'DEADLINE'])
async def test_restart_preserves_settlement_reason(tmp_path, config, settings, monkeypatch, reason):
    complete = False
    polling = asyncio.Event()
    posts = []

    async def transport(self, method, *args, **kwargs):
        if method == 'POST':
            posts.append(True)
            return {'task_id': 'saved', 'task_status': 'PENDING'}
        polling.set()
        if not complete:
            await asyncio.sleep(10)
        return {'task_status': 'SUCCEED', 'output_images': ['https://example.test/recovered.png']}

    monkeypatch.setattr(APIToolAdapter, '_request', transport)

    def build():
        store = FileRunStore(tmp_path)
        return RunService(
            store,
            AgentService(config, locks=store.locks, llm_client=FakeLLM()),
            runtime_config=settings.runtime.model_dump(),
            event_config=settings.events.model_dump(),
        )

    runs = build()
    await runs.initialize()
    body = request(custom=True)
    await runs.start('u1', body)
    await runs.command('u1', body['run_uuid'], command('approve', await blocking(runs, body['run_uuid'])))
    await polling.wait()
    await runs.command('u1', body['run_uuid'], command('stop', stop_reason=reason))
    await wait_for(runs, body['run_uuid'])
    await runs.close()
    complete = True
    revived = build()
    await revived.initialize()
    try:
        async with asyncio.timeout(3):
            while not (await revived.get('u1', body['run_uuid']))['can_release_activity']:
                await asyncio.sleep(0.002)
        view = await revived.get('u1', body['run_uuid'])
        assert len(posts) == 1 and view['status'] == ('cancelled' if reason == 'USER_STOP' else 'failed')
        assert view['error'] is None if reason == 'USER_STOP' else view['error']['code'] == 'DEADLINE_EXCEEDED'
    finally:
        await revived.close()
