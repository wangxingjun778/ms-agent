"""交互控制端口：一个阻断交互与一个非阻断干预，所有决策先持久化。"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError


class InteractionTimeout(RuntimeError):
    """交互期限耗尽，不自动批准。"""


class RunControl:
    def __init__(self, record, *, permit=None, timeout=0, guard=None):
        self.record, self.permit, self.timeout = record, permit, timeout
        self._guard = guard
        self.pending = None
        self.answer = None
        self.interventions = []
        self.nonblocking = None

    async def guard(self):
        if self._guard:
            await self._guard()

    async def open_intervention(self):
        if self.nonblocking:
            return
        identifier = 'i-' + uuid4().hex
        self.nonblocking = {
            'interaction_id': identifier,
            'type': 'intervention',
            'blocking': False,
            'title': '调整后续规划',
            'step_id': None,
            'arguments': None,
            'actions': [
                {
                    'action_id': identifier + '-intervene',
                    'action': 'intervene',
                    'label': '补充要求',
                    'input_schema': {
                        'type': 'object',
                        'required': ['text'],
                        'properties': {'text': {'type': 'string', 'minLength': 1, 'maxLength': 20000}},
                        'additionalProperties': False,
                    },
                }
            ],
        }
        await self.record('interaction_requested', self.nonblocking)

    async def boundary(self, messages):
        await self.guard()
        while self.interventions:
            command = self.interventions.pop(0)
            messages.append({'role': 'user', 'content': command['input']['text']})
            await self.record('command_applied', {'command_id': command['command_id']})
        await self.open_intervention()

    async def _wait(self, kind, data):
        self.answer = asyncio.get_running_loop().create_future()
        identifier = 'i-' + uuid4().hex
        actions = [{'action_id': identifier + '-skip', 'action': 'skip', 'label': '跳过', 'input_schema': None}]
        if kind == 'permission':
            actions.insert(
                0,
                {'action_id': identifier + '-approve', 'action': 'approve', 'label': '允许本次', 'input_schema': None},
            )
        else:
            schema = data.pop('interaction_schema', None)
            if schema is None:
                schema = {
                    'type': 'object',
                    'properties': {'values': data.pop('input_schema')},
                    'required': ['values'],
                    'additionalProperties': False,
                }
            actions.insert(
                0,
                {
                    'action_id': identifier + '-input',
                    'action': 'submit_input',
                    'label': '补充输入',
                    'input_schema': schema,
                },
            )
        self.pending = {
            'interaction_id': identifier,
            'type': kind,
            'title': '确认本次工具调用',
            'blocking': True,
            'actions': actions,
            'step_id': data['invocation_id'],
            'arguments': data['arguments'],
        }
        try:
            await self.record('interaction_requested', self.pending)
            if self.permit:
                self.permit.release()
            try:
                command = await asyncio.wait_for(self.answer, self.timeout) if self.timeout else await self.answer
            except asyncio.TimeoutError as exc:
                raise InteractionTimeout('等待交互超时') from exc
            if self.permit:
                await self.permit.resume()
            await self.guard()
            await self.record('command_applied', {'command_id': command['command_id']})
            await self.record('interaction_resolved', {'interaction_id': identifier, 'status': 'RESOLVED'})
            return command
        finally:
            self.pending, self.answer = None, None

    async def ask_input(self, invocation_id, question):
        return await self._wait(
            'input',
            {
                'invocation_id': invocation_id,
                'arguments': {'question': question},
                'interaction_schema': {
                    'type': 'object',
                    'required': ['text'],
                    'properties': {'text': {'type': 'string', 'minLength': 1, 'maxLength': 20000}},
                    'additionalProperties': False,
                },
            },
        )

    async def prepare_tool(self, invocation_id, name, args, schema, approval):
        validator = Draft202012Validator(schema)
        try:
            validator.validate(args)
        except ValidationError:
            command = await self._wait(
                'input', {'invocation_id': invocation_id, 'arguments': args, 'input_schema': schema}
            )
            if command['action'] == 'skip':
                return None
            args = {**args, **command['input']['values']}
            validator.validate(args)
        if approval:
            command = await self._wait('permission', {'invocation_id': invocation_id, 'arguments': args})
            if command['action'] == 'skip':
                return None
        return args

    def deliver(self, command):
        if command['action'] == 'intervene':
            self.interventions.append(command)
            self.nonblocking = None
        elif self.answer and not self.answer.done():
            self.answer.set_result(command)
