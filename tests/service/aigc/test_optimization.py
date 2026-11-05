"""单 Pod 优化的配置、缓存、公平性、取消和上游副作用边界。"""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path

import aiohttp
import httpx
import pytest
import yaml
from aiohttp import web
from conftest import FakeLLM, request, wait_for
from pydantic import ValidationError

from ms_agent.agent.api import AgentService
from ms_agent.agent.control import InteractionTimeout, RunControl
from ms_agent.agent.run_service import RunError, RunService
from ms_agent.agent.runner import AgentRunner
from ms_agent.agent.scheduler import FairScheduler
from ms_agent.llm.http_client import HTTPLLMClient
from ms_agent.session.locking import FileLease, PathLocks, PersistenceError
from ms_agent.session.run_store import FileRunStore, StorageCorrupted
from ms_agent.session.store import SessionStore
from ms_agent.tools.api_adapter import APIConfig, APIToolAdapter, APIToolSpec, AuthConfig, OutputMapping
from ms_agent.tools.schema import ToolResult
from ms_agent.utils.limiter import UpstreamLimiter
from service.aigc.app import create_app
from service.aigc.deploy import render_eas
from service.aigc.settings import Settings

ROOT = Path(__file__).resolve().parents[3]
POLICY = {'attempts': 3, 'initial': 0.001, 'maximum': 0.005, 'budget': 1}


@pytest.fixture
def configured(monkeypatch):
    for key in (
        'AIGC_CONFIG',
        'API_TOOLS_CONFIG',
        'AIGC_QUEUE_LIMIT',
        'AIGC_MAX_INFLIGHT',
        'AIGC_EAS_ENABLED',
        'AIGC_EAS_GATEWAY',
        'AIGC_SERVICE_TOKEN_FILE',
        'LLM_API_KEY_FILE',
        'API_TOKEN_FILE',
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('AIGC_SERVICE_TOKEN', 'secret-service')
    monkeypatch.setenv('LLM_API_KEY', 'secret-llm')
    monkeypatch.setenv('API_TOKEN', 'secret-image')
    return Settings.load()


@pytest.mark.parametrize(
    'legacy',
    [
        {'service': {'max_runs': 4}},
        {'agent_config': {}},
        {'service_token': 'old'},
        {'data_dir': '/tmp/old'},
        {'host': '127.0.0.1'},
        {'port': 8001},
        {'max_runs': 4},
        {'queue_limit': 100},
        {'shutdown_timeout': 30},
        {'max_body_bytes': 1024},
        {'recipe_timeout': 60},
        {'recipe_concurrency': 2},
        {'runtime': {'queue_limit': 10}},
    ],
)
def test_removed_settings_entries_are_rejected(legacy):
    with pytest.raises(ValidationError):
        Settings.model_validate(legacy)


@pytest.mark.parametrize('name', ['API_TOOLS_CONFIG', 'AIGC_QUEUE_LIMIT'])
def test_obsolete_environment_is_rejected(configured, monkeypatch, name):
    monkeypatch.setenv(name, 'old-value')
    with pytest.raises(ValueError, match=name):
        Settings.load()


def test_service_configuration_no_second_yaml(configured, monkeypatch):
    import ms_agent.config.lightweight as lightweight

    monkeypatch.setattr(
        lightweight, 'load_lightweight_config', lambda *_args, **_kwargs: pytest.fail('禁止合并旧 SDK 配置')
    )
    assert Settings.load().to_agent_config()['session']['base_dir'] == configured.storage.data_dir
    assert not hasattr(configured, 'data_dir')
    assert not hasattr(configured, 'agent_config')
    assert not hasattr(AgentRunner, '_call_llm_native')
    assert not hasattr(AgentRunner, '_call_llm_http')
    assert not (ROOT / 'service/aigc/configs/api_tools.yaml').exists()


def test_storage_has_only_one_implementation(tmp_path):
    with pytest.raises(TypeError):
        FileRunStore(tmp_path, optimized=False)
    with pytest.raises(TypeError):
        SessionStore(str(tmp_path), 'u', optimized=False)


def test_unified_configuration_and_secrets(configured, monkeypatch):
    assert configured.runtime.max_active_runs == 64
    assert configured.runtime.max_inflight_runs == 500
    assert configured.to_agent_config()['tools']['api']['auth']['token'] == 'secret-image'
    exported = json.dumps(configured.effective_config())
    assert all(value not in exported for value in ('secret-service', 'secret-llm', 'secret-image'))
    assert configured.config_digest == configured.config_digest
    monkeypatch.setenv('AIGC_MAX_RUNS', '16')
    monkeypatch.setenv('AIGC_MAX_INFLIGHT', '36')
    assert Settings.load().runtime.max_inflight_runs == 36
    monkeypatch.setenv('API_TOOLS_CONFIG', 'ignored.yaml')
    with pytest.raises(ValueError, match='API_TOOLS_CONFIG'):
        Settings.load()


@pytest.mark.parametrize(
    'override',
    [
        {'eas': {'instance': 2}},
        {'storage': {'misspelled_option': True}},
        {'runtime': {'max_active_runs': 100, 'max_inflight_runs': 2}},
        {'upstreams': {'llm': {'concurrency': 33, 'connections': 32}}},
        {'eas': {'enabled': True, 'gateway': 'mse'}, 'server': {'max_body_bytes': 2097152}},
        {'eas': {'enabled': True, 'gateway': 'mse'}, 'server': {'max_body_bytes': 1048576, 'keep_alive_timeout': 5}},
        {
            'eas': {'enabled': True, 'gateway': 'mse'},
            'server': {'max_body_bytes': 1048576},
            'events': {'max_stream_seconds': 600},
        },
        {'secrets': {'api': '../../token'}},
    ],
)
def test_configuration_rejects_inconsistent_boundaries(override):
    with pytest.raises(ValidationError):
        Settings.model_validate(override)


def test_tool_settings_reject_unknown_parameter(configured):
    data = configured.model_dump()
    data['tools']['api']['tools'][0]['api']['poll_interva'] = 10
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_eas_generator_has_isolated_mounts_and_correct_rpc(configured):
    configured.eas.image = 'registry-vpc.example/ns/aigc:test'
    configured.eas.nas_server = 'fs.example.nas.aliyuncs.com'
    document = render_eas(configured, 2)
    assert document['name'] == 'aigc_2'
    metadata = document['metadata']
    assert metadata['instance'] == 1
    assert metadata['rpc'] == {'enable_sigterm': True, 'keepalive': 600000}
    assert 'worker_threads' not in json.dumps(document)
    assert metadata['rolling_strategy'] == {'max_surge': 0, 'max_unavailable': 1}
    assert document['storage'][0]['nfs']['path'] == '/aigc/shard-2'
    assert all(s['nfs']['readOnly'] for s in document['storage'][1:])
    values = {e['name']: e['value'] for e in document['containers'][0]['env']}
    assert values['AIGC_AUTH_HEADER'] == 'X-AIGC-Service-Token'
    assert values['AIGC_PUBLIC_PATH_PREFIX'] == '/api/predict/aigc_2'
    assert 'secret-service' not in json.dumps(document)
    assert 'AIGC_CONFIG' in values
    with pytest.raises(ValueError):
        render_eas(configured, -1)


@pytest.mark.asyncio
async def test_hot_store_reads_and_commits_do_not_replay(tmp_path, monkeypatch):
    store = FileRunStore(tmp_path, projection='never')
    await store.recover()
    async with store.lock('u', 'runs', 'r'):
        await store.commit('u', 'runs', 'r', {'status': 'running'}, 'run_started')
    calls = []
    original = os.fsync
    monkeypatch.setattr(os, 'fsync', lambda fd: (calls.append(fd), original(fd))[-1])
    monkeypatch.setattr(store, '_read_sync', lambda *_: pytest.fail('热路径不应回放 journal'))
    async with store.lock('u', 'runs', 'r'):
        await store.commit('u', 'runs', 'r', {}, 'public_summary', {'content': 'hello'})
    assert len(calls) == 2
    view, events = await store.read('u', 'runs', 'r')
    assert view['last_agent_seq'] == 2 and len(events) == 2
    view['status'] = 'tampered'
    events[0] = {}
    assert (await store.read('u', 'runs', 'r'))[0]['status'] == 'running'
    with pytest.raises(ValueError):
        await store.commit('u', 'runs', 'r', {'value': float('nan')})
    assert store.healthy


@pytest.mark.asyncio
async def test_active_index_recovery_no_full_scan(tmp_path, monkeypatch):
    store = FileRunStore(tmp_path, projection='terminal')
    await store.recover()
    await store.commit('u', 'runs', 'active', {'status': 'running'})
    await store.commit('u', 'runs', 'done', {'status': 'succeeded'})
    revived = FileRunStore(tmp_path)

    async def forbidden():
        pytest.fail('已有索引不应扫描全部历史')

    monkeypatch.setattr(revived, 'list_entries', forbidden)
    await revived.recover()
    assert (await revived.read('u', 'runs', 'active'))[0]['status'] == 'interrupted'
    assert (await revived.read('u', 'runs', 'done'))[0]['status'] == 'succeeded'
    assert not revived._index_entries()


@pytest.mark.asyncio
async def test_corrupt_journal_isolated_and_never_reused(tmp_path):
    old = FileRunStore(tmp_path)
    await old.commit('u', 'runs', 'bad', {'status': 'running'})
    bad = old.directory('u', 'runs', 'bad') / 'journal.jsonl'
    bad.write_text('{}\n', encoding='utf-8')
    store = FileRunStore(tmp_path)
    await store.recover()
    with pytest.raises(StorageCorrupted):
        await store.read('u', 'runs', 'bad')
    assert store.healthy
    await store.commit('u', 'runs', 'good', {'status': 'succeeded'})
    with pytest.raises(StorageCorrupted):
        await store.commit('u', 'runs', 'bad', {'status': 'queued'})


@pytest.mark.asyncio
async def test_epoch_change_fails_closed(tmp_path):
    store = FileRunStore(tmp_path)
    store.lease.acquire()
    try:
        await store.initialize_epoch()
        await store.check_epoch()
        store._epoch_path.write_text('foreign-coordinator', encoding='utf-8')
        with pytest.raises(PersistenceError):
            await store.check_epoch()
        assert not store.healthy
    finally:
        store.lease.release()


@pytest.mark.asyncio
async def test_scheduler_fairness_and_cancellation():
    scheduler = FairScheduler(1, 1)
    await scheduler.acquire('a')
    a = asyncio.create_task(scheduler.acquire('a'))
    b = asyncio.create_task(scheduler.acquire('b'))
    await asyncio.sleep(0)
    scheduler.release('a')
    await asyncio.wait_for(b, 1)
    assert not a.done()
    a.cancel()
    await asyncio.gather(a, return_exceptions=True)
    scheduler.release('b')
    assert not scheduler.active and not scheduler._queues
    # 已授予但 await 尚未返回时取消也必须归还。
    await scheduler.acquire('a')
    c = asyncio.create_task(scheduler.acquire('c'))
    await asyncio.sleep(0)
    scheduler.release('a')
    c.cancel()
    await asyncio.gather(c, return_exceptions=True)
    assert not scheduler.active


@pytest.mark.asyncio
async def test_interaction_releases_slot_and_timeout():
    scheduler = FairScheduler(1, 1)

    async def record(*_):
        return None

    async with scheduler.permit('u') as permit:
        control = RunControl(record, permit=permit, timeout=0.01)
        with pytest.raises(InteractionTimeout):
            await control._wait('approval', {})
        assert not permit.held and not scheduler.active


@pytest.mark.asyncio
async def test_waiting_run_does_not_block_other_user(tmp_path, config, settings):
    call = {'id': 'x', 'function': {'name': 'test_tool', 'arguments': '{}'}}
    llm = FakeLLM([{'role': 'assistant', 'tool_calls': [call]}, {'role': 'assistant', 'content': 'ok'}])
    runs = RunService(
        FileRunStore(tmp_path),
        AgentService(config, llm_client=llm),
        runtime_config={
            **settings.runtime.model_dump(),
            'max_active_runs': 1,
            'per_user_active': 1,
            'max_inflight_runs': 3,
            'per_user_inflight': 2,
            'interaction_timeout': 1,
        },
        event_config=settings.events.model_dump(),
    )
    await runs.initialize()
    try:
        registry = runs.agent._tool_registry
        registry.register(
            APIToolSpec('test_tool', 'test', APIConfig(), {'type': 'object'}, OutputMapping(), requires_approval=True),
            'http://unused.invalid',
            AuthConfig(type='none'),
        )

        async def execute(*_):
            return ToolResult.from_success('test_tool', 'ok')

        registry._tools['test_tool']._sync_call = execute
        body_a = request()
        await runs.start('u1', body_a)
        await wait_for(runs, body_a['run_uuid'], 'waiting_input')
        body_b = request(session_id='other')
        await runs.start('u2', body_b)
        async with asyncio.timeout(2):
            while (await runs.get('u2', body_b['run_uuid']))['status'] != 'succeeded':
                await asyncio.sleep(0.001)
        interaction = (await runs.get('u1', body_a['run_uuid']))['pending_interaction']['interaction_id']
        await runs.command(
            'u1',
            body_a['run_uuid'],
            {'command_id': 'ok', 'type': 'approve', 'interaction_id': interaction, 'payload': {}},
        )
        await wait_for(runs, body_a['run_uuid'], 'succeeded')
        assert len(llm.calls) == 3
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_same_user_multiple_sessions_and_queue_reservation(tmp_path, config, settings):
    llm = FakeLLM(delay=0.05)
    runs = RunService(
        FileRunStore(tmp_path),
        AgentService(config, llm_client=llm),
        runtime_config={
            **settings.runtime.model_dump(),
            'max_active_runs': 1,
            'per_user_active': 1,
            'max_inflight_runs': 5,
            'per_user_inflight': 2,
        },
        event_config=settings.events.model_dump(),
    )
    await runs.initialize()
    try:
        bodies = [request(session_id=f's{i}') for i in range(5)]
        results = await asyncio.gather(*(runs.start('u1', body) for body in bodies), return_exceptions=True)
        rejected = [r for r in results if isinstance(r, RunError)]
        assert len(rejected) == 3 and all(r.details['scope'] == 'user' for r in rejected)
        assert len(runs._tasks) <= 2 and not runs._reservations
    finally:
        await runs.close()


@pytest.mark.asyncio
async def test_http_etag_auth_prefix_and_stats(settings):
    settings.auth.header, settings.auth.scheme = 'X-AIGC-Service-Token', 'raw'
    settings.server.public_path_prefix = '/api/predict/aigc_0'
    app = create_app(settings, llm_client=FakeLLM())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url='http://test',
            headers={'X-AIGC-Service-Token': 'test-service-secret', 'X-User-Id': 'u1'},
        ) as client:
            body = request()
            started = await client.post('/v1/aigc/runs', json=body)
            assert started.json()['links']['self'].startswith('/api/predict/aigc_0/')
            await wait_for(app.state.runs, body['run_uuid'])
            url = f'/v1/aigc/runs/{body["run_uuid"]}'
            response = await client.get(url)
            cached = await client.get(url, headers={'If-None-Match': response.headers['ETag']})
            assert cached.status_code == 304 and not cached.content
            wrong_user = await client.get(url, headers={'X-User-Id': 'u2', 'If-None-Match': response.headers['ETag']})
            assert wrong_user.status_code == 404
            stats = await client.get('/internal/stats')
            assert stats.status_code == 200 and 'config_digest' in stats.json()
            app.state.runs.begin_drain()
            assert (await client.get('/health/ready')).status_code == 503


@pytest.mark.asyncio
async def test_sse_no_lost_wakeup_or_idle_journal_replay(runtime, monkeypatch):
    runs, llm = runtime
    llm.delay = 0.08
    runs._event_config.update(heartbeat_seconds=0.02, max_stream_seconds=0.05)
    body = request()
    await runs.start('u1', body)
    stream = runs.events('u1', body['run_uuid'])
    received = [e async for e in stream]
    assert any(e is None for e in received)
    assert runs.subscribers == 0
    await wait_for(runs, body['run_uuid'])
    seq = max(e['agent_seq'] for e in received if e)
    replay = [e async for e in runs.events('u1', body['run_uuid'], seq)]
    assert replay[-1]['event_type'] == 'run_terminal'


@pytest.mark.asyncio
async def test_optimized_session_batch_seq_and_single_flush(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path), 'u')
    await store.create_session('s')
    fsyncs = []
    original = os.fsync
    monkeypatch.setattr(os, 'fsync', lambda fd: (fsyncs.append(fd), original(fd))[-1])
    await store.append('s', [{'role': 'user', 'content': 'one'}, {'role': 'assistant', 'content': 'two'}])
    assert len(fsyncs) == 1
    await store.append('s', [{'role': 'user', 'content': 'three'}])
    history = await store.load_history('s')
    assert [r['seq'] for r in history] == [0, 1, 2]
    fresh = SessionStore(str(tmp_path), 'u')
    assert len(await fresh.load_history('s')) == 3


def test_history_window_preserves_tool_exchange():
    history = [
        {'role': 'user', 'content': 'old'},
        {'role': 'assistant', 'tool_calls': [{'id': 'x'}]},
        {'role': 'tool', 'tool_call_id': 'x', 'content': 'result'},
        {'role': 'assistant', 'content': 'old-done'},
        {'role': 'user', 'content': 'new'},
        {'role': 'assistant', 'content': 'new-done'},
    ]
    window = AgentRunner._history_window(history, {'max_messages': 4, 'max_chars': 10000})
    assert window == history[-2:]
    assert AgentRunner._history_window(history, {'max_messages': 0}) == history


async def mock_server(handler):
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    return runner, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'


@pytest.mark.asyncio
async def test_llm_retries_releases_slots_and_rpm_cancellation():
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return web.json_response({}, status=429, headers={'Retry-After': '0'})
        return web.json_response({'choices': [{'message': {'content': 'ok'}}]})

    server, base = await mock_server(handler)
    limiter = UpstreamLimiter(1)
    async with aiohttp.ClientSession() as session:
        client = HTTPLLMClient(
            {'model': 'test', 'base_url': base, 'upstream': {'retry': POLICY}}, session, limiter=limiter
        )
        assert (await client.complete([]))['message']['content'] == 'ok'
        assert len(calls) == 3 and limiter.inflight == 0
    await server.cleanup()
    rate = UpstreamLimiter(1, rpm=1)
    async with rate:
        pass

    async def waiting():
        async with rate:
            pass

    task = asyncio.create_task(waiting())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert rate._semaphore._value == 1 and rate.inflight == 0


@pytest.mark.asyncio
async def test_image_poll_tolerates_more_than_three_errors_without_resubmit():
    calls = {'submit': 0, 'poll': 0}

    async def handler(request):
        if request.method == 'POST':
            calls['submit'] += 1
            return web.json_response({'task_id': 'task', 'task_status': 'PENDING'})
        calls['poll'] += 1
        if calls['poll'] <= 4:
            return web.json_response({}, status=503)
        return web.json_response({'task_status': 'SUCCEED', 'output_images': ['https://example.com/x.png']})

    server, base = await mock_server(handler)
    spec = APIToolSpec(
        'image',
        'image',
        APIConfig(async_mode=True, path='/submit', poll_path='/tasks/{task_id}', poll_timeout=0.5, poll_interval=0.001),
        {},
        OutputMapping(task_id_path='task_id', status_path='task_status', success_path='output_images'),
    )
    async with aiohttp.ClientSession() as session:
        adapter = APIToolAdapter(spec, base, AuthConfig(type='none'), session)
        adapter.upstreams = {'poll': {'retry': POLICY}, 'submit': {'retry': POLICY}}
        result = await adapter.execute({})
        assert result.success and calls == {'submit': 1, 'poll': 5}
    await server.cleanup()


@pytest.mark.asyncio
async def test_image_unknown_delivery_is_not_retried():
    calls = []

    async def handler(request):
        calls.append(1)
        return web.json_response({}, status=502)

    server, base = await mock_server(handler)
    spec = APIToolSpec(
        'image',
        'image',
        APIConfig(async_mode=True, path='/submit', poll_path='/tasks/{task_id}'),
        {},
        OutputMapping(task_id_path='task_id', status_path='task_status'),
    )
    async with aiohttp.ClientSession() as session:
        adapter = APIToolAdapter(spec, base, AuthConfig(type='none'), session)
        adapter.upstreams = {'submit': {'retry': POLICY}}
        result = await adapter.execute({})
        assert not result.success and len(calls) == 1
        assert result.metadata['delivery_unknown']
    await server.cleanup()


def test_path_locks_are_collectible(tmp_path):
    import gc

    locks = PathLocks()
    lock = locks.for_path(tmp_path)
    assert len(locks._locks) == 1
    del lock
    gc.collect()
    assert not locks._locks


@pytest.mark.asyncio
async def test_cancelled_session_tail_preserves_fifo(runtime):
    runs, llm = runtime
    llm.delay = 0.05
    a, b, c, d = (request(content=name) for name in ('a', 'b', 'c', 'd'))
    await runs.start('u1', a)
    await wait_for(runs, a['run_uuid'], 'running')
    await runs.start('u1', b)
    await runs.start('u1', c)
    await runs.command('u1', b['run_uuid'], {'command_id': 'stop-b', 'type': 'stop', 'payload': {}})
    await runs.command('u1', c['run_uuid'], {'command_id': 'stop-c', 'type': 'stop', 'payload': {}})
    await asyncio.sleep(0)
    await runs.start('u1', d)
    await wait_for(runs, d['run_uuid'], 'succeeded')
    assert len(llm.calls) == 2
    assert llm.calls[0][-1]['content'] == 'a'
    assert any(m.get('content') == '完成' for m in llm.calls[1][:-1])
    assert llm.calls[1][-1]['content'] == 'd'


@pytest.mark.asyncio
async def test_cancelled_predecessor_does_not_release_later_run_early(runtime):
    runs, llm = runtime
    llm.delay = 0.05
    a, b, c = (request(content=name) for name in ('a', 'b', 'c'))
    await runs.start('u1', a)
    await wait_for(runs, a['run_uuid'], 'running')
    await runs.start('u1', b)
    await runs.start('u1', c)
    await runs.command('u1', b['run_uuid'], {'command_id': 'stop-b', 'type': 'stop', 'payload': {}})
    await wait_for(runs, c['run_uuid'], 'succeeded')
    assert len(llm.calls) == 2
    assert any(m.get('content') == '完成' for m in llm.calls[1][:-1])


@pytest.mark.asyncio
async def test_cache_eviction_and_nested_event_mutation(tmp_path):
    store = FileRunStore(tmp_path, cache_entries=1, projection='never')
    await store.commit('u', 'runs', 'a', {'status': 'running'}, 'started', {'x': 1})
    _, events = await store.read('u', 'runs', 'a')
    events[0]['data']['x'] = 99
    assert (await store.read_events('u', 'runs', 'a', 0))[1][0]['data']['x'] == 1
    await store.commit('u', 'runs', 'b', {'status': 'running'})
    assert len(store._cache) == 1
    assert (await store.read('u', 'runs', 'a'))[1][0]['data']['x'] == 1


@pytest.mark.asyncio
async def test_projection_failure_preserves_committed_terminal(tmp_path, monkeypatch):
    import ms_agent.session.run_store as module

    store = FileRunStore(tmp_path, projection='terminal')
    await store.recover()
    await store.commit('u', 'runs', 'a', {'status': 'running'})
    original = module.atomic_write_json

    def fail(path, *args, **kwargs):
        if path.name == 'state.json':
            raise OSError('projection write failure')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(module, 'atomic_write_json', fail)
    with pytest.raises(PersistenceError):
        await store.commit('u', 'runs', 'a', {'status': 'succeeded'})
    assert not store.healthy
    new = FileRunStore(tmp_path, projection='never')
    await new.recover()
    assert (await new.read('u', 'runs', 'a'))[0]['status'] == 'succeeded'
    assert not new._index_entries()


@pytest.mark.parametrize(
    'fs_type,options,safe',
    [
        ('nfs', 'vers=3,nolock', False),
        ('nfs4', 'vers=4.1,local_lock=flock', False),
        ('nfs4', 'vers=4.1,local_lock=none', True),
    ],
)
def test_nfs_lock_policy_fails_closed(tmp_path, monkeypatch, fs_type, options, safe):
    exists, read_text = Path.exists, Path.read_text
    monkeypatch.setattr(Path, 'exists', lambda self: True if str(self) == '/proc/self/mountinfo' else exists(self))
    line = f'1 0 0:1 / / rw - {fs_type} server:/ rw,{options}\n'
    monkeypatch.setattr(
        Path,
        'read_text',
        lambda self, *a, **kw: line if str(self) == '/proc/self/mountinfo' else read_text(self, *a, **kw),
    )
    if safe:
        assert not FileLease(tmp_path).check_filesystem()['unsafe_lock']
    else:
        with pytest.raises(RuntimeError, match='NFSv4'):
            FileLease(tmp_path).check_filesystem()


def test_eas_render_preserves_visible_environment_overrides(configured, monkeypatch):
    monkeypatch.setenv('LLM_MODEL', 'test-model')
    settings = Settings.load()
    settings.eas.image, settings.eas.nas_server = 'registry.example/app:v1', 'nas.example'
    document = render_eas(settings, 0)
    env = {entry['name']: entry['value'] for entry in document['containers'][0]['env']}
    assert env['LLM_MODEL'] == 'test-model'
    assert 'LLM_API_KEY' not in env


@pytest.mark.asyncio
async def test_sse_quota_and_openapi_follow_auth_configuration(settings):
    settings.auth.header, settings.auth.scheme = 'X-AIGC-Service-Token', 'raw'
    settings.events.max_subscribers = 1
    app = create_app(settings, llm_client=FakeLLM(delay=0.02))
    async with app.router.lifespan_context(app):
        headers = {'X-AIGC-Service-Token': 'test-service-secret', 'X-User-Id': 'u1'}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test', headers=headers
        ) as client:
            schema = (await client.get('/openapi.json')).json()
            assert schema['components']['securitySchemes']['AIGCServiceToken']['name'] == settings.auth.header
            body = request()
            await client.post('/v1/aigc/runs', json=body)
            app.state.runs.reserve_subscription()
            busy = await client.get(f'/v1/aigc/runs/{body["run_uuid"]}/events')
            assert busy.status_code == 429 and busy.json()['code'] == 'EVENT_SUBSCRIBERS_FULL'
            app.state.runs.subscribers -= 1
            await wait_for(app.state.runs, body['run_uuid'])
            result = await client.get(f'/v1/aigc/runs/{body["run_uuid"]}/events')
            assert result.status_code == 200 and app.state.runs.subscribers == 0


@pytest.mark.asyncio
async def test_drain_allows_active_run_to_complete(runtime):
    runs, llm = runtime
    llm.delay = 0.02
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'], 'running')
    await runs.close(drain_timeout=0.2)
    assert (await runs.get('u1', body['run_uuid']))['status'] == 'succeeded'
    assert not runs._tasks and not runs.store.lease.held


@pytest.mark.asyncio
async def test_interaction_timeout_rejects_late_approval(runtime):
    runs, llm = runtime
    runs._interaction_timeout = 0.01
    llm.messages = [
        {'role': 'assistant', 'tool_calls': [{'id': 't', 'function': {'name': 'test_tool', 'arguments': '{}'}}]}
    ]
    runs.agent._tool_registry.register(
        APIToolSpec('test_tool', 'test', APIConfig(), {'type': 'object'}, OutputMapping(), requires_approval=True),
        'http://unused.invalid',
        AuthConfig(type='none'),
    )
    body = request()
    await runs.start('u1', body)
    view = await wait_for(runs, body['run_uuid'], 'failed')
    assert view['error']['code'] == 'INTERACTION_TIMEOUT'
    rows = [row async for row in runs.events('u1', body['run_uuid'])]
    pending = next(row['data'] for row in rows if row['event_type'] == 'interaction_requested')
    receipt = await runs.command(
        'u1',
        body['run_uuid'],
        {'command_id': 'late', 'type': 'approve', 'interaction_id': pending['interaction_id'], 'payload': {}},
    )
    assert receipt['reason_code'] == 'INTERACTION_EXPIRED' and len(llm.calls) == 1


@pytest.mark.asyncio
async def test_drain_during_acceptance_does_not_launch_model(runtime, monkeypatch):
    runs, llm = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    snapshot = runs.store.snapshot

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        await snapshot(*args, **kwargs)

    monkeypatch.setattr(runs.store, 'snapshot', delayed)
    body = request()
    submit = asyncio.create_task(runs.start('u1', body))
    await entered.wait()
    runs.begin_drain()
    release.set()
    await submit
    view = await wait_for(runs, body['run_uuid'], 'interrupted')
    assert view['error']['code'] == 'SERVICE_INTERRUPTED'
    assert not llm.calls
