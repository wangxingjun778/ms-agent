"""联调合同测试资源；不访问收费模型或推理平台。"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_DNS, uuid4, uuid5

import pytest
import pytest_asyncio

from ms_agent.agent.api import AgentService
from ms_agent.agent.protocol import StartRunRequest
from ms_agent.agent.run_service import RunService
from ms_agent.session.run_store import FileRunStore
from service.aigc.settings import ServiceCredentials, Settings

MODEL = 'Qwen/Qwen-Image-2.1-Turbo'


class FakeLLM:
    def __init__(self, messages=None, delay=0):
        self.messages = messages or [{'role': 'assistant', 'content': '完成'}]
        self.delay, self.calls, self.closed = delay, [], False

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append(deepcopy(messages))
        await asyncio.sleep(self.delay)
        return {
            'message': deepcopy(self.messages[min(len(self.calls) - 1, len(self.messages) - 1)]),
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        }

    async def close(self):
        self.closed = True


def request(run_uuid=None, session_id='s1', content='你好', *, custom=False, deadline=None):
    sid = str(uuid5(NAMESPACE_DNS, session_id))
    parent = str(uuid5(NAMESPACE_DNS, session_id + ':root'))
    body = {
        'run_uuid': run_uuid or str(uuid4()),
        'session_id': sid,
        'user_message_uuid': str(uuid4()),
        'assistant_message_uuid': str(uuid4()),
        'parent_message_uuid': parent,
        'execution_policy': {
            'deadline_at': deadline or (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        },
        'input_snapshot': {
            'effective': {
                'prompt': content,
                'generation_mode': 'custom' if custom else 'agent',
                'custom_selection': {'model_ref': MODEL} if custom else None,
            }
        },
    }
    return StartRunRequest.model_validate(body).sdk_input()


async def wait_for(runs, identifier, status=None, user='u1'):
    async with asyncio.timeout(5):
        while True:
            view = await runs.get(user, identifier)
            if view['status'] == status if status else view['execution_closed']:
                return view
            await asyncio.sleep(0.002)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for key in ('AIGC_CONFIG', 'API_TOOLS_CONFIG', 'AIGC_QUEUE_LIMIT', 'AIGC_EAS_ENABLED', 'AIGC_EAS_GATEWAY'):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.load(deployment=True)
    settings.storage.data_dir = str(tmp_path)
    settings.runtime.max_active_runs = 4
    settings.runtime.max_inflight_runs = 104
    settings.runtime.per_user_inflight = 104
    settings.runtime.interaction_timeout = 0
    settings.runtime.drain_timeout = 0
    settings.settlement.budget = 0.2
    settings.settlement.retry_interval = 0.005
    settings.settlement.query_timeout = 0.05
    return settings.bind_credentials(
        ServiceCredentials(service='test-service-secret', llm='test-model-secret', api='test-image-secret')
    )


@pytest.fixture
def config(settings):
    return settings.to_agent_config()


@pytest_asyncio.fixture
async def runtime(tmp_path, config, settings):
    llm = FakeLLM()
    store = FileRunStore(tmp_path)
    runs = RunService(
        store,
        AgentService(config, locks=store.locks, llm_client=llm),
        runtime_config=settings.runtime.model_dump(),
        event_config=settings.events.model_dump(),
    )
    await runs.initialize()
    try:
        yield runs, llm
    finally:
        await runs.close()
