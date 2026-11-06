"""Recipe 固定事实、显式重试和 CAS，不混入原 Run 事件。"""

import asyncio
import json
from copy import deepcopy

import pytest
from conftest import FakeLLM, request, wait_for

from ms_agent.agent.recipe import RecipeService
from ms_agent.agent.run_service import RunError

pytestmark = pytest.mark.asyncio
GENERATION = {
    'prompt_template': '生成 {{subject}}',
    'negative_prompt_template': '',
    'variables': [{'name': 'subject', 'type': 'string', 'required': True, 'description': '主题'}],
    'examples': [{'description': '示例', 'inputs': {'subject': '猫'}}],
}


async def finish(builder, run_id, payload):
    async with asyncio.timeout(3):
        while True:
            view, _ = await builder.get_or_build('u1', run_id, payload)
            if view['status'] not in ('queued', 'running'):
                return view
            await asyncio.sleep(0.002)


async def test_recipe_idempotent_and_versions(runtime):
    runs, _ = runtime
    body = request()
    await runs.start('u1', body)
    source = await wait_for(runs, body['run_uuid'])
    llm = FakeLLM([{'content': json.dumps(GENERATION)}])
    builder = RecipeService(runs, llm)
    payload = {'recipe_request_id': 'r1', 'output_ids': ['final_text'], 'context': '固定用途'}
    try:
        first, created = await builder.get_or_build('u1', body['run_uuid'], payload)
        assert created and first['attempt'] == 1
        view = await finish(builder, body['run_uuid'], payload)
        assert view['status'] == 'succeeded' and view['schema_version'] == view['result']['schema_version'] == 1
        assert view['result']['provenance']['template_version'] == '1'
        assert view['result']['content_revision'] == 1 and len(llm.calls) == 1
        assert (await runs.get('u1', body['run_uuid']))['last_agent_seq'] == source['last_agent_seq']
        with pytest.raises(RunError, match='RECIPE_INPUT_CONFLICT'):
            await builder.get_or_build('u1', body['run_uuid'], {**payload, 'context': 'different'})
    finally:
        await builder.close()


async def test_recipe_failed_query_does_not_retry_and_cas(runtime):
    runs, _ = runtime
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'])
    llm = FakeLLM([{'content': 'invalid json'}, {'content': json.dumps(GENERATION)}], delay=0.02)
    builder = RecipeService(runs, llm)
    base = {'recipe_request_id': 'r1', 'output_ids': ['final_text']}
    try:
        await builder.get_or_build('u1', body['run_uuid'], base)
        failed = await finish(builder, body['run_uuid'], base)
        assert failed['status'] == 'failed'
        assert (await builder.get_or_build('u1', body['run_uuid'], base))[0]['attempt'] == 1 and len(llm.calls) == 1
        retry = {**base, 'retry_command_id': 'retry1', 'expected_attempt': 1}
        outcomes = await asyncio.gather(*(builder.get_or_build('u1', body['run_uuid'], retry) for _ in range(8)))
        assert sum(created for _, created in outcomes) == 1
        with pytest.raises(RunError, match='RECIPE_ATTEMPT_CONFLICT'):
            await builder.get_or_build('u1', body['run_uuid'], {**retry, 'retry_command_id': 'retry2'})
        succeeded = await finish(builder, body['run_uuid'], base)
        assert succeeded['attempt'] == 2 and succeeded['status'] == 'succeeded'
        assert len(llm.calls) == 2 and succeeded['attempts'][0]['status'] == 'failed'
        assert (await builder.get_or_build('u1', body['run_uuid'], retry))[0]['attempt'] == 2
        with pytest.raises(RunError, match='RECIPE_RETRY_CONFLICT'):
            await builder.get_or_build('u1', body['run_uuid'], {**retry, 'expected_attempt': 2})
        # 每次 attempt 复用第一次冻结的输入事实，而非最新 context。
        assert llm.calls[0] == llm.calls[1]
    finally:
        await builder.close()


async def test_recipe_unknown_source_never_builds(runtime):
    runs, _ = runtime
    builder = RecipeService(runs, FakeLLM())
    try:
        with pytest.raises(RunError, match='RUN_NOT_FOUND'):
            await builder.get_or_build('u1', request()['run_uuid'], {'recipe_request_id': 'r1', 'output_ids': ['x']})
    finally:
        await builder.close()


async def test_recipe_validation_required_target_and_negative_template():
    bad = deepcopy(GENERATION)
    bad['negative_prompt_template'] = '{{unknown}}'
    with pytest.raises(ValueError):
        RecipeService.validate_generation(bad, {})
    bad = deepcopy(GENERATION)
    bad['examples'][0]['inputs'] = {}
    with pytest.raises(ValueError):
        RecipeService.validate_generation(bad, {})
    bad = deepcopy(GENERATION)
    bad['variables'][0]['target'] = {'invocation_id': 'x', 'parameter': 'unknown'}
    with pytest.raises(ValueError):
        RecipeService.validate_generation(bad, {})


async def test_different_retry_commands_compete_and_old_receipt_is_stable(runtime):
    runs, _ = runtime
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'])
    llm = FakeLLM([{'content': 'bad'}, {'content': 'bad'}, {'content': json.dumps(GENERATION)}], delay=0.02)
    builder = RecipeService(runs, llm)
    base = {'recipe_request_id': 'race', 'output_ids': ['final_text']}
    try:
        await builder.get_or_build('u1', body['run_uuid'], base)
        await finish(builder, body['run_uuid'], base)
        retries = [{**base, 'retry_command_id': f'retry-{i}', 'expected_attempt': 1} for i in range(16)]
        results = await asyncio.gather(
            *(builder.get_or_build('u1', body['run_uuid'], retry) for retry in retries), return_exceptions=True
        )
        winners = [i for i, result in enumerate(results) if not isinstance(result, Exception)]
        assert len(winners) == 1 and all(
            isinstance(result, RunError) and result.code == 'RECIPE_ATTEMPT_CONFLICT'
            for i, result in enumerate(results)
            if i not in winners
        )
        await finish(builder, body['run_uuid'], base)
        third = {**base, 'retry_command_id': 'third', 'expected_attempt': 2}
        await builder.get_or_build('u1', body['run_uuid'], third)
        success = await finish(builder, body['run_uuid'], base)
        assert success['status'] == 'succeeded' and success['attempt'] == 3
        old, created = await builder.get_or_build('u1', body['run_uuid'], retries[winners[0]])
        assert old['attempt'] == 2 and old['status'] == 'failed' and not created
        assert len(llm.calls) == 3 and llm.calls[0] == llm.calls[1] == llm.calls[2]
    finally:
        await builder.close()


async def test_recipe_parameters_only_from_selected_output_facts(runtime, monkeypatch):
    from test_runs import blocking, command

    from ms_agent.tools.api_adapter import APIToolAdapter

    runs, _ = runtime

    async def transport(self, method, *args, **kwargs):
        if method == 'POST':
            return {'task_id': 'one', 'task_status': 'PENDING'}
        return {'task_status': 'SUCCEED', 'output_images': ['https://example.test/one.png']}

    monkeypatch.setattr(APIToolAdapter, '_request', transport)
    body = request(custom=True)
    body['input_snapshot']['effective']['preferences']['seed'] = 42
    await runs.start('u1', body)
    await runs.command('u1', body['run_uuid'], command('approve', await blocking(runs, body['run_uuid'])))
    source = await wait_for(runs, body['run_uuid'])
    output = next(o for o in source['public_snapshot']['outputs'] if o['type'] == 'image')
    llm = FakeLLM([{'content': json.dumps(GENERATION)}])
    builder = RecipeService(runs, llm)
    base = {'recipe_request_id': 'facts', 'output_ids': [output['output_id']]}
    try:
        await builder.get_or_build('u1', body['run_uuid'], base)
        view = await finish(builder, body['run_uuid'], base)
        assert view['status'] == 'succeeded'
        result = view['result']
        assert result['parameters']['confirmed'] == {output['output_id']: None}
        assert next(iter(result['parameters']['submitted'].values()))['seed'] == 42
        assert all(len(d['content_digest']) == 64 for d in result['dependencies'])
        assert result['source']['assistant_message_uuid'] == body['assistant_message_uuid']
        text_base = {'recipe_request_id': 'text-only', 'output_ids': ['final_text']}
        await builder.get_or_build('u1', body['run_uuid'], text_base)
        text = await finish(builder, body['run_uuid'], text_base)
        assert text['result']['parameters']['submitted'] == {}
    finally:
        await builder.close()


async def test_interrupted_recipe_requires_explicit_retry(runtime):
    runs, _ = runtime
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'])
    builder = RecipeService(runs, FakeLLM(delay=10))
    base = {'recipe_request_id': 'interrupted', 'output_ids': ['final_text']}
    await builder.get_or_build('u1', body['run_uuid'], base)
    await asyncio.sleep(0.02)
    await builder.close()
    next_llm = FakeLLM([{'content': json.dumps(GENERATION)}])
    builder = RecipeService(runs, next_llm)
    try:
        view, created = await builder.get_or_build('u1', body['run_uuid'], base)
        assert view['status'] == 'interrupted' and not created and not next_llm.calls
        await builder.get_or_build(
            'u1', body['run_uuid'], {**base, 'retry_command_id': 'resume', 'expected_attempt': 1}
        )
        assert (await finish(builder, body['run_uuid'], base))['status'] == 'succeeded'
    finally:
        await builder.close()
