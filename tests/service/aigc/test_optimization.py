"""单 Pod 性能机制和集中配置在联调接口中保持有效。"""

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from conftest import request, wait_for
from pydantic import ValidationError

from ms_agent.agent.scheduler import FairScheduler
from ms_agent.llm.http_client import HTTPLLMClient
from ms_agent.session.locking import FileLease, PathLocks, PersistenceError
from ms_agent.session.run_store import FileRunStore, StorageCorrupted
from ms_agent.session.store import SessionStore
from ms_agent.tools.api_adapter import APIConfig, APIToolAdapter, APIToolSpec, AuthConfig, OutputMapping
from ms_agent.utils.limiter import UpstreamLimiter
from service.aigc.deploy import render_eas
from service.aigc.settings import Settings

POLICY = {'attempts': 3, 'initial': 0.001, 'maximum': 0.005, 'budget': 1}


@pytest.mark.parametrize(
    'legacy',
    [
        {'data_dir': '/tmp/old'},
        {'max_runs': 4},
        {'queue_limit': 100},
        {'agent_config': {}},
        {'service_token': 'old'},
        {'runtime': {'queue_limit': 10}},
        {'agent': {'memory_enabled': True}},
        {'agent': {'history': {}}},
    ],
)
def test_removed_configuration_rejected(legacy):
    with pytest.raises(ValidationError):
        Settings.model_validate(legacy)


@pytest.mark.parametrize(
    'override',
    [
        {'eas': {'instance': 2}},
        {'server': {'workers': 2}},
        {'runtime': {'max_active_runs': 100, 'max_inflight_runs': 2}},
        {'upstreams': {'llm': {'concurrency': 33, 'connections': 32}}},
        {'eas': {'enabled': True, 'gateway': 'mse'}, 'server': {'keep_alive_timeout': 5}},
        {'eas': {'enabled': True, 'gateway': 'mse'}, 'events': {'max_stream_seconds': 600}},
        {'secrets': {'api': '../../token'}},
    ],
)
def test_cross_field_validation(override):
    with pytest.raises(ValidationError):
        Settings.model_validate(override)


@pytest.mark.parametrize('name', ['API_TOOLS_CONFIG', 'AIGC_QUEUE_LIMIT'])
def test_obsolete_environment_rejected(monkeypatch, name):
    monkeypatch.setenv(name, 'old')
    with pytest.raises(ValueError, match=name):
        Settings.load(deployment=True)


def test_eas_generator_preserves_single_source_and_isolated_mounts(settings):
    settings.eas.image = 'registry-vpc.example/ns/aigc:test'
    settings.eas.nas_server = 'fs.nas.example'
    rendered = render_eas(settings, 2)
    assert rendered['metadata']['instance'] == 1
    assert rendered['metadata']['rpc'] == {'enable_sigterm': True, 'keepalive': 600000}
    assert rendered['metadata']['rolling_strategy'] == {'max_surge': 0, 'max_unavailable': 1}
    assert rendered['storage'][0]['nfs']['path'] == '/aigc/shard-2'
    assert all(s['nfs']['readOnly'] for s in rendered['storage'][1:])
    text = json.dumps(rendered)
    assert 'worker_threads' not in text and 'test-image-secret' not in text
    with pytest.raises(ValueError):
        render_eas(settings, -1)


@pytest.mark.asyncio
async def test_hot_store_does_not_read_journal_and_two_flushes(tmp_path, monkeypatch):
    store = FileRunStore(tmp_path, projection='never')
    await store.commit('u', 'runs', 'r', {'status': 'running'}, 'started')
    calls = []
    original = os.fsync
    monkeypatch.setattr(os, 'fsync', lambda fd: (calls.append(fd), original(fd))[-1])
    monkeypatch.setattr(store, '_read_sync', lambda *_: pytest.fail('热路径不能回放全部 journal'))
    await store.commit('u', 'runs', 'r', {}, 'text', {'text': 'hello'})
    assert len(calls) == 2
    view, events = await store.read('u', 'runs', 'r')
    assert view['last_agent_seq'] == 2
    events[0]['payload']['x'] = 'tampered'
    assert 'x' not in (await store.read('u', 'runs', 'r'))[1][0]['payload']
    with pytest.raises(ValueError):
        await store.commit('u', 'runs', 'r', {'x': float('nan')})
    assert store.healthy


@pytest.mark.asyncio
async def test_cache_eviction_and_corrupt_isolation(tmp_path):
    store = FileRunStore(tmp_path, cache_entries=1, projection='never')
    await store.commit('u', 'runs', 'a', {'status': 'running'})
    await store.commit('u', 'runs', 'b', {'status': 'running'})
    assert len(store._cache) == 1
    assert (await store.read('u', 'runs', 'a'))[0]['status'] == 'running'
    path = store.directory('u', 'runs', 'a') / 'journal.jsonl'
    path.write_text('{}\n')
    fresh = FileRunStore(tmp_path)
    with pytest.raises(StorageCorrupted):
        await fresh.read('u', 'runs', 'a')
    assert fresh.healthy
    with pytest.raises(StorageCorrupted):
        await fresh.commit('u', 'runs', 'a', {'status': 'queued'})


@pytest.mark.asyncio
async def test_epoch_fails_closed_and_collectible_locks(tmp_path):
    import gc

    store = FileRunStore(tmp_path)
    store.lease.acquire()
    try:
        await store.initialize_epoch()
        await store.check_epoch()
        store._epoch_path.write_text('foreign')
        with pytest.raises(PersistenceError):
            await store.check_epoch()
    finally:
        store.lease.release()
    locks = PathLocks()
    lock = locks.for_path(tmp_path)
    assert len(locks._locks) == 1
    del lock
    gc.collect()
    assert not locks._locks


@pytest.mark.asyncio
async def test_scheduler_fairness_and_cancelled_grant():
    scheduler = FairScheduler(1, 1)
    await scheduler.acquire('a')
    a = asyncio.create_task(scheduler.acquire('a'))
    b = asyncio.create_task(scheduler.acquire('b'))
    await asyncio.sleep(0)
    scheduler.release('a')
    await b
    assert not a.done()
    a.cancel()
    await asyncio.gather(a, return_exceptions=True)
    scheduler.release('b')
    assert not scheduler.active
    await scheduler.acquire('a')
    c = asyncio.create_task(scheduler.acquire('c'))
    await asyncio.sleep(0)
    scheduler.release('a')
    c.cancel()
    await asyncio.gather(c, return_exceptions=True)
    assert not scheduler.active


@pytest.mark.asyncio
async def test_user_admission_concurrent_reservations(runtime):
    runs, llm = runtime
    runs._user_limit = 2
    llm.delay = 0.1
    results = await asyncio.gather(
        *(runs.start('u1', request(session_id=f's{i}')) for i in range(5)), return_exceptions=True
    )
    rejected = [r for r in results if isinstance(r, Exception)]
    assert len(rejected) == 3 and all(r.details['scope'] == 'user' for r in rejected)
    assert not runs._reservations


@pytest.mark.asyncio
async def test_sse_heartbeat_quota_and_lifetime(runtime):
    runs, llm = runtime
    llm.delay = 0.08
    runs._event_config.update(heartbeat_seconds=0.01, max_stream_seconds=0.03, max_subscribers=1)
    body = request()
    await runs.start('u1', body)
    received = [e async for e in runs.events('u1', body['run_uuid'])]
    assert any(e is None for e in received) and runs.subscribers == 0
    await wait_for(runs, body['run_uuid'])
    assert [e async for e in runs.events('u1', body['run_uuid'])][-1]['event_type'] == 'run.completed'


@pytest.mark.asyncio
async def test_sdk_session_batch_flush_and_seq(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path), 'u')
    await store.create_session('s')
    calls = []
    original = os.fsync
    monkeypatch.setattr(os, 'fsync', lambda fd: (calls.append(fd), original(fd))[-1])
    await store.append('s', [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}])
    assert len(calls) == 1 and [r['seq'] for r in await store.load_history('s')] == [0, 1]


async def mock_server(handler):
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    return runner, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'


@pytest.mark.asyncio
async def test_llm_retry_releases_slots_and_rpm_cancellation():
    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return web.json_response({}, status=429, headers={'Retry-After': '0'})
        return web.json_response({'choices': [{'message': {'content': 'ok'}}]})

    server, base = await mock_server(handler)
    limiter = UpstreamLimiter(1)
    try:
        async with aiohttp.ClientSession() as session:
            client = HTTPLLMClient(
                {'model': 'test', 'base_url': base, 'upstream': {'retry': POLICY}}, session, limiter=limiter
            )
            assert (await client.complete([]))['message']['content'] == 'ok'
            assert len(calls) == 3 and limiter.inflight == 0
    finally:
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
    assert rate._semaphore._value == 1


@pytest.mark.asyncio
async def test_image_poll_tolerates_errors_without_resubmit():
    calls = {'post': 0, 'get': 0}

    async def handler(request):
        if request.method == 'POST':
            calls['post'] += 1
            return web.json_response({'task_id': 'task', 'task_status': 'PENDING'})
        calls['get'] += 1
        if calls['get'] < 5:
            return web.json_response({}, status=503)
        return web.json_response({'task_status': 'SUCCEED', 'output_images': ['https://example.test/image.png']})

    server, base = await mock_server(handler)
    spec = APIToolSpec(
        'image',
        'image',
        APIConfig(async_mode=True, path='/submit', poll_path='/tasks/{task_id}', poll_timeout=1),
        {},
        OutputMapping(task_id_path='task_id', status_path='task_status', success_path='output_images'),
    )
    try:
        async with aiohttp.ClientSession() as session:
            adapter = APIToolAdapter(spec, base, AuthConfig(type='none'), session)
            adapter.upstreams = {'poll': {'retry': POLICY}, 'submit': {'retry': POLICY}}
            assert (await adapter.execute({})).success and calls == {'post': 1, 'get': 5}
    finally:
        await server.cleanup()
