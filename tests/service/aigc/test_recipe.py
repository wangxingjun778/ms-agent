"""配方生成、事实约束、幂等与中断。"""

import asyncio
import json

import pytest
from conftest import FakeLLM, request, wait_for

from ms_agent.agent.recipe import RecipeService
from ms_agent.agent.run_service import RunError

pytestmark = pytest.mark.asyncio


def generation():
    return {
        'prompt_template': '生成 {{subject}}',
        'variables': [{'name': 'subject', 'type': 'string', 'description': '主题', 'default': '猫'}],
        'examples': [{'description': '示例', 'inputs': {'subject': '狗'}}],
    }


async def wait_recipe(builder, identifier):
    async with asyncio.timeout(5):
        while True:
            state, _ = await builder.store.read('u1', 'recipes', identifier)
            if state['status'] in {'succeeded', 'failed', 'interrupted'}:
                return state
            await asyncio.sleep(0.01)


async def test_recipe_generation_idempotent_and_conflict(runtime):
    runs, _ = runtime
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'])
    llm = FakeLLM([{'role': 'assistant', 'content': json.dumps(generation(), ensure_ascii=False)}], delay=0.02)
    builder = RecipeService(runs, llm)
    recipe_request = {'recipe_request_id': 'recipe1', 'artifact_ids': ['final_text'], 'context': '配方上下文'}
    try:
        outcomes = await asyncio.gather(
            *(builder.get_or_build('u1', body['run_uuid'], recipe_request) for _ in range(8))
        )
        assert sum(created for _, created in outcomes) == 1
        state = await wait_recipe(builder, 'recipe1')
        assert state['status'] == 'succeeded'
        assert state['result']['source']['run_uuid'] == body['run_uuid']
        assert state['result']['dependencies'] == [{'type': 'llm', 'model': 'test'}]
        cached, created = await builder.get_or_build('u1', body['run_uuid'], recipe_request)
        assert not created and cached['result'] == state['result'] and len(llm.calls) == 1
        with pytest.raises(RunError, match='RECIPE_INPUT_CONFLICT'):
            await builder.get_or_build('u1', body['run_uuid'], {**recipe_request, 'context': 'changed'})
    finally:
        await builder.close()


@pytest.mark.parametrize('content', ['not json', json.dumps({**generation(), 'fake_tool': 'dangerous'})])
async def test_invalid_llm_output_is_persisted_failure(runtime, content):
    runs, _ = runtime
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'])
    llm = FakeLLM([{'role': 'assistant', 'content': content}])
    builder = RecipeService(runs, llm)
    input_data = {'recipe_request_id': 'bad1', 'artifact_ids': ['final_text'], 'context': ''}
    try:
        await builder.get_or_build('u1', body['run_uuid'], input_data)
        state = await wait_recipe(builder, 'bad1')
        assert state['status'] == 'failed' and not state['result']
        await builder.get_or_build('u1', body['run_uuid'], input_data)
        assert len(llm.calls) == 1
        assert (await runs.get('u1', body['run_uuid']))['status'] == 'succeeded'
    finally:
        await builder.close()


async def test_recipe_timeout_and_shutdown(runtime):
    runs, _ = runtime
    body = request()
    await runs.start('u1', body)
    await wait_for(runs, body['run_uuid'])
    builder = RecipeService(runs, FakeLLM(delay=10), timeout=0.01)
    try:
        await builder.get_or_build(
            'u1',
            body['run_uuid'],
            {
                'recipe_request_id': 'timeout1',
                'artifact_ids': ['final_text'],
                'context': '',
            },
        )
        assert (await wait_recipe(builder, 'timeout1'))['status'] == 'failed'
    finally:
        await builder.close()
    builder = RecipeService(runs, FakeLLM(delay=10))
    await builder.get_or_build(
        'u1',
        body['run_uuid'],
        {
            'recipe_request_id': 'interrupted1',
            'artifact_ids': ['final_text'],
            'context': '',
        },
    )
    await asyncio.sleep(0.01)
    await builder.close()
    assert (await wait_recipe(builder, 'interrupted1'))['status'] == 'interrupted'


async def test_fake_variable_binding_rejected():
    output = generation()
    output['variables'][0]['target'] = {'invocation_id': 'fake', 'parameter': 'model'}
    with pytest.raises(ValueError, match='不存在'):
        RecipeService.validate_generation(output, {})
