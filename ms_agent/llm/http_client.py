"""可取消的 OpenAI-compatible 客户端，服务模式共享限流与有界重试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from ms_agent.utils.limiter import retry_delay


class HTTPLLMClient:
    def __init__(self, config: dict[str, Any], session: aiohttp.ClientSession | None = None, *, limiter=None) -> None:
        self.config = dict(config)
        self._session = session
        self._owns_session = session is None
        self.limiter = limiter

    @asynccontextmanager
    async def _slot(self):
        if self.limiter:
            async with self.limiter:
                yield
        else:
            yield

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        clean = [
            {
                k: v
                for k, v in message.items()
                if k
                in {
                    'role',
                    'content',
                    'tool_calls',
                    'tool_call_id',
                    'name',
                }
            }
            for message in messages
        ]
        payload: dict[str, Any] = {'model': self.config['model'], 'messages': clean}
        for key in ('temperature', 'top_p', 'max_tokens'):
            if key in self.config:
                payload[key] = self.config[key]
        if tools:
            payload['tools'] = tools
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        headers = {'Content-Type': 'application/json'}
        if self.config.get('api_key'):
            headers['Authorization'] = f'Bearer {self.config["api_key"]}'
        options = self.config.get('upstream', {})
        policy = options.get(
            'retry', {'attempts': 1, 'budget': float(self.config.get('timeout', 300)), 'initial': 1, 'maximum': 30}
        )
        timeout = aiohttp.ClientTimeout(
            total=float(self.config.get('timeout', 300)),
            connect=options.get('connect_timeout'),
            sock_read=options.get('read_timeout'),
        )
        url = self.config['base_url'].rstrip('/') + '/chat/completions'

        async def execute():
            for attempt in range(policy['attempts']):
                retry_headers = {}
                try:
                    async with self._slot():
                        async with self._session.post(url, headers=headers, json=payload, timeout=timeout) as response:
                            if response.status == 200:
                                return await response.json()
                            status = response.status
                            retry_headers = dict(response.headers)
                            await response.read()
                            if status not in (429, 502, 503, 504) or attempt + 1 == policy['attempts']:
                                raise RuntimeError(f'LLM_HTTP_{status}')
                            if self.limiter:
                                self.limiter.throttles += 1
                except aiohttp.ClientConnectorError:
                    if attempt + 1 == policy['attempts']:
                        raise
                await asyncio.sleep(retry_delay(retry_headers, attempt, policy))
            raise RuntimeError('LLM_RETRY_EXHAUSTED')

        body = await asyncio.wait_for(execute(), policy['budget'])
        choices = body.get('choices') or []
        if not choices or not isinstance(choices[0].get('message'), dict):
            raise ValueError('LLM 响应缺少 message')
        return {'message': choices[0]['message'], 'usage': body.get('usage') or {}}

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None
