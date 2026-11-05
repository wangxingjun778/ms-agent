"""图片 API 契约、错误、取消与 task_id 持久化顺序。"""

import asyncio
from unittest.mock import Mock

import aiohttp
import pytest

from ms_agent.session.locking import PersistenceError
from ms_agent.tools.api_adapter import (
    APIConfig,
    APIToolAdapter,
    APIToolSpec,
    AuthConfig,
    OutputMapping,
    _parse_json_or_wrap,
)
from ms_agent.tools.api_registry import APIToolRegistry
from ms_agent.tools.schema import ToolInvocationContext


def adapter():
    spec = APIToolSpec(
        'image_generation',
        '图片',
        APIConfig(
            path='/v1/images/generations',
            async_mode=True,
            poll_path='/v1/tasks/{task_id}',
            poll_interval=0.01,
            headers={'X-ModelScope-Async-Mode': 'true', 'X-ModelScope-Task-Type': 'image_generation'},
        ),
        {'type': 'object'},
        OutputMapping(success_path='output_images', task_id_path='task_id', status_path='task_status'),
    )
    return APIToolAdapter(
        spec, 'http://test', AuthConfig(type='bearer', token='fake-token'), http_client=Mock(spec=aiohttp.ClientSession)
    )


@pytest.mark.asyncio
async def test_submit_persist_before_poll_and_full_images(monkeypatch):
    tool = adapter()
    log = []

    async def record(kind, data):
        log.append((kind, data))

    async def transport(method, url, headers, **kwargs):
        assert headers['Authorization'] == 'Bearer fake-token'
        if method == 'POST':
            assert headers['X-ModelScope-Async-Mode'] == 'true'
            return {'task_id': 'task/a', 'task_status': 'SUCCEED', 'request_id': 'request1'}
        assert log[0][0] == 'task_submitted' and log[0][1]['task_id'] == 'task/a'
        assert url.endswith('task%2Fa') and headers['X-ModelScope-Task-Type'] == 'image_generation'
        return {
            'task_id': 'task/a',
            'task_status': 'SUCCEED',
            'output_images': ['https://test/a.png', 'https://test/b.png'],
        }

    monkeypatch.setattr(tool, '_request', transport)
    result = await tool.execute({}, ToolInvocationContext('u1', 's1', 'r1', 'call1', record=record))
    assert result.success and len(result.raw_data['output_images']) == 2
    assert result.metadata['task_id'] == 'task/a'


@pytest.mark.asyncio
async def test_submission_not_retried_and_delivery_unknown(monkeypatch):
    tool = adapter()
    attempts, records = [], []

    async def record(kind, data):
        records.append(kind)

    async def transport(*args, **kwargs):
        attempts.append(True)
        raise asyncio.TimeoutError

    monkeypatch.setattr(tool, '_request', transport)
    result = await tool.execute({}, ToolInvocationContext('u1', 's1', 'r1', 'call1', record=record))
    assert not result.success and len(attempts) == 1 and records == ['delivery_unknown']


@pytest.mark.asyncio
async def test_persistence_failure_prevents_poll(monkeypatch):
    tool = adapter()
    calls = []

    async def record(*args):
        raise PersistenceError('disk full')

    async def transport(method, *args, **kwargs):
        calls.append(method)
        return {'task_id': 'task1', 'task_status': 'SUCCEED'}

    monkeypatch.setattr(tool, '_request', transport)
    with pytest.raises(PersistenceError):
        await tool.execute({}, ToolInvocationContext('u1', 's1', 'r1', 'call1', record=record))
    assert calls == ['POST']


@pytest.mark.parametrize(
    'body,status',
    [
        ('{"task_status":"FAILED","errors":{"message":"bad"}}', 200),
        ('{"output_images":["https://test/image.png"]}', 403),
        ('not json', 200),
        ('{"task_status":"SUCCEED","output_images":[]}', 200),
    ],
)
def test_http_and_business_errors(body, status):
    assert not adapter()._shape_result(_parse_json_or_wrap(body, status)).success


@pytest.mark.asyncio
async def test_unknown_status_fails_without_infinite_poll(monkeypatch):
    tool = adapter()

    async def transport(method, *args, **kwargs):
        return {'task_id': 't1', 'task_status': 'SUCCEED' if method == 'POST' else 'UNRECOGNIZED'}

    monkeypatch.setattr(tool, '_request', transport)
    assert not (await tool.execute({})).success


@pytest.mark.asyncio
async def test_cancel_poll_preserves_submitted_task(monkeypatch):
    tool = adapter()
    ready = asyncio.Event()
    records = []

    async def record(kind, data):
        records.append((kind, data))

    async def transport(method, *args, **kwargs):
        if method == 'POST':
            return {'task_id': 't1', 'task_status': 'SUCCEED'}
        ready.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(tool, '_request', transport)
    task = asyncio.create_task(tool.execute({}, ToolInvocationContext('u1', 's1', 'r1', 'call1', record=record)))
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert records[0][1]['task_id'] == 't1'


@pytest.mark.asyncio
async def test_registry_strict_config_and_disabled_tools():
    registry = APIToolRegistry(strict=True)
    try:
        await registry.load_from_dict(
            {
                'base_url': 'http://test',
                'tools': [
                    {
                        'name': 'disabled',
                        'description': 'off',
                        'parameters': {},
                        'enabled': False,
                    }
                ],
            }
        )
        assert registry.get_tool_names() == []
        with pytest.raises(ValueError):
            await registry.load_from_dict({'base_url': 'http://test', 'tools': [{'name': 'invalid'}]})
    finally:
        await registry.close()
