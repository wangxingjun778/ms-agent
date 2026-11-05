"""未确认外部副作用与结果提交边界不得触发隐式重跑。"""

import asyncio
import json

import pytest
from conftest import request, wait_for

from ms_agent.tools.api_adapter import APIConfig, APIToolSpec, AuthConfig, OutputMapping
from ms_agent.tools.schema import ToolResult

pytestmark = pytest.mark.asyncio


async def test_unresolved_tool_stops_llm_replanning(runtime, monkeypatch):
    runs, llm = runtime
    llm.messages = [
        {
            'role': 'assistant',
            'content': '',
            'tool_calls': [{'id': 'c1', 'function': {'name': 'image_generation', 'arguments': '{}'}}],
        }
    ]
    registry = runs.agent._tool_registry
    registry.register(
        APIToolSpec('image_generation', 'image', APIConfig(), {'type': 'object'}, OutputMapping()),
        'http://unused.invalid',
        AuthConfig(),
    )
    calls = []

    async def execute(name, args, context):
        calls.append(True)
        await context.emit('delivery_unknown', {'tool_name': name, 'remote_cancel_supported': False})
        return ToolResult.from_error(name, '未确认', delivery_unknown=True)

    monkeypatch.setattr(registry, 'execute', execute)
    body = request()
    await runs.start('u1', body)
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'failed' and view['error']['code'] == 'EXTERNAL_TASK_UNRESOLVED'
    assert view['requires_reconciliation'] and len(calls) == len(llm.calls) == 1
    assert (await runs.start('u1', body))[1] is False


async def test_partial_result_survives_cancel_during_session_commit(runtime, monkeypatch):
    from ms_agent.session.store import SessionStore

    runs, _ = runtime
    original = SessionStore.append
    saving = asyncio.Event()

    async def delayed(self, session_id, messages):
        if any(message['role'] == 'assistant' for message in messages):
            saving.set()
            await asyncio.sleep(10)
        await original(self, session_id, messages)

    monkeypatch.setattr(SessionStore, 'append', delayed)
    body = request()
    await runs.start('u1', body)
    await asyncio.wait_for(saving.wait(), 3)
    await runs.close()
    state, _ = await runs.store.read('u1', 'runs', body['run_uuid'])
    assert state['status'] == 'interrupted'
    assert state['result']['messages'][0]['content'] == '完成'
    assert state['result']['metadata']['partial']


async def test_complete_corrupt_record_must_not_erase_idempotency(runtime):
    from ms_agent.session.run_store import StorageCorrupted

    runs, _ = runtime
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'])
    path = runs.store.directory('u1', 'runs', body['run_uuid']) / 'journal.jsonl'
    lines = path.read_text().splitlines()
    record = json.loads(lines[-1])
    record['checksum'] = '0' * 64
    lines[-1] = json.dumps(record)
    path.write_text('\n'.join(lines) + '\n')
    # 活跃服务禁止外部写者改文件；此处模拟重启后冷加载发现损坏。
    runs.store._cache.clear()
    runs.store._cached_bytes = 0
    with pytest.raises(StorageCorrupted):
        await runs.start('u1', body)
    assert runs.store.healthy
