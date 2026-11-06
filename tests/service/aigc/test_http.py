"""五接口 HTTP 契约与鉴权、ETag、流式边界。"""

import asyncio
import json

import httpx
import pytest
from conftest import FakeLLM, request, wait_for

from service.aigc.app import create_app

pytestmark = pytest.mark.asyncio
HEADERS = {'Authorization': 'Bearer test-service-secret', 'X-User-Id': 'u1'}


async def test_http_endpoints_and_sse(settings):
    app = create_app(settings, llm_client=FakeLLM())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test', headers=HEADERS
        ) as client:
            assert (await client.get('/health/ready')).status_code == 200
            assert (await client.post('/v2/aigc/runs', json={})).status_code == 404
            body = request()
            response = await client.post('/v1/aigc/runs', json=body)
            assert response.status_code == 202 and response.json()['schema_version'] == 1
            identifier = body['run_uuid']
            await wait_for(app.state.runs, identifier)
            duplicate = await client.post('/v1/aigc/runs', json=body)
            assert duplicate.status_code == 200
            url = f'/v1/aigc/runs/{identifier}'
            result = await client.get(url)
            cached = await client.get(url, headers={'If-None-Match': result.headers['ETag']})
            assert cached.status_code == 304 and not cached.content
            assert (await client.get(url, headers={'X-User-Id': 'u2'})).status_code == 404
            events = await client.get(url + '/events')
            rows = [json.loads(line[6:]) for line in events.text.splitlines() if line.startswith('data: ')]
            assert rows[-1]['event_type'] == 'run.completed'
            assert all('payload' in row and 'data' not in row for row in rows)
            for cursor in ('999', '-1', 'invalid'):
                invalid = await client.get(url + '/events', params={'after_seq': cursor})
                assert invalid.status_code == 400 and invalid.json()['code'] == 'EVENT_CURSOR_INVALID'
            assert (await client.get(url + '/events', headers={'Last-Event-ID': 'invalid'})).status_code == 400
            replay = await client.get(url + '/events?after_seq=2', headers={'Last-Event-ID': 'ignored-invalid'})
            assert [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith('data: ')] == rows[2:]
            assert (await client.post('/v1/aigc/runs', json={**body, 'user_id': 'fake'})).status_code == 422
            operations = {v['operationId'] for path in app.openapi()['paths'].values() for v in path.values()}
            assert {'StartRun', 'GetRun', 'SubscribeRunEvents', 'ControlRun', 'GetOrBuildRecipe'} <= operations
            assert app.openapi()['info']['version'] == '1.0.0'


async def test_auth_prefix_and_body_boundary(settings):
    settings.auth.header, settings.auth.scheme = 'X-AIGC-Service-Token', 'raw'
    settings.server.public_path_prefix = '/api/predict/aigc_0'
    settings.server.max_body_bytes = 1024
    app = create_app(settings, llm_client=FakeLLM())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/internal/stats')).status_code == 401
            headers = {'X-AIGC-Service-Token': 'test-service-secret', 'X-User-Id': 'u1'}
            response = await client.post('/v1/aigc/runs', content=b'x' * 2048, headers=headers)
            assert response.status_code == 413 and 'X-Request-Id' in response.headers
            settings.server.max_body_bytes = 1048576
            response = await client.post('/v1/aigc/runs', json=request(), headers=headers)
            assert response.json()['links']['self'].startswith('/api/predict/aigc_0/v1/aigc/')
            assert (await client.get('/internal/stats', headers=headers)).status_code == 200
            assert app.openapi()['components']['securitySchemes']['AIGCServiceToken']['name'] == settings.auth.header


async def test_http_control_before_start(settings):
    llm = FakeLLM()
    app = create_app(settings, llm_client=llm)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test', headers=HEADERS
        ) as client:
            body = request()
            url = f'/v1/aigc/runs/{body["run_uuid"]}/commands'
            stop = await client.post(url, json={'command_id': 'stop', 'action': 'stop'})
            assert stop.status_code == 200 and stop.json()['can_release_activity']
            started = await client.post('/v1/aigc/runs', json=body)
            assert started.status_code == 200 and started.json()['status'] == 'cancelled'
            invalid = await client.post(url, json={'command_id': 'approve', 'action': 'approve'})
            assert invalid.status_code == 422 and not llm.calls


async def test_http_recipe_result(settings):
    generation = {
        'prompt_template': '介绍 {{subject}}',
        'negative_prompt_template': '',
        'variables': [{'name': 'subject', 'type': 'string', 'required': True, 'description': '主题'}],
        'examples': [{'description': '示例', 'inputs': {'subject': '猫'}}],
    }
    app = create_app(settings, llm_client=FakeLLM(), recipe_llm=FakeLLM([{'content': json.dumps(generation)}]))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test', headers=HEADERS
        ) as client:
            body = request()
            await client.post('/v1/aigc/runs', json=body)
            await wait_for(app.state.runs, body['run_uuid'])
            url = f'/v1/aigc/runs/{body["run_uuid"]}/recipe'
            payload = {'recipe_request_id': 'r1', 'output_ids': ['final_text']}
            first = await client.post(url, json=payload)
            assert first.status_code == 202
            async with asyncio.timeout(3):
                while True:
                    response = await client.post(url, json=payload)
                    if response.status_code == 200:
                        break
                    await asyncio.sleep(0.002)
            assert response.json()['result']['schema_version'] == 1
            assert response.json()['attempt'] == 1


async def test_disconnect_during_acceptance_does_not_duplicate(runtime, monkeypatch):
    runs, llm = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    original = runs.store.snapshot

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(runs.store, 'snapshot', delayed)
    body = request()
    pending = asyncio.create_task(runs.start('u1', body))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    release.set()
    await asyncio.gather(*runs._operations)
    await wait_for(runs, body['run_uuid'])
    assert not (await runs.start('u1', body))[1] and len(llm.calls) == 1
