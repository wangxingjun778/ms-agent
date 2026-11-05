"""可取消、无自动重试的 OpenAI-compatible HTTP 客户端。"""

from __future__ import annotations

from typing import Any

import aiohttp


class HTTPLLMClient:
    def __init__(self, config: dict[str, Any], session: aiohttp.ClientSession | None = None) -> None:
        self.config = dict(config)
        self._session = session
        self._owns_session = session is None

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
        timeout = aiohttp.ClientTimeout(total=float(self.config.get('timeout', 300)))
        url = self.config['base_url'].rstrip('/') + '/chat/completions'
        async with self._session.post(url, headers=headers, json=payload, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f'LLM_HTTP_{response.status}')
            body = await response.json()
        choices = body.get('choices') or []
        if not choices or not isinstance(choices[0].get('message'), dict):
            raise ValueError('LLM 响应缺少 message')
        return {'message': choices[0]['message'], 'usage': body.get('usage') or {}}

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None
