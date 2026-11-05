"""请求级控制端口；状态与命令回执由 RunService 持久化。"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError


class RunControl:
    def __init__(self, record: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self.record = record
        self.pending: dict[str, Any] | None = None
        self.answer: asyncio.Future | None = None
        self.interventions: list[tuple[str, str]] = []

    async def boundary(self, messages: list[dict[str, Any]]) -> None:
        while self.interventions:
            command_id, content = self.interventions.pop(0)
            messages.append({'role': 'user', 'content': content})
            await self.record('command_applied', {'command_id': command_id})

    async def _wait(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        self.answer = asyncio.get_running_loop().create_future()
        self.pending = {'interaction_id': str(uuid4()), 'kind': kind, **data}
        try:
            await self.record('interaction_requested', self.pending)
            command = await self.answer
            await self.record('command_applied', {'command_id': command['command_id'], 'clear_interaction': True})
            return command
        finally:
            self.pending = None
            self.answer = None

    async def prepare_tool(
        self,
        invocation_id: str,
        name: str,
        args: dict[str, Any],
        schema: dict[str, Any],
        approval: bool,
    ) -> dict[str, Any] | None:
        validator = Draft202012Validator(schema)
        try:
            validator.validate(args)
        except ValidationError:
            command = await self._wait(
                'input',
                {
                    'invocation_id': invocation_id,
                    'tool_name': name,
                    'arguments': args,
                    'input_schema': schema,
                },
            )
            if command['type'] == 'skip':
                return None
            args = {**args, **command['payload']}
            validator.validate(args)
        if approval:
            command = await self._wait(
                'approval',
                {
                    'invocation_id': invocation_id,
                    'tool_name': name,
                    'arguments': args,
                },
            )
            if command['type'] == 'skip':
                return None
        return args

    def deliver(self, command: dict[str, Any]) -> None:
        if command['type'] == 'intervene':
            self.interventions.append((command['command_id'], command['payload']['content']))
        elif self.answer is not None and not self.answer.done():
            self.answer.set_result(command)
