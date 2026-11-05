"""轻量导入闭包、skills、配置开关与执行边界。"""

import asyncio
import json
import subprocess
import sys

import pytest
from conftest import request, wait_for

from ms_agent.agent.context import SkillService
from ms_agent.config.lightweight import load_lightweight_config
from ms_agent.session.locking import PersistenceError


def test_lightweight_import_without_legacy_modules():
    source = """
import sys
from service.aigc.app import create_app
create_app().openapi()
assert not any(name in sys.modules for name in (
    'modelscope', 'ms_agent.agent.llm_agent', 'ms_agent.tools.code',
    'ms_agent.tools.tool_manager', 'ms_agent.memory.unified', 'ms_agent.mcp',
    'ms_agent.workflow', 'sqlite3', 'ms_agent.config.resolver',
))
"""
    subprocess.run([sys.executable, '-c', source], check=True)


def test_skills_full_injection_and_missing_directory(tmp_path):
    root = tmp_path / 'selected-skill'
    root.mkdir()
    (root / 'SKILL.md').write_text('---\nname: Selected\ndescription: test\n---\n只使用图片 API。', encoding='utf-8')
    skill = SkillService([str(root)], {'strict': True})
    content = skill.build_skill_prompt_section()
    assert '只使用图片 API' in content and 'skill_view' not in content
    assert len(skill.get_active_skills()) == 1
    with pytest.raises(ValueError):
        SkillService([str(tmp_path / 'missing')], {'strict': True}).build_skill_prompt_section()


@pytest.mark.parametrize(
    'patch',
    [
        {'modules': {'sandbox': True}},
        {'tools': {'builtin_enabled': True}},
        {'memory': {'storage_backend': 'sqlite'}},
    ],
)
def test_service_rejects_forbidden_modules(patch):
    patch['llm'] = {'transport': 'http', 'model': 'test', 'api_key': 'fake', 'base_url': 'http://test/v1'}
    with pytest.raises(ValueError):
        load_lightweight_config(patch, strict=True)


@pytest.mark.asyncio
async def test_disabled_memory_does_not_create_files(runtime):
    runs, _ = runtime
    runs.agent._config['memory']['enabled'] = False
    body = request()
    await runs.start('u1', body)
    assert (await wait_for(runs, body['run_uuid']))['status'] == 'succeeded'
    assert not (runs.store.root / 'users' / 'u1' / 'sessions' / 's1' / 'memory').exists()


@pytest.mark.asyncio
async def test_disk_failure_before_llm_does_not_execute(runtime, monkeypatch):
    runs, llm = runtime
    original = runs.store.snapshot

    async def snapshot(user, kind, identifier, name, data):
        if name == 'execution_context.json':
            runs.store.healthy = False
            raise PersistenceError('disk full')
        return await original(user, kind, identifier, name, data)

    monkeypatch.setattr(runs.store, 'snapshot', snapshot)
    body = request()
    await runs.start('u1', body)
    await asyncio.gather(*runs._tasks.values())
    assert not llm.calls and not runs.accepting


@pytest.mark.asyncio
async def test_corrupt_session_fails_closed(runtime):
    runs, llm = runtime
    first = request()
    await runs.start('u1', first)
    await wait_for(runs, first['run_uuid'])
    log = runs.store.root / 'users' / 'u1' / 'sessions' / 's1' / 's1.jsonl'
    with log.open('a') as handle:
        handle.write('{bad json}\n')
    second = request(content='不能继续')
    await runs.start('u1', second)
    view = await wait_for(runs, second['run_uuid'])
    assert view['status'] == 'interrupted' and view['error']['code'] == 'STORAGE_UNAVAILABLE'
    assert len(llm.calls) == 1 and not runs.accepting


@pytest.mark.asyncio
async def test_intervention_applies_at_next_boundary(runtime):
    runs, llm = runtime
    llm.delay = 0.1
    body = request()
    await runs.start('u1', body)
    async with asyncio.timeout(3):
        while not llm.calls:
            await asyncio.sleep(0.01)
    intervention = {
        'command_id': 'intervene1',
        'type': 'intervene',
        'interaction_id': None,
        'payload': {'content': '新的指令'},
    }
    assert (await runs.command('u1', body['run_uuid'], intervention))['status'] == 'accepted'
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'succeeded' and len(llm.calls) == 2
    assert llm.calls[1][-1]['content'] == '新的指令'
    assert view['commands']['intervene1']['status'] == 'applied'
