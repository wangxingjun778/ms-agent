"""HTTP 认证、参数、SSE 回放及完整 OpenAPI。"""

import asyncio
import json

import httpx
import pytest
from conftest import FakeLLM, request, wait_for

from service.aigc.app import create_app

pytestmark = pytest.mark.asyncio
_HEADERS = {'Authorization': 'Bearer test-service-secret', 'X-User-Id': 'u1'}


async def test_http_endpoints_and_sse(settings):
    app = create_app(settings, llm_client=FakeLLM())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/health/ready')).status_code == 200
            body = request()
            assert (await client.post('/v1/aigc/runs', json=body)).status_code == 401
            response = await client.post('/v1/aigc/runs', json=body, headers=_HEADERS)
            assert response.status_code == 202
            identifier = response.json()['run_uuid']
            await wait_for(app.state.runs, identifier)
            duplicate = await client.post('/v1/aigc/runs', json=body, headers=_HEADERS)
            assert duplicate.status_code == 200
            assert duplicate.json()['status'] == 'succeeded'
            assert duplicate.json()['links']['events'].endswith('/events')
            foreign = await client.get(f'/v1/aigc/runs/{identifier}', headers={**_HEADERS, 'X-User-Id': 'u2'})
            assert foreign.status_code == 404
            events = await client.get(f'/v1/aigc/runs/{identifier}/events', headers=_HEADERS)
            assert events.status_code == 200 and events.headers['content-type'].startswith('text/event-stream')
            rows = [json.loads(line[6:]) for line in events.text.splitlines() if line.startswith('data: ')]
            assert rows[-1]['event_type'] == 'run_terminal'
            replay = await client.get(f'/v1/aigc/runs/{identifier}/events', headers={**_HEADERS, 'Last-Event-ID': '2'})
            assert [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith('data: ')] == rows[2:]
            invalid = await client.get(f'/v1/aigc/runs/{identifier}/events?after_seq=999', headers=_HEADERS)
            assert invalid.status_code == 400
            bad = await client.post('/v1/aigc/runs', json={**body, 'user_id': 'fake'}, headers=_HEADERS)
            assert bad.status_code == 422 and bad.json()['code'] == 'INPUT_INVALID'
            assert 'x-request-id' in bad.headers
            assert not any('input' in row for row in bad.json()['details'])
            schema = app.openapi()
            operations = {value['operationId'] for path in schema['paths'].values() for value in path.values()}
            assert {'StartRun', 'GetRun', 'SubscribeRunEvents', 'ControlRun', 'GetOrBuildRecipe'} <= operations


async def test_http_control_before_start_and_payload_validation(settings):
    llm = FakeLLM()
    app = create_app(settings, llm_client=llm)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test', headers=_HEADERS
        ) as client:
            body = request()
            response = await client.post(
                f'/v1/aigc/runs/{body["run_uuid"]}/commands', json={'command_id': 'stop1', 'type': 'stop'}
            )
            assert response.status_code == 200 and response.json()['status'] == 'applied'
            response = await client.post('/v1/aigc/runs', json=body)
            assert response.status_code == 200 and response.json()['status'] == 'cancelled'
            assert not llm.calls
            invalid = await client.post(
                f'/v1/aigc/runs/{body["run_uuid"]}/commands', json={'command_id': 'approval', 'type': 'approve'}
            )
            assert invalid.status_code == 422
            invalid = await client.post('/v1/aigc/runs', json={**request(), 'session_id': '../escape'})
            assert invalid.status_code == 422


async def test_http_recipe_result_schema(settings):
    result = {
        'prompt_template': '生成 {{subject}}',
        'variables': [{'name': 'subject', 'type': 'string', 'description': '主题'}],
        'examples': [],
    }
    builder_llm = FakeLLM([{'role': 'assistant', 'content': json.dumps(result)}])
    app = create_app(settings, llm_client=FakeLLM(), recipe_llm=builder_llm)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test', headers=_HEADERS
        ) as client:
            body = request()
            await client.post('/v1/aigc/runs', json=body)
            await wait_for(app.state.runs, body['run_uuid'])
            url = f'/v1/aigc/runs/{body["run_uuid"]}/recipe'
            payload = {'recipe_request_id': 'recipe-http', 'artifact_ids': ['final_text']}
            initial = await client.post(url, json=payload)
            assert initial.status_code == 202
            async with asyncio.timeout(3):
                while True:
                    response = await client.post(url, json=payload)
                    if response.status_code == 200:
                        break
                    await asyncio.sleep(0.01)
            assert response.json()['result']['source']['run_uuid'] == body['run_uuid']
            assert len(builder_llm.calls) == 1


async def test_body_size_limit(settings):
    settings.server.max_body_bytes = 1024
    app = create_app(settings, llm_client=FakeLLM())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/v1/aigc/runs', content=b'x' * 2048, headers=_HEADERS)
            assert response.status_code == 413


async def test_live_subscription_replay_transition_and_disconnect(runtime):
    runs, llm = runtime
    llm.delay = 0.1
    body = request()
    await runs.start('u1', body)
    stream = runs.events('u1', body['run_uuid'])
    first = await anext(stream)
    assert first['agent_seq'] == 1
    await stream.aclose()
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'succeeded'
    remaining = [event async for event in runs.events('u1', body['run_uuid'], first['agent_seq'])]
    assert remaining[-1]['agent_seq'] == view['last_agent_seq']


async def test_disconnect_during_acceptance_does_not_duplicate(runtime, monkeypatch):
    runs, llm = runtime
    original = runs.store.snapshot
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(runs.store, 'snapshot', delayed)
    body = request()
    submit = asyncio.create_task(runs.start('u1', body))
    await started.wait()
    submit.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submit
    release.set()
    await asyncio.gather(*runs._operations)
    await wait_for(runs, body['run_uuid'])
    assert (await runs.start('u1', body))[1] is False
    assert len(llm.calls) == 1
