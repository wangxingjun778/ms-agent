"""轻量 Run 生命周期，持久幂等与交互控制；不依赖 HTTP 框架。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, AsyncIterator

from jsonschema import Draft202012Validator, ValidationError

from ms_agent.agent.api import AgentService
from ms_agent.agent.control import RunControl
from ms_agent.agent.schema import AgentRequest, AgentResponse
from ms_agent.session.locking import PersistenceError, validate_id
from ms_agent.session.run_store import TERMINAL, FileRunStore, digest, now, without_secrets
from ms_agent.tools.schema import ExternalTaskUnresolved


class RunError(Exception):
    def __init__(self, code: str, status: int = 409, details: Any = None) -> None:
        super().__init__(code)
        self.code, self.status, self.details = code, status, details


class RunService:
    def __init__(
        self,
        store: FileRunStore,
        agent: AgentService,
        *,
        profile_id: str = 'aigc_assistant',
        max_runs: int = 4,
        queue_limit: int = 100,
    ) -> None:
        self.store, self.agent, self.profile_id = store, agent, profile_id
        self.max_runs, self.queue_limit = max_runs, queue_limit
        self._semaphore = asyncio.Semaphore(max_runs)
        self._capacity = asyncio.Lock()
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._tails: dict[tuple[str, str], asyncio.Task] = {}
        self._controls: dict[tuple[str, str], RunControl] = {}
        self._operations: set[asyncio.Task] = set()
        self.accepting = False

    async def initialize(self) -> None:
        self.store.lease.acquire()
        try:
            await self.store.recover()
            await self.agent.initialize()
        except BaseException:
            await self.agent.shutdown()
            self.store.lease.release()
            raise
        self.accepting = True

    async def owned(self, coroutine):
        """请求断开不撤销已受理事务，由服务负责等候和关闭。"""
        task = asyncio.create_task(coroutine)
        self._operations.add(task)

        def done(completed):
            self._operations.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def start(self, user_id: str, request: dict[str, Any]) -> tuple[dict, bool]:
        return await self.owned(self._start(user_id, deepcopy(request)))

    async def _start(self, user_id: str, request: dict[str, Any]) -> tuple[dict, bool]:
        run_uuid = validate_id(request['run_uuid'])
        validate_id(user_id, 'user_id')
        validate_id(request['session_id'], 'session_id')
        request_digest = digest(request)
        async with self._capacity, self.store.lock(user_id, 'runs', run_uuid):
            existing, _ = await self.store.read(user_id, 'runs', run_uuid)
            if existing and existing.get('request_digest'):
                if existing['request_digest'] != request_digest:
                    raise RunError('RUN_INPUT_CONFLICT')
                return self.view(existing), False
            if not self.accepting or not self.store.healthy:
                raise RunError('SERVICE_UNAVAILABLE', 503)
            if request['profile_id'] != self.profile_id:
                raise RunError('PROFILE_NOT_FOUND', 404)
            if len(self._tasks) >= self.max_runs + self.queue_limit and not existing:
                raise RunError('RUN_QUEUE_FULL', 429)
            registry = self.agent._tool_registry
            snapshot = without_secrets(
                {
                    'request': request,
                    'profile_id': self.profile_id,
                    'config': self.agent._config,
                    'tools': registry.get_tool_specs(),
                    'skills': self.agent._skill_service.get_active_skills() if self.agent._skill_service else [],
                }
            )
            await self.store.snapshot(user_id, 'runs', run_uuid, 'input_snapshot.json', snapshot)
            cancelled_before_start = bool(existing and existing['status'] == 'cancelled')
            status = 'cancelled' if cancelled_before_start else 'queued'
            state = await self.store.commit(
                user_id,
                'runs',
                run_uuid,
                {
                    'run_uuid': run_uuid,
                    'user_id': user_id,
                    'session_id': request['session_id'],
                    'profile_id': self.profile_id,
                    'status': status,
                    'request_digest': request_digest,
                    'snapshot_digest': digest(snapshot),
                    'created_at': existing.get('created_at') if existing else now(),
                    'pending_interaction': None,
                    'result': None,
                    'artifacts': [],
                    'external_tasks': [],
                    'tool_records': [],
                    'commands': existing.get('commands', {}) if existing else {},
                    'error': None,
                    'requires_reconciliation': False,
                },
                'run_cancelled' if cancelled_before_start else 'run_accepted',
                {'status': status},
            )
            if status == 'queued':
                session_key = (user_id, request['session_id'])
                predecessor = self._tails.get(session_key)
                task = asyncio.create_task(self._execute(user_id, request, predecessor))
                self._tasks[(user_id, run_uuid)] = task
                self._tails[session_key] = task
                task.add_done_callback(lambda t: self._finished(user_id, run_uuid, session_key, t))
            return self.view(state), True

    def _finished(self, user_id, run_uuid, session_key, task) -> None:
        self._tasks.pop((user_id, run_uuid), None)
        self._controls.pop((user_id, run_uuid), None)
        if self._tails.get(session_key) is task:
            self._tails.pop(session_key, None)
        if not task.cancelled():
            task.exception()

    async def get(self, user_id: str, run_uuid: str) -> dict[str, Any]:
        async with self.store.lock(user_id, 'runs', run_uuid):
            state, _ = await self.store.read(user_id, 'runs', run_uuid)
            if state is None:
                raise RunError('RUN_NOT_FOUND', 404)
            return self.view(state)

    @staticmethod
    def view(state: dict[str, Any]) -> dict[str, Any]:
        fields = {
            'run_uuid',
            'session_id',
            'profile_id',
            'status',
            'request_digest',
            'snapshot_digest',
            'last_agent_seq',
            'pending_interaction',
            'result',
            'artifacts',
            'error',
            'external_tasks',
            'commands',
            'requires_reconciliation',
            'created_at',
            'updated_at',
        }
        return {k: deepcopy(v) for k, v in state.items() if k in fields}

    async def record(self, user_id: str, run_uuid: str, event_type: str, data: dict[str, Any]) -> None:
        async with self.store.lock(user_id, 'runs', run_uuid):
            state, _ = await self.store.read(user_id, 'runs', run_uuid)
            if state is None or state['status'] in TERMINAL:
                return
            if state['status'] == 'stopping' and event_type not in {
                'task_submitted',
                'delivery_unknown',
                'tool_completed',
            }:
                raise asyncio.CancelledError
            updates: dict[str, Any] = {}
            public_type, public_data = event_type, data
            if event_type == 'execution_context':
                await self.store.snapshot(user_id, 'runs', run_uuid, 'execution_context.json', data)
                return
            if event_type == 'interaction_requested':
                updates.update(status='waiting_input', pending_interaction=data)
            elif event_type == 'command_applied':
                commands = deepcopy(state['commands'])
                command_id = data['command_id']
                commands[command_id].update(
                    status='applied', reason_code='APPLIED', applied_agent_seq=state.get('last_agent_seq', 0) + 1
                )
                updates['commands'] = commands
                if data.get('clear_interaction'):
                    updates.update(status='running', pending_interaction=None)
                public_data = commands[command_id]
                public_type = 'command_receipt'
            elif event_type in {'task_submitted', 'delivery_unknown'}:
                tasks = deepcopy(state['external_tasks'])
                tasks.append({**data, 'requires_reconciliation': True})
                updates.update(external_tasks=tasks, requires_reconciliation=True)
            elif event_type == 'tool_completed':
                records = state['tool_records'] + [data]
                updates['tool_records'] = records
                tasks = deepcopy(state['external_tasks'])
                for task in tasks:
                    if task.get('invocation_id') == data['invocation_id'] and data['result']['success']:
                        task.update(task_status='SUCCEED', requires_reconciliation=False)
                updates['external_tasks'] = tasks
                updates['requires_reconciliation'] = any(t['requires_reconciliation'] for t in tasks)
                result = data['result']
                raw = result.get('raw_data') or {}
                artifacts = deepcopy(state['artifacts'])
                if result['success']:
                    for index, url in enumerate(raw.get('output_images') or []):
                        artifacts.append(
                            {
                                'artifact_id': f'{data["invocation_id"]}:image:{index}',
                                'type': 'image',
                                'media_type': 'image/*',
                                'url': url,
                                'invocation_id': data['invocation_id'],
                                'tool_name': data['tool_name'],
                            }
                        )
                updates['artifacts'] = artifacts
                public_data = {
                    'invocation_id': data['invocation_id'],
                    'tool_name': data['tool_name'],
                    'success': result['success'],
                    'artifacts': artifacts,
                }
                public_type = 'step_completed'
                await self.store.commit(user_id, 'runs', run_uuid, updates, public_type, without_secrets(public_data))
                if artifacts != state['artifacts']:
                    await self.store.commit(
                        user_id, 'runs', run_uuid, {}, 'artifact_committed', {'artifacts': artifacts}
                    )
                return
            elif event_type == 'tool_started':
                public_type = 'step_started'
            elif event_type == 'llm_started':
                public_type = 'step_started'
                public_data = {'step': 'llm', **data}
            elif event_type == 'llm_delta':
                public_type = 'public_summary'
            elif event_type == 'llm_complete':
                public_type = 'step_completed'
                if data.get('partial_result'):
                    updates['result'] = data['partial_result']
                public_data = {'step': 'llm', 'round': data.get('round', 0)}
            elif event_type == 'llm_tool_call':
                public_type = 'action_preview'
                public_data = {'tool_name': data.get('tool_name')}
            elif event_type in {'stream_start', 'stream_end', 'error', 'tool_result'}:
                return
            await self.store.commit(user_id, 'runs', run_uuid, updates, public_type, without_secrets(public_data))

    async def _execute(self, user_id: str, request: dict, predecessor: asyncio.Task | None) -> None:
        run_uuid = request['run_uuid']
        events = []
        try:
            if predecessor is not None:
                try:
                    await asyncio.shield(predecessor)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except Exception:
                    pass
            async with self._semaphore:
                async with self.store.lock(user_id, 'runs', run_uuid):
                    state, _ = await self.store.read(user_id, 'runs', run_uuid)
                    if state['status'] != 'queued':
                        return
                    await self.store.commit(
                        user_id, 'runs', run_uuid, {'status': 'running'}, 'run_started', {'status': 'running'}
                    )

                async def record(kind, data):
                    await self.record(user_id, run_uuid, kind, data)

                control = RunControl(record)
                self._controls[(user_id, run_uuid)] = control
                overrides = dict(request.get('parameters') or {})
                if 'temperature' in overrides:
                    overrides['llm'] = {'temperature': overrides.pop('temperature')}
                agent_request = AgentRequest.from_dict(
                    {
                        'user_id': user_id,
                        'session_id': request['session_id'],
                        'messages': request['messages'],
                        'config_overrides': overrides,
                    }
                )
                async for event in self.agent.run(agent_request, run_uuid=run_uuid, control=control, record=record):
                    events.append(event)
                    kind = str(event.event_type.value if hasattr(event.event_type, 'value') else event.event_type)
                    data = dict(event.data)
                    if kind == 'llm_complete':
                        partial = AgentResponse.from_events(request['session_id'], events).to_dict()
                        partial['metadata']['partial'] = True
                        data['partial_result'] = partial
                    await record(kind, data)
                result = AgentResponse.from_events(request['session_id'], events).to_dict()
                await self._terminal(user_id, run_uuid, 'succeeded', result=result)
        except asyncio.CancelledError:
            try:
                state = await self.get(user_id, run_uuid)
                await self._terminal(user_id, run_uuid, 'cancelled' if state['status'] == 'stopping' else 'interrupted')
            except PersistenceError:
                self.accepting = False
            raise
        except PersistenceError:
            self.accepting = False
            if self.store.healthy:
                try:
                    await self._terminal(
                        user_id,
                        run_uuid,
                        'interrupted',
                        error={
                            'code': 'STORAGE_UNAVAILABLE',
                            'message': '本地状态损坏或不可写，执行已停止',
                        },
                    )
                except PersistenceError:
                    pass
        except ExternalTaskUnresolved:
            await self._terminal(
                user_id,
                run_uuid,
                'failed',
                error={
                    'code': 'EXTERNAL_TASK_UNRESOLVED',
                    'message': '外部任务未确认，请对账；未自动重新提交',
                },
            )
        except Exception:
            await self._terminal(user_id, run_uuid, 'failed', error={'code': 'EXECUTION_FAILED', 'message': '执行失败'})

    async def _terminal(self, user_id: str, run_uuid: str, status: str, **updates) -> None:
        async with self.store.lock(user_id, 'runs', run_uuid):
            state, _ = await self.store.read(user_id, 'runs', run_uuid)
            if state['status'] in TERMINAL:
                return
            if state['status'] == 'stopping':
                status, updates = 'cancelled', {}
            commands = deepcopy(state['commands'])
            next_seq = state.get('last_agent_seq', 0) + 1 + bool(updates.get('result'))
            for command in commands.values():
                if command['status'] == 'accepted':
                    command.update(
                        status='applied' if command['type'] == 'stop' else 'rejected',
                        reason_code='APPLIED' if command['type'] == 'stop' else 'RUN_TERMINATED',
                        applied_agent_seq=next_seq,
                    )
            artifacts = deepcopy(state['artifacts'])
            if updates.get('result'):
                artifacts.append(
                    {
                        'artifact_id': 'final_text',
                        'type': 'text',
                        'media_type': 'text/plain',
                        'content': '\n'.join(m['content'] for m in updates['result']['messages']),
                    }
                )
                await self.store.snapshot(
                    user_id,
                    'runs',
                    run_uuid,
                    'result.json',
                    {
                        'result': updates['result'],
                        'artifacts': artifacts,
                    },
                )
            if artifacts != state['artifacts']:
                await self.store.commit(
                    user_id, 'runs', run_uuid, {'artifacts': artifacts}, 'artifact_committed', {'artifacts': artifacts}
                )
            await self.store.commit(
                user_id,
                'runs',
                run_uuid,
                {
                    'status': status,
                    'pending_interaction': None,
                    'commands': commands,
                    'artifacts': artifacts,
                    **updates,
                },
                'run_terminal',
                {'status': status, 'commands': list(commands.values()), 'artifacts': artifacts},
            )

    async def command(self, user_id: str, run_uuid: str, command: dict[str, Any]) -> dict[str, Any]:
        return await self.owned(self._command(user_id, run_uuid, deepcopy(command)))

    async def _command(self, user_id: str, run_uuid: str, command: dict) -> dict:
        command_id = validate_id(command['command_id'], 'command_id')
        command_digest = digest(command)
        async with self.store.lock(user_id, 'runs', run_uuid):
            state, _ = await self.store.read(user_id, 'runs', run_uuid)
            state = state or {
                'run_uuid': run_uuid,
                'user_id': user_id,
                'commands': {},
                'status': 'missing',
                'last_agent_seq': 0,
                'created_at': now(),
            }
            previous = state['commands'].get(command_id)
            if previous:
                if previous['digest'] != command_digest:
                    raise RunError('COMMAND_INPUT_CONFLICT')
                return deepcopy(previous)
            receipt = {
                'command_id': command_id,
                'type': command['type'],
                'digest': command_digest,
                'status': 'accepted',
                'reason_code': 'ACCEPTED',
                'applied_agent_seq': None,
            }
            updates: dict[str, Any] = {}
            control = self._controls.get((user_id, run_uuid))
            if command['type'] == 'stop':
                receipt.update(
                    remote_cancel_supported=False, remote_task_may_continue=bool(state.get('external_tasks'))
                )
                if state['status'] in TERMINAL:
                    receipt.update(status='no_op', reason_code='RUN_TERMINAL')
                elif state['status'] in {'missing', 'queued'}:
                    updates.update(status='cancelled', pending_interaction=None)
                    receipt.update(status='applied', reason_code='CANCELLED_BEFORE_START')
                else:
                    updates.update(status='stopping')
            elif state['status'] in TERMINAL or state['status'] in {'missing', 'queued', 'stopping'}:
                receipt.update(status='rejected', reason_code='RUN_NOT_CONTROLLABLE')
            elif command['type'] == 'intervene':
                if control is None:
                    receipt.update(status='rejected', reason_code='RUN_NOT_CONTROLLABLE')
            else:
                pending = state.get('pending_interaction')
                expected = {'approve': 'approval', 'input': 'input'}
                if not pending or pending['interaction_id'] != command.get('interaction_id') or control is None:
                    receipt.update(status='rejected', reason_code='INTERACTION_EXPIRED')
                elif control.answer is None or control.answer.done():
                    receipt.update(status='rejected', reason_code='INTERACTION_CONSUMED')
                elif command['type'] != 'skip' and expected.get(command['type']) != pending['kind']:
                    receipt.update(status='rejected', reason_code='INTERACTION_TYPE_MISMATCH')
                elif command['type'] == 'input':
                    try:
                        Draft202012Validator(pending['input_schema']).validate(
                            {**pending['arguments'], **command['payload']}
                        )
                    except ValidationError:
                        receipt.update(status='rejected', reason_code='INPUT_INVALID')
            if receipt['status'] == 'applied':
                receipt['applied_agent_seq'] = state['last_agent_seq'] + 1
            updates['commands'] = {**state['commands'], command_id: receipt}
            if state['status'] == 'missing':
                updates.update(
                    run_uuid=run_uuid,
                    user_id=user_id,
                    created_at=state['created_at'],
                    status=updates.get('status', 'missing'),
                )
            await self.store.commit(user_id, 'runs', run_uuid, updates, 'command_receipt', receipt)
            if receipt['reason_code'] == 'CANCELLED_BEFORE_START':
                await self.store.commit(user_id, 'runs', run_uuid, {}, 'run_terminal', {'status': 'cancelled'})
            if receipt['status'] == 'accepted':
                if command['type'] == 'stop':
                    task = self._tasks.get((user_id, run_uuid))
                    if task:
                        task.cancel()
                elif control:
                    control.deliver(command)
            return deepcopy(receipt)

    async def events(self, user_id: str, run_uuid: str, after_seq: int = 0) -> AsyncIterator[dict | None]:
        first = True
        idle = 0
        while True:
            async with self.store.lock(user_id, 'runs', run_uuid):
                state, events = await self.store.read(user_id, 'runs', run_uuid)
                if state is None:
                    raise RunError('RUN_NOT_FOUND', 404)
                if first and (after_seq < 0 or after_seq > state.get('last_agent_seq', 0)):
                    raise RunError('EVENT_CURSOR_INVALID', 400)
            first = False
            for event in events:
                if event['agent_seq'] > after_seq:
                    after_seq = event['agent_seq']
                    yield event
                    idle = 0
            if state['status'] in TERMINAL:
                return
            await asyncio.sleep(0.25)
            idle += 1
            if idle >= 40:
                yield None
                idle = 0

    async def close(self) -> None:
        self.accepting = False
        if self._operations:
            await asyncio.gather(*self._operations, return_exceptions=True)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.agent.shutdown()
        self.store.lease.release()
