"""联调输入、能力、资产、公开 reducer 与隐私边界。"""

import asyncio
import json
import subprocess
import sys
from copy import deepcopy
from uuid import uuid4

import pytest
from conftest import MODEL, request, wait_for
from pydantic import ValidationError

from ms_agent.agent.protocol import StartRunRequest
from ms_agent.agent.public_state import reduce_public
from ms_agent.agent.run_service import RunError
from ms_agent.session.run_store import digest


def test_lightweight_import_without_legacy_modules():
    source = """
import sys
from service.aigc.app import create_app
assert create_app().openapi()['info']['version'] == '1.0.0'
assert not any(n in sys.modules for n in ('modelscope','ms_agent.agent.llm_agent','ms_agent.tools.tool_manager','ms_agent.memory.unified','sqlite3'))
"""
    subprocess.run([sys.executable, '-c', source], check=True)


def test_interface_versions_are_fixed_pending_user_approval():
    from ms_agent.agent.protocol import (
        API_PREFIX,
        PROTOCOL_VERSION,
        SERVICE_VERSION,
        EventEnvelope,
        InputSnapshot,
        PublicSnapshot,
        Recipe,
        RecipeView,
        RunView,
    )
    from service.aigc.app import create_app

    assert API_PREFIX == '/v1/aigc'
    assert SERVICE_VERSION == '1.0.0' and PROTOCOL_VERSION == 1
    schema = create_app().openapi()
    assert schema['info']['version'] == '1.0.0'
    business_paths = [path for path in schema['paths'] if path.startswith('/v')]
    assert set(business_paths) == {
        '/v1/aigc/runs',
        '/v1/aigc/runs/{run_uuid}',
        '/v1/aigc/runs/{run_uuid}/events',
        '/v1/aigc/runs/{run_uuid}/commands',
        '/v1/aigc/runs/{run_uuid}/recipe',
    }
    for model in (StartRunRequest, InputSnapshot, PublicSnapshot, RunView, EventEnvelope, Recipe, RecipeView):
        version = model.model_json_schema()['properties']['schema_version']
        assert version['const'] == version['default'] == 1


@pytest.mark.asyncio
async def test_journal_snapshot_and_events_use_same_approved_version(runtime):
    runs, _ = runtime
    body = request()
    assert body['schema_version'] == body['input_snapshot']['schema_version'] == 1
    await runs.start('u1', body)
    view = await wait_for(runs, body['run_uuid'])
    assert view['schema_version'] == view['public_snapshot']['schema_version'] == 1
    path = runs.store.directory('u1', 'runs', body['run_uuid']) / 'journal.jsonl'
    for line in path.read_text().splitlines():
        payload = json.loads(line)['payload']
        assert payload['schema_version'] == payload['updates']['schema_version'] == 1
        if payload['event']:
            assert payload['event']['schema_version'] == 1
    snapshot = await runs.store.read_snapshot('u1', 'runs', body['run_uuid'], 'input_snapshot.json')
    assert snapshot['schema_version'] == snapshot['input_snapshot']['schema_version'] == 1


def test_documented_paths_versions_and_json_examples_match_current_contract():
    import re
    from pathlib import Path

    from ms_agent.agent.protocol import EventEnvelope

    root = Path(__file__).resolve().parents[3]
    text = (root / 'service/README.md').read_text(encoding='utf-8')
    assert '接口前缀：`/v1/aigc`' in text and '服务声明：`1.0.0`' in text
    assert '任何接口版本变更必须先获得用户审批' in text
    assert '/v2/aigc' not in text
    for path in (
        '/runs',
        '/runs/{run_uuid}',
        '/runs/{run_uuid}/events',
        '/runs/{run_uuid}/commands',
        '/runs/{run_uuid}/recipe',
    ):
        assert f'`/v1/aigc{path}`' in text
    for raw in re.findall(r'```json\n(.*?)\n```', text, re.DOTALL):
        example = json.loads(raw)
        if 'schema_version' in example:
            assert example['schema_version'] == 1
        if 'input_snapshot' in example:
            StartRunRequest.model_validate(example)
    event = json.loads(re.search(r'^data: (.+)$', text, re.MULTILINE)[1])
    assert EventEnvelope.model_validate(event).schema_version == 1
    for target in re.findall(r'\]\(([^)]+)\)', text):
        if not target.startswith(('https://', 'http://', '#')):
            assert (root / 'service' / target.split('#', 1)[0]).is_file(), target
    for name in ('README.md', 'README_ZH.md'):
        summary = (root / name).read_text(encoding='utf-8')
        assert '/v2/aigc' not in summary
        assert '/v1/aigc' in summary


@pytest.mark.parametrize(
    'patch',
    [
        {'messages': []},
        {'parameters': {}},
        {'schema_version': 2},
        {'profile_id': 'aigc_assistant'},
    ],
)
def test_old_input_rejected(patch):
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate(request() | patch)


@pytest.mark.parametrize(
    'mutation',
    ['duplicate-context', 'wrong-parent', 'mcp', 'secret', 'custom-seed', 'custom-negative-prompt', 'duplicate-lora'],
)
def test_snapshot_strict_validation(mutation):
    body = request()
    snapshot = body['input_snapshot']
    message = {'message_uuid': body['parent_message_uuid'], 'role': 'assistant', 'content': [], 'output_refs': []}
    if mutation == 'duplicate-context':
        snapshot['context'] = [message, message]
    elif mutation == 'wrong-parent':
        snapshot['context'] = [{**message, 'message_uuid': str(uuid4())}]
    elif mutation == 'mcp':
        snapshot['asset_refs'] = [{'type': 'mcp', 'resource_ref': 'x', 'revision': '1', 'variables': {}}]
    elif mutation == 'secret':
        snapshot['provenance'] = {'api_key': 'secret'}
    else:
        body = request(custom=True)
        selection = body['input_snapshot']['effective']['custom_selection']
        if mutation == 'duplicate-lora':
            selection['loras'] = [{'model_ref': 'one', 'weight': 0.5}] * 2
        else:
            selection['params'] = {'seed': 42} if mutation == 'custom-seed' else {'negative_prompt': 'override'}
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate(body)


@pytest.mark.asyncio
@pytest.mark.parametrize('feature', ['video', 'byok', 'sampler', 'resolution', 'model'])
async def test_unconfigured_capability_fails_before_acceptance(runtime, feature):
    runs, llm = runtime
    body = request(custom=True)
    effective = body['input_snapshot']['effective']
    if feature == 'video':
        effective['modality'] = 'video'
    elif feature == 'byok':
        effective.update(model_source='byok', byok_connection_id='conn1')
    elif feature == 'sampler':
        effective['custom_selection']['params'] = {'sampler': 'unverified'}
    elif feature == 'resolution':
        effective['preferences']['resolution'] = '4k'
    else:
        effective['custom_selection']['model_ref'] = 'foreign'
    with pytest.raises(RunError) as error:
        await runs.start('u1', body)
    assert error.value.status == (503 if feature == 'byok' else 422) and not llm.calls
    assert not runs._tasks


@pytest.mark.asyncio
async def test_asset_snapshot_digest_and_variables(runtime):
    runs, llm = runtime
    body = request()
    content = {
        'instructions': '使用 {{theme}} 风格',
        'variables_schema': {
            'type': 'object',
            'properties': {'theme': {'type': 'string'}},
            'required': ['theme'],
            'additionalProperties': False,
        },
    }
    ref = {'type': 'skill', 'resource_ref': 'trusted/skill', 'revision': 'r1', 'variables': {'theme': '猫'}}
    body['input_snapshot']['asset_refs'] = [ref]
    body['input_snapshot']['asset_snapshots'] = [
        {k: ref[k] for k in ('type', 'resource_ref', 'revision')}
        | {'content': content, 'content_digest': digest(content)}
    ]
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'])
    assert '猫' in llm.calls[-1][0]['content']
    invalid = deepcopy(body)
    invalid['run_uuid'] = str(uuid4())
    invalid['input_snapshot']['asset_snapshots'][0]['content_digest'] = '0' * 64
    with pytest.raises(RunError):
        await runs.start('u1', invalid)


@pytest.mark.asyncio
async def test_context_budget_is_rejection_not_silent_truncation(runtime):
    runs, llm = runtime
    runs.agent._config['capabilities']['context']['max_chars'] = 10
    with pytest.raises(RunError, match='CONTEXT_TOO_LARGE'):
        await runs.start('u1', request())
    assert not llm.calls


@pytest.mark.asyncio
async def test_public_snapshot_replay_equals_same_watermark(runtime):
    runs, llm = runtime
    body = request()
    await runs.start('u1', body)
    view = await wait_for(runs, body['run_uuid'])
    state, events = await runs.store.read('u1', 'runs', body['run_uuid'])
    replay = None
    # journal 同水位的 state 与公开 payload 同时提交，逐提交验证。
    path = runs.store.directory('u1', 'runs', body['run_uuid']) / 'journal.jsonl'
    current = {}
    for line in path.read_text().splitlines():
        payload = json.loads(line)['payload']
        current.update(payload['updates'])
        if payload['event']:
            replay = reduce_public(replay, None, payload['event'])
            assert replay == current['public_snapshot']
            assert reduce_public(replay, None, payload['event']) == replay
    assert replay == view['public_snapshot'] and len(events) == view['snapshot_agent_seq']
    public = json.dumps(view)
    assert all(s not in public for s in ('test-service-secret', 'test-model-secret', 'test-image-secret'))
    assert 'system_prompt' not in public and 'raw_data' not in public


@pytest.mark.asyncio
async def test_config_changes_do_not_change_existing_snapshot(runtime):
    runs, llm = runtime
    body = request()
    await runs.start('u1', body)
    original = await wait_for(runs, body['run_uuid'])
    runs.agent._config['agent']['system_prompt'] = 'changed'
    runs.agent._config['capabilities']['models'] = {}
    again, created = await runs.start('u1', body)
    assert not created and original['execution_snapshot_digest'] == again['execution_snapshot_digest']
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_configured_video_contract_confirmed_not_requested(settings, monkeypatch):
    from conftest import FakeLLM
    from test_runs import blocking, command

    from ms_agent.agent.api import AgentService
    from ms_agent.agent.run_service import RunService
    from ms_agent.session.run_store import FileRunStore
    from ms_agent.tools.api_adapter import APIToolAdapter
    from service.aigc.settings import ModelCapability

    settings.capabilities.models['mock-video'] = ModelCapability(
        model_id='video-model',
        modality='video',
        tool_name='video_generation',
        actions=['generate'],
        output_type='video',
        parameter_map={'video_duration': 'duration'},
        confirmed_path='actual',
        poster_path='poster',
    )
    settings.tools.api['tools'].append(
        {
            'name': 'video_generation',
            'enabled': True,
            'requires_approval': True,
            'description': 'mock video',
            'api': {
                'method': 'POST',
                'path': '/video',
                'async_mode': True,
                'poll_path': '/task/{task_id}',
                'timeout': 1,
            },
            'parameters': {
                'type': 'object',
                'required': ['model', 'prompt', 'duration'],
                'additionalProperties': False,
                'properties': {
                    'model': {'type': 'string'},
                    'prompt': {'type': 'string'},
                    'duration': {'type': 'integer'},
                },
            },
            'output_mapping': {
                'success_path': 'output_videos',
                'task_id_path': 'task_id',
                'status_path': 'task_status',
            },
        }
    )

    async def transport(self, method, *args, **kwargs):
        if method == 'POST':
            return {'task_id': 'video-task', 'task_status': 'PENDING'}
        return {
            'task_status': 'SUCCEED',
            'output_videos': ['https://example.test/video.mp4'],
            'poster': 'https://example.test/poster.png',
            'actual': {'prompt': 'confirmed prompt', 'model_ref': 'mock-video', 'params': {'duration': 5}},
        }

    monkeypatch.setattr(APIToolAdapter, '_request', transport)
    config = settings.to_agent_config()
    store = FileRunStore(settings.storage.data_dir)
    llm = FakeLLM()
    runs = RunService(
        store,
        AgentService(config, locks=store.locks, llm_client=llm),
        runtime_config=settings.runtime.model_dump(),
        event_config=settings.events.model_dump(),
    )
    await runs.initialize()
    try:
        body = request(custom=True)
        effective = body['input_snapshot']['effective']
        effective['modality'] = 'video'
        effective['custom_selection']['model_ref'] = 'mock-video'
        effective['preferences']['video_duration'] = 5
        await runs.start('u1', body)
        await runs.command('u1', body['run_uuid'], command('approve', await blocking(runs, body['run_uuid'])))
        view = await wait_for(runs, body['run_uuid'])
        output = next(o for o in view['public_snapshot']['outputs'] if o['type'] == 'video')
        assert output['actual_generation']['prompt'] == 'confirmed prompt'
        assert output['generation']['submitted']['prompt'] == '你好'
        assert output['poster_url'].endswith('poster.png') and not llm.calls
        assert {a['action'] for a in output['result_actions']} == {'save_recipe'}
    finally:
        await runs.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['revision', 'variables', 'schema', 'remote-ref', 'size', 'missing', 'recipe'])
async def test_asset_boundaries_fail_closed(runtime, mutation):
    runs, llm = runtime
    body = request()
    content = {
        'instructions': '固定说明',
        'variables_schema': {'type': 'object', 'additionalProperties': False},
    }
    ref = {'type': 'skill', 'resource_ref': 'skill/owned', 'revision': 'r1', 'variables': {}}
    asset = {k: ref[k] for k in ('type', 'resource_ref', 'revision')}
    if mutation == 'revision':
        asset['revision'] = 'r2'
    elif mutation == 'variables':
        ref['variables'] = {'unexpected': 'value'}
    elif mutation == 'schema':
        content['variables_schema']['type'] = 'invalid'
    elif mutation == 'remote-ref':
        content['variables_schema']['$ref'] = 'https://example.invalid/schema'
    elif mutation == 'size':
        runs.agent._config['capabilities']['context']['max_asset_bytes'] = 10
    elif mutation == 'recipe':
        ref['type'] = asset['type'] = 'recipe'
        content = {
            'schema_version': 1,
            'prompt_template': 'x',
            'variables_schema': {'type': 'object'},
            'parameters': {'steps': 30},
        }
    asset.update(content=content, content_digest=digest(content))
    body['input_snapshot'].update(asset_refs=[ref], asset_snapshots=[] if mutation == 'missing' else [asset])
    with pytest.raises(RunError) as error:
        await runs.start('u1', body)
    assert error.value.details['execution_started'] is False and not llm.calls
    assert await runs.store.read_state('u1', 'runs', body['run_uuid']) is None


@pytest.mark.asyncio
async def test_agent_parameter_range_rejected_at_admission(runtime):
    runs, llm = runtime
    runs.agent._tool_registry.parameters('image_generation')['properties']['seed'] = {
        'type': 'integer',
        'minimum': 0,
        'maximum': 10,
    }
    body = request()
    body['input_snapshot']['effective']['preferences']['seed'] = 99
    with pytest.raises(RunError, match='CAPABILITY_NOT_SUPPORTED'):
        await runs.start('u1', body)
    assert not llm.calls


def test_uuid_normalization_and_branch_associations():
    body = request()
    parent = str(uuid4())
    body['parent_message_uuid'] = parent.upper()
    body['input_snapshot']['context'] = [
        {'message_uuid': parent, 'role': 'assistant', 'content': [], 'output_refs': []},
    ]
    assert StartRunRequest.model_validate(body).parent_message_uuid == parent
    body['input_snapshot']['context'].insert(
        0, {**body['input_snapshot']['context'][0], 'message_uuid': parent.upper()}
    )
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate(body)


@pytest.mark.asyncio
async def test_replay_snapshot_suffix_and_full_output_upsert(runtime):
    from ms_agent.agent.protocol import Output

    runs, _ = runtime
    body = request()
    await runs.start('u1', body)
    view = await wait_for(runs, body['run_uuid'])
    events = [e async for e in runs.events('u1', body['run_uuid'])]
    prefix = None
    for event in events[:3]:
        prefix = reduce_public(prefix, None, event)
    for event in events[3:]:
        prefix = reduce_public(prefix, None, event)
    assert prefix == view['public_snapshot']
    async with runs.store.lock('u1', 'runs', body['run_uuid']):
        for text in ('first', 'replacement'):
            output = Output(output_id='stable', type='text', content=text).model_dump()
            await runs.store.commit('u1', 'runs', body['run_uuid'], {}, 'output.upsert', output)
    state, updated = await runs.store.read('u1', 'runs', body['run_uuid'])
    for event in updated[len(events) :]:
        prefix = reduce_public(prefix, None, event)
    assert prefix == state['public_snapshot']
    assert [o['content'] for o in prefix['outputs'] if o['output_id'] == 'stable'] == ['replacement']


@pytest.mark.asyncio
async def test_custom_lora_and_parameters_stay_frozen_through_approval(runtime, monkeypatch):
    from test_runs import blocking, command

    from ms_agent.tools.api_adapter import APIToolAdapter

    runs, llm = runtime
    model = runs.agent._config['capabilities']['models'][MODEL]
    model['parameter_map'].update(loras='loras', sampler='sampler')
    for tool in runs.agent._config['tools']['api']['tools']:
        tool['parameters']['properties'].update(loras={'type': 'object'}, sampler={'enum': ['fixed']})
    schema = runs.agent._tool_registry.parameters('image_generation')
    schema['properties'].update(loras={'type': 'object'}, sampler={'enum': ['fixed']})
    submitted = []

    async def transport(self, method, *args, **kwargs):
        if method == 'POST':
            submitted.append(deepcopy(kwargs['json_body']))
            return {'task_id': 'fixed', 'task_status': 'PENDING'}
        return {'task_status': 'SUCCEED', 'output_images': ['https://example.test/fixed.png']}

    monkeypatch.setattr(APIToolAdapter, '_request', transport)
    body = request(custom=True)
    effective = body['input_snapshot']['effective']
    effective['preferences']['seed'] = 123
    effective['custom_selection'].update(
        loras=[{'model_ref': 'approved-lora', 'weight': 0.75}],
        params={'steps': 20, 'guidance_scale': 4.0, 'sampler': 'fixed'},
    )
    await runs.start('u1', body)
    interaction = await blocking(runs, body['run_uuid'])
    runs.agent._config['capabilities']['models'] = {}
    runs.agent._config['agent']['system_prompt'] = '不能影响已受理 Run'
    await runs.command('u1', body['run_uuid'], command('approve', interaction))
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'succeeded' and not llm.calls
    assert submitted[0] == {
        'model': MODEL,
        'prompt': '你好',
        'n': 1,
        'seed': 123,
        'steps': 20,
        'guidance': 4.0,
        'sampler': 'fixed',
        'loras': {'approved-lora': 0.75},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('remote_status', ['SUCCEED', 'FAILED'])
async def test_partial_completion_retains_confirmed_deliveries(runtime, monkeypatch, remote_status):
    from test_runs import blocking, command

    from ms_agent.tools.api_adapter import APIToolAdapter

    runs, _ = runtime

    async def transport(self, method, *args, **kwargs):
        if method == 'POST':
            return {'task_id': 'batch', 'task_status': 'PENDING'}
        return {'task_status': remote_status, 'output_images': ['https://example.test/delivered.png']}

    monkeypatch.setattr(APIToolAdapter, '_request', transport)
    body = request(custom=True)
    body['input_snapshot']['effective']['preferences']['image_count'] = 2
    await runs.start('u1', body)
    await runs.command('u1', body['run_uuid'], command('approve', await blocking(runs, body['run_uuid'])))
    view = await wait_for(runs, body['run_uuid'])
    assert view['status'] == 'partial_succeeded' and view['can_release_activity']
    assert len(view['completion']['completed_items']) == len(view['completion']['incomplete_items']) == 1
    assert next(o for o in view['public_snapshot']['outputs'] if o['type'] == 'image')['actual_generation'] is None
