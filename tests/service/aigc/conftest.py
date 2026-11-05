"""不访问收费接口的服务测试资源。"""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
import pytest_asyncio

from ms_agent.agent.api import AgentService
from ms_agent.agent.run_service import RunService
from ms_agent.session.run_store import FileRunStore
from service.aigc.settings import Settings


class FakeLLM:
    def __init__(self, messages=None, delay=0):
        self.messages = messages or [{'role': 'assistant', 'content': '完成'}]
        self.delay = delay
        self.calls = []
        self.closed = False

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append(deepcopy(messages))
        await asyncio.sleep(self.delay)
        return {
            'message': deepcopy(self.messages[min(len(self.calls) - 1, len(self.messages) - 1)]),
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        }

    async def close(self):
        self.closed = True


def request(run_uuid=None, session_id='s1', content='你好'):
    return {
        'run_uuid': run_uuid or str(uuid4()),
        'session_id': session_id,
        'profile_id': 'aigc_assistant',
        'messages': [{'role': 'user', 'content': content, 'attachments': []}],
        'parameters': {},
    }


async def wait_for(runs, identifier, status=None):
    async with asyncio.timeout(5):
        while True:
            view = await runs.get('u1', identifier)
            if (
                view['status'] == status
                if status
                else view['status'] in {'succeeded', 'failed', 'cancelled', 'interrupted'}
            ):
                return view
            await asyncio.sleep(0.01)


@pytest.fixture
def config(tmp_path):
    return {
        'strict': True,
        'agent': {'mode': 'lightweight', 'max_chat_round': 5},
        'tools': {'builtin_enabled': False},
        'skills': {'enabled': False},
        'session': {'base_dir': str(tmp_path)},
        'memory': {'base_dir': str(tmp_path), 'scope': 'session', 'enabled': True},
        'llm': {
            'transport': 'http',
            'model': 'test',
            'api_key': 'test-model-secret',
            'base_url': 'http://unused.invalid/v1',
        },
    }


@pytest.fixture
def settings(tmp_path, config):
    return Settings(data_dir=str(tmp_path), service_token='test-service-secret', agent_config=config)


@pytest_asyncio.fixture
async def runtime(tmp_path, config):
    llm = FakeLLM()
    store = FileRunStore(tmp_path)
    runs = RunService(store, AgentService(config, locks=store.locks, llm_client=llm))
    await runs.initialize()
    try:
        yield runs, llm
    finally:
        await runs.close()
