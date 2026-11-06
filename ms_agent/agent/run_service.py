"""Agentic 单协调器生命周期：冻结执行、公开事实、精确控制及有限原任务收尾。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone

from jsonschema import Draft202012Validator, ValidationError

from ms_agent.agent.control import InteractionTimeout, RunControl
from ms_agent.agent.outputs import completion_for, tool_outputs
from ms_agent.agent.protocol import PROTOCOL_VERSION, ControlRunRequest, RunView, StartRunRequest
from ms_agent.agent.public_state import FINAL, settlement_for
from ms_agent.agent.scheduler import FairScheduler
from ms_agent.agent.snapshot import CapabilityError, prepare_snapshot
from ms_agent.session.locking import PersistenceError, durable_io, validate_id
from ms_agent.session.run_store import ProtocolVersionUnsupported, StorageCorrupted, digest, now
from ms_agent.tools.schema import ExternalTaskUnresolved


class RunError(Exception):
    def __init__(self, code, status=409, details=None):
        super().__init__(code)
        self.code, self.status, self.details = code, status, details


def seed_state(user, identifier):
    return {
        'schema_version': PROTOCOL_VERSION,
        'run_uuid': identifier,
        'agent_run_id': identifier,
        'user_id': user,
        'session_id': None,
        'user_message_uuid': None,
        'assistant_message_uuid': None,
        'parent_message_uuid': None,
        'status': 'missing',
        'state_revision': 0,
        'last_agent_seq': 0,
        'snapshot_agent_seq': 0,
        'request_digest': None,
        'input_snapshot_digest': None,
        'execution_snapshot_digest': None,
        'commands': {},
        'external_tasks': [],
        'tool_records': [],
        'execution_closed': False,
        'can_release_activity': False,
        'completion': {'completed_items': [], 'incomplete_items': []},
        'settlement': {'status': 'none', 'tasks': [], 'error': None},
        'error': None,
        'created_at': now(),
        'updated_at': now(),
        'completed_at': None,
        'stop_reason': None,
        'public_snapshot': None,
    }


class RunService:
    def __init__(self, store, agent, *, runtime_config, event_config, lease_wait=0):
        self.store, self.agent = store, agent
        self.options, self._event_config = dict(runtime_config), dict(event_config)
        self.max_active_runs = runtime_config['max_active_runs']
        self._scheduler = FairScheduler(self.max_active_runs, runtime_config['per_user_active'])
        self._limit, self._user_limit = runtime_config['max_inflight_runs'], runtime_config['per_user_inflight']
        self._interaction_timeout, self._lease_wait = runtime_config['interaction_timeout'], lease_wait
        self._tasks, self._tails, self._controls, self._settling = {}, {}, {}, {}
        self._operations, self._reservations = set(), set()
        self._unresolved = set()
        self.draining = asyncio.Event()
        self.accepting, self.subscribers = False, 0
        self._settlement_slots = asyncio.Semaphore(agent._config['settlement']['concurrency'])

    async def initialize(self):
        try:
            await durable_io(self.store.lease.acquire, self._lease_wait)
            await self.store.initialize_epoch()
            recovered = await self.store.recover()
            await self.agent.initialize()
            for user, kind, identifier in recovered:
                if kind == 'runs':
                    state = await self.store.read_state(user, 'runs', identifier)
                    await self._finish(
                        user,
                        identifier,
                        state.get('desired_terminal', 'failed'),
                        state.get('error') or {'code': 'SERVICE_INTERRUPTED', 'message': '执行中断，未续跑'},
                    )
        except BaseException:
            await self.agent.shutdown()
            self.store.lease.release()
            raise
        self.accepting = True

    async def owned(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._operations.add(task)

        def done(t):
            self._operations.discard(t)
            if not t.cancelled():
                t.exception()

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def start(self, user_id, request):
        body = StartRunRequest.model_validate(request).sdk_input()
        return await self.owned(self._start(validate_id(user_id), body))

    async def _start(self, user, request):
        identifier = request['run_uuid']
        key = (user, identifier)
        async with self.store.lock(user, 'runs', identifier):
            existing = await self.store.read_state(user, 'runs', identifier)
            request_hash = digest(request)
            if existing and existing['request_digest']:
                if existing['request_digest'] != request_hash:
                    raise RunError('RUN_INPUT_CONFLICT')
                return self.view(existing), False
            if not self.accepting or not self.store.healthy:
                raise RunError('SERVICE_UNAVAILABLE', 503)
            cancelled = existing and existing['execution_closed']
            inflight = set(self._tasks) | set(self._settling) | self._reservations | self._unresolved
            if not cancelled and (
                len(inflight) >= self._limit or sum(k[0] == user for k in inflight) >= self._user_limit
            ):
                raise RunError('RUN_QUEUE_FULL', 429, {'scope': 'global' if len(inflight) >= self._limit else 'user'})
            self._reservations.add(key)
            try:
                if request['source_run_uuid']:
                    source = await self.store.read_state(user, 'runs', request['source_run_uuid'])
                    if (
                        not source
                        or not source['can_release_activity']
                        or source['status'] not in ('failed', 'cancelled')
                    ):
                        raise RunError('SOURCE_RUN_NOT_RETRYABLE')
                    if (
                        source['input_snapshot_digest'] != digest(request['input_snapshot'])
                        or source['session_id'] != request['session_id']
                        or source['parent_message_uuid'] != request['parent_message_uuid']
                    ):
                        raise RunError('RETRY_SNAPSHOT_CONFLICT')
                try:
                    prepared = (
                        prepare_snapshot(request, self.agent._config, self.agent._tool_registry)
                        if not cancelled
                        else None
                    )
                except (CapabilityError, ValidationError) as exc:
                    raise RunError(
                        getattr(exc, 'code', 'CAPABILITY_NOT_SUPPORTED'),
                        503 if getattr(exc, 'code', '') == 'DEPENDENCY_UNAVAILABLE' else 422,
                        {'execution_started': False, 'message': '冻结输入的能力或资产验证失败'},
                    ) from exc
                state = existing or seed_state(user, identifier)
                updates = {
                    **state,
                    **{
                        k: request[k]
                        for k in ('session_id', 'user_message_uuid', 'assistant_message_uuid', 'parent_message_uuid')
                    },
                    'request_digest': request_hash,
                    'input_snapshot_digest': digest(request['input_snapshot']),
                    'execution_snapshot_digest': digest(prepared) if prepared else None,
                    'request': request,
                    'prepared': prepared,
                    'status': state['status'] if cancelled else 'queued',
                }
                await self.store.snapshot(user, 'runs', identifier, 'input_snapshot.json', request)
                if prepared:
                    await self.store.snapshot(user, 'runs', identifier, 'execution_context.json', prepared)
                state = await self.store.commit(
                    user, 'runs', identifier, updates, 'run.status', {'status': updates['status']}
                )
                if not cancelled:
                    session_key = (user, request['session_id'])
                    predecessor = self._tails.get(session_key)
                    task = asyncio.create_task(self._execute(user, request, prepared, predecessor))
                    task._aigc_predecessor = predecessor
                    self._tasks[key], self._tails[session_key] = task, task
                    task.add_done_callback(lambda t: self._finished(key, session_key, t))
                return self.view(state), not cancelled
            finally:
                self._reservations.discard(key)

    def _finished(self, key, session, task):
        self._tasks.pop(key, None)
        self._controls.pop(key, None)
        predecessor = getattr(task, '_aigc_predecessor', None)
        if self._tails.get(session) is task:
            previous = predecessor
            while previous is not None and previous.done():
                previous = getattr(previous, '_aigc_predecessor', None)
            if previous is None:
                self._tails.pop(session, None)
            else:
                self._tails[session] = previous
        if predecessor is not None and not predecessor.done():
            predecessor.add_done_callback(lambda _: setattr(task, '_aigc_predecessor', None))
        else:
            task._aigc_predecessor = None
        if not task.cancelled():
            task.exception()

    async def get(self, user, identifier):
        async with self.store.lock(user, 'runs', identifier):
            state = await self.store.read_state(user, 'runs', identifier)
            if state is None:
                raise RunError('RUN_NOT_FOUND', 404)
            return self.view(state)

    @staticmethod
    def view(state):
        fields = RunView.model_fields
        value = {key: deepcopy(v) for key, v in state.items() if key in fields}
        return RunView.model_validate(value).model_dump()

    async def guard(self, user, identifier):
        async with self.store.lock(user, 'runs', identifier):
            state = await self.store.read_state(user, 'runs', identifier)
            if self.draining.is_set() or state['execution_closed'] or state['status'] == 'stopping':
                raise asyncio.CancelledError
            deadline = datetime.fromisoformat(state['request']['execution_policy']['deadline_at'])
            expired = datetime.now(timezone.utc) >= deadline
        if expired:
            await self._expire(user, identifier)
            raise asyncio.CancelledError

    async def _expire(self, user, identifier):
        async with self.store.lock(user, 'runs', identifier):
            state = await self.store.read_state(user, 'runs', identifier)
            if state['can_release_activity'] or state['stop_reason']:
                return
            await self.store.commit(
                user,
                'runs',
                identifier,
                {'stop_reason': 'DEADLINE', 'stop_at': now(), 'status': 'stopping'},
                'run.status',
                {'status': 'stopping'},
            )

    async def record(self, user, identifier, kind, data):
        async with self.store.lock(user, 'runs', identifier):
            state = await self.store.read_state(user, 'runs', identifier)
            if state['can_release_activity']:
                return
            updates, public_kind, payload = {}, None, None
            if kind == 'execution_context':
                return  # 已在受理时冻结，不能覆盖为动态上下文。
            if kind == 'submission_intent':
                if self.draining.is_set() or state['execution_closed'] or state['status'] == 'stopping':
                    raise asyncio.CancelledError
                deadline = datetime.fromisoformat(state['request']['execution_policy']['deadline_at'])
                if datetime.now(timezone.utc) >= deadline:
                    await self.store.commit(
                        user,
                        'runs',
                        identifier,
                        {
                            'stop_reason': state['stop_reason'] or 'DEADLINE',
                            'stop_at': state.get('stop_at') or now(),
                            'status': 'stopping',
                        },
                        'run.status',
                        {'status': 'stopping'},
                    )
                    raise asyncio.CancelledError
            if kind in (
                'submission_intent',
                'submission_not_sent',
                'task_submitted',
                'delivery_unknown',
                'tool_completed',
            ):
                tasks = deepcopy(state['external_tasks'])
                invocation = next((t for t in tasks if t['invocation_id'] == data['invocation_id']), None)
                if invocation is None:
                    invocation = {
                        'invocation_id': data['invocation_id'],
                        'tool_name': data['tool_name'],
                        'arguments': data.get('arguments', {}),
                        'task_id': None,
                        'request_id': None,
                        'status': 'intent',
                        'remote_cancel_supported': False,
                    }
                    tasks.append(invocation)
                if kind == 'submission_intent':
                    invocation['arguments'] = deepcopy(data['arguments'])
                elif kind == 'task_submitted':
                    invocation.update(task_id=data['task_id'], request_id=data.get('request_id'), status='submitted')
                elif kind == 'delivery_unknown':
                    # 已知未发出的事实优先于推测性未知，停止竞态不得把 unsent 改判为 unknown。
                    if invocation['status'] != 'failed':
                        invocation['status'] = 'unknown'
                elif kind == 'submission_not_sent':
                    invocation['status'] = 'failed'
                else:
                    result = data['result']
                    if result['metadata'].get('unresolved') or result['metadata'].get('delivery_unknown'):
                        invocation['status'] = 'submitted' if invocation.get('task_id') else 'unknown'
                    else:
                        invocation['status'] = 'succeeded' if result['success'] else 'failed'
                    updates['tool_records'] = state['tool_records'] + [deepcopy(data)]
                    if invocation['status'] in ('succeeded', 'failed'):
                        for output in tool_outputs(
                            state['prepared'], invocation, result, state['assistant_message_uuid']
                        ):
                            await self.store.commit(user, 'runs', identifier, {}, 'output.upsert', output)
                    public_kind = 'step.upsert'
                    payload = {
                        'step_id': data['invocation_id'],
                        'title': '工具执行',
                        'status': 'succeeded' if result['success'] else 'failed',
                        'summary': '工具已确认结果' if result['success'] else '工具结果未成功确认',
                        'tool': {'type': 'api', 'name': data['tool_name']},
                        'error': None if result['success'] else {'code': 'TOOL_FAILED', 'message': '工具执行未成功'},
                    }
                updates.update(external_tasks=tasks, settlement=settlement_for(tasks))
                public_kind = public_kind or 'settlement.upsert'
                payload = payload or updates['settlement']
            elif kind == 'interaction_requested':
                if state['status'] == 'stopping' or state['execution_closed']:
                    raise asyncio.CancelledError
                if data['blocking']:
                    updates['status'] = 'waiting_input'
                public_kind, payload = 'interaction.required', data
            elif kind == 'interaction_resolved':
                if state['status'] == 'waiting_input':
                    updates['status'] = 'running'
                public_kind, payload = 'interaction.resolved', data
            elif kind == 'command_applied':
                commands = deepcopy(state['commands'])
                receipt = commands[data['command_id']]
                receipt.update(
                    command_status='applied', reason_code='APPLIED', applied_agent_seq=state['last_agent_seq'] + 1
                )
                updates['commands'] = commands
                public_kind, payload = 'command.result', self._receipt(state | updates, receipt)
            elif kind == 'llm_started':
                step = identifier + ':llm:' + str(data['round'])
                updates['current_step'] = step
                public_kind, payload = (
                    'step.upsert',
                    {
                        'step_id': step,
                        'title': '固定参数执行' if state['prepared']['mode'] == 'custom' else '规划执行',
                        'status': 'running',
                        'summary': '',
                        'tool': None,
                        'error': None,
                    },
                )
            elif kind == 'llm_delta':
                public_kind, payload = 'reply.delta', {'text': data['content']}
            elif kind == 'llm_complete':
                if data.get('content'):
                    from ms_agent.agent.protocol import Output

                    output = Output(output_id='final_text', type='text', content=data['content']).model_dump()
                    await self.store.commit(user, 'runs', identifier, {}, 'output.upsert', output)
                public_kind, payload = (
                    'step.upsert',
                    {
                        'step_id': state['current_step'],
                        'title': '规划执行',
                        'status': 'succeeded',
                        'summary': '本次规划完成',
                        'tool': None,
                        'error': None,
                    },
                )
            elif kind == 'llm_tool_call':
                public_kind, payload = (
                    'behavior.preview',
                    {
                        'summary': '执行已授权工具，保留冻结的输入约束',
                        'tools': [{'type': 'api', 'name': data['tool_name']}],
                        'assets': state['prepared']['input_snapshot']['asset_refs'],
                        'actions': [],
                    },
                )
            elif kind == 'tool_started':
                public_kind, payload = (
                    'step.upsert',
                    {
                        'step_id': data['invocation_id'],
                        'title': '工具执行',
                        'status': 'running',
                        'summary': '已验证输入，准备执行',
                        'tool': {'type': 'api', 'name': data['tool_name']},
                        'error': None,
                    },
                )
            if public_kind:
                await self.store.commit(user, 'runs', identifier, updates, public_kind, payload)
                if kind == 'tool_started':
                    await self.store.commit(
                        user,
                        'runs',
                        identifier,
                        {},
                        'summary.delta',
                        {
                            'summary_id': data['invocation_id'] + ':summary',
                            'step_id': data['invocation_id'],
                            'text': '使用已验证的工具与冻结输入执行本步骤。',
                        },
                    )

    async def _execute(self, user, request, prepared, predecessor):
        identifier = request['run_uuid']

        async def record(kind, data):
            await self.record(user, identifier, kind, data)

        try:
            # 排队也受整轮 deadline 控制，定时器不依赖后端连接。
            timeout = max(
                0,
                (
                    datetime.fromisoformat(request['execution_policy']['deadline_at']) - datetime.now(timezone.utc)
                ).total_seconds(),
            )

            async def run():
                nonlocal predecessor
                while predecessor is not None:
                    try:
                        await asyncio.shield(predecessor)
                        break
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                        predecessor = getattr(predecessor, '_aigc_predecessor', None)
                async with self._scheduler.permit(user) as permit:
                    await self.guard(user, identifier)
                    async with self.store.lock(user, 'runs', identifier):
                        state = await self.store.read_state(user, 'runs', identifier)
                        if state['execution_closed'] or state['stop_reason'] or self.draining.is_set():
                            raise asyncio.CancelledError
                        await self.store.commit(
                            user, 'runs', identifier, {'status': 'running'}, 'run.status', {'status': 'running'}
                        )
                    control = RunControl(
                        record,
                        permit=permit,
                        timeout=self._interaction_timeout,
                        guard=lambda: self.guard(user, identifier),
                    )
                    self._controls[(user, identifier)] = control
                    async for event in self.agent.run_frozen(
                        prepared,
                        user_id=user,
                        session_id=request['session_id'],
                        run_uuid=identifier,
                        control=control,
                        record=record,
                    ):
                        await record(event.event_type.value, dict(event.data))

            await asyncio.wait_for(run(), timeout)
            await self._finish(user, identifier, 'succeeded')
        except asyncio.TimeoutError:
            await self._expire(user, identifier)
            await self._finish(user, identifier, 'failed', {'code': 'DEADLINE_EXCEEDED', 'message': '整轮截止'})
        except asyncio.CancelledError:
            await self._finish(
                user, identifier, 'failed', {'code': 'SERVICE_INTERRUPTED', 'message': '执行已停止，未自动续跑'}
            )
            raise
        except InteractionTimeout:
            await self._finish(user, identifier, 'failed', {'code': 'INTERACTION_TIMEOUT', 'message': '等待交互超时'})
        except ExternalTaskUnresolved:
            await self._finish(
                user, identifier, 'failed', {'code': 'EXTERNAL_TASK_UNRESOLVED', 'message': '原任务结果未确认'}
            )
        except (StorageCorrupted, ProtocolVersionUnsupported):
            return
        except PersistenceError:
            self.accepting = False
        except Exception as exc:
            await self._finish(
                user, identifier, 'failed', {'code': getattr(exc, 'code', 'EXECUTION_FAILED'), 'message': '执行未完成'}
            )

    async def _finish(self, user, identifier, desired, error=None):
        async with self.store.lock(user, 'runs', identifier):
            state = await self.store.read_state(user, 'runs', identifier)
            if state['can_release_activity']:
                return
            for step in list(state['public_snapshot']['steps']):
                if step['status'] == 'running':
                    await self.store.commit(
                        user,
                        'runs',
                        identifier,
                        {},
                        'step.upsert',
                        {**step, 'status': 'interrupted', 'summary': '执行已结束或中断'},
                    )
            for interaction in list(state['public_snapshot']['pending_interactions']):
                await self.store.commit(
                    user,
                    'runs',
                    identifier,
                    {},
                    'interaction.resolved',
                    {'interaction_id': interaction['interaction_id'], 'status': 'EXPIRED'},
                )
            state = await self.store.read_state(user, 'runs', identifier)
            tasks = deepcopy(state['external_tasks'])
            for task in tasks:
                if task['status'] == 'intent':
                    task['status'] = 'unknown'
            settlement = settlement_for(tasks)
            safe = settlement['status'] in ('none', 'resolved')
            completion = completion_for(state) if state.get('prepared') else state['completion']
            if state['stop_reason']:
                desired = 'cancelled' if state['stop_reason'] == 'USER_STOP' else 'failed'
                error = None if desired == 'cancelled' else {'code': 'DEADLINE_EXCEEDED', 'message': '整轮截止'}
            elif desired == 'succeeded':
                if completion['incomplete_items']:
                    desired = 'partial_succeeded' if completion['completed_items'] else 'failed'
            commands = deepcopy(state['commands'])
            for receipt in commands.values():
                if receipt['command_status'] == 'accepted':
                    applied = receipt['action'] == 'stop'
                    receipt.update(
                        command_status='applied' if applied else 'rejected',
                        reason_code='APPLIED' if applied else 'RUN_TERMINATED',
                        applied_agent_seq=state['last_agent_seq'] + 1 if applied else None,
                    )
            updates = {
                'execution_closed': True,
                'can_release_activity': safe,
                'external_tasks': tasks,
                'settlement': settlement,
                'commands': commands,
                'completion': completion,
                'error': error,
                'desired_terminal': desired,
                'status': desired if safe else 'stopping' if settlement['status'] == 'tracking' else 'interrupted',
                'completed_at': now() if safe else None,
            }
            if safe and state['public_snapshot']['outputs']:
                await self.store.commit(
                    user,
                    'runs',
                    identifier,
                    {},
                    'recipe.suggested',
                    {
                        'output_ids': [o['output_id'] for o in state['public_snapshot']['outputs']],
                        'message': '可将已确认的执行事实保存为可编辑配方，不自动发布 Skill',
                    },
                )
            await self.store.commit(
                user,
                'runs',
                identifier,
                updates,
                'run.completed' if safe else 'settlement.upsert',
                {'status': updates['status'], 'settlement': settlement, 'can_release_activity': safe},
            )
        if not safe:
            self._unresolved.add((user, identifier))
            self._schedule_settlement(user, identifier)

    def _schedule_settlement(self, user, identifier):
        key = (user, identifier)
        if key in self._settling or self.draining.is_set():
            return
        task = asyncio.create_task(self._settle(user, identifier))
        self._settling[key] = task

        def done(t):
            self._settling.pop(key, None)
            if not t.cancelled():
                t.exception()

        task.add_done_callback(done)

    async def _settle(self, user, identifier):
        options = self.agent._config['settlement']
        async with self._settlement_slots:
            state = await self.store.read_state(user, 'runs', identifier)
            prepared = state.get('prepared')
            if not prepared or not any(
                t.get('task_id') for t in state['external_tasks'] if t['status'] not in ('failed', 'succeeded')
            ):
                return
            registry, tools = self.agent.frozen_registry(prepared, settlement=True)
            deadline = asyncio.get_running_loop().time() + options['budget']
            try:
                await registry.load_from_dict(tools['api'])
                while not self.draining.is_set() and asyncio.get_running_loop().time() < deadline:
                    state = await self.store.read_state(user, 'runs', identifier)
                    for invocation in state['external_tasks']:
                        if not invocation.get('task_id') or invocation['status'] in ('failed', 'succeeded'):
                            continue
                        try:
                            result = await asyncio.wait_for(
                                registry._tools[invocation['tool_name']].query_task(invocation['task_id']),
                                min(options['query_timeout'], max(0.001, deadline - asyncio.get_running_loop().time())),
                            )
                        except (OSError, asyncio.TimeoutError, KeyError, ValueError):
                            continue
                        if result is not None and not result.metadata.get('unresolved'):
                            await self.record(
                                user, identifier, 'tool_completed', {**invocation, 'result': asdict(result)}
                            )
                    state = await self.store.read_state(user, 'runs', identifier)
                    if state['settlement']['status'] in ('none', 'resolved'):
                        await self._finish(user, identifier, state['desired_terminal'], state['error'])
                        self._unresolved.discard((user, identifier))
                        return
                    await asyncio.sleep(
                        min(options['retry_interval'], max(0, deadline - asyncio.get_running_loop().time()))
                    )
                await self._settlement_unknown(user, identifier)
            except PersistenceError:
                self.accepting = False
            except Exception:
                await self._settlement_unknown(user, identifier)
            finally:
                await registry.close()

    async def _settlement_unknown(self, user, identifier):
        async with self.store.lock(user, 'runs', identifier):
            state = await self.store.read_state(user, 'runs', identifier)
            if state['can_release_activity']:
                return
            settlement = {
                **state['settlement'],
                'status': 'unknown',
                'error': {'code': 'SETTLEMENT_UNRESOLVED', 'message': '收尾未核清，禁止自动重提'},
            }
            await self.store.commit(
                user,
                'runs',
                identifier,
                {'status': 'interrupted', 'settlement': settlement},
                'settlement.upsert',
                settlement,
            )

    def _receipt(self, state, receipt):
        value = {k: deepcopy(v) for k, v in receipt.items() if k not in ('digest', 'request')}
        value.update(
            run_status=state['status'],
            state_revision=state.get('state_revision', 0),
            execution_closed=state['execution_closed'],
            settlement_status=state['settlement']['status'],
            can_release_activity=state['can_release_activity'],
            remote_task_may_continue=any(
                t['status'] in ('intent', 'submitted', 'unknown') for t in state['external_tasks']
            ),
        )
        return value

    async def command(self, user, identifier, command):
        return await self.owned(self._command(user, identifier, ControlRunRequest.model_validate(command).model_dump()))

    async def _command(self, user, identifier, command, *, cancel_task=True):
        command_id = command['command_id']
        async with self.store.lock(user, 'runs', identifier):
            state = await self.store.read_state(user, 'runs', identifier) or seed_state(user, identifier)
            old = state['commands'].get(command_id)
            if old:
                if old['digest'] != digest(command):
                    raise RunError('COMMAND_KEY_CONFLICT')
                return self._receipt(state, old)
            action = command['action']
            reason, receipt_status, updates = 'ACCEPTED', 'accepted', {}
            control = self._controls.get((user, identifier))
            if action == 'stop':
                if state['can_release_activity']:
                    reason, receipt_status = 'RUN_TERMINAL', 'no_op'
                else:
                    updates.update(
                        stop_reason=state['stop_reason'] or command['stop_reason'],
                        stop_at=state.get('stop_at') or now(),
                        status='stopping',
                    )
                    receipt_status = 'applied'
                    reason = 'APPLIED'
            else:
                interactions = (state['public_snapshot'] or {}).get('pending_interactions', [])
                pending = next((i for i in interactions if i['interaction_id'] == command['interaction_id']), None)
                allowed = (
                    next((a for a in pending['actions'] if a['action_id'] == command['action_id']), None)
                    if pending
                    else None
                )
                if (
                    state['status'] in FINAL
                    or state['execution_closed']
                    or state['status'] == 'stopping'
                    or control is None
                ):
                    reason, receipt_status = 'RUN_STATE_CONFLICT', 'rejected'
                elif not allowed or allowed['action'] != action:
                    reason, receipt_status = 'INTERACTION_EXPIRED', 'rejected'
                elif pending['blocking'] and (control.answer is None or control.answer.done()):
                    reason, receipt_status = 'INTERACTION_EXPIRED', 'rejected'
                elif allowed.get('input_schema'):
                    try:
                        body = {k: v for k, v in command['input'].items() if v not in (None, {}, [])}
                        Draft202012Validator(allowed['input_schema']).validate(body)
                    except ValidationError:
                        reason, receipt_status = 'INVALID_INTERACTION_INPUT', 'rejected'
            receipt = {
                'run_uuid': identifier,
                'command_id': command_id,
                'action': action,
                'command_status': receipt_status,
                'reason_code': reason,
                'interaction_id': command['interaction_id'],
                'action_id': command['action_id'],
                'applied_agent_seq': state['last_agent_seq'] + 1 if receipt_status == 'applied' else None,
                'remote_cancel_supported': False,
                'remote_task_may_continue': bool(state['external_tasks']),
                'digest': digest(command),
                'request': command,
            }
            updates['commands'] = {**state['commands'], command_id: receipt}
            if state['public_snapshot'] is None:
                updates = {**state, **updates}
            committed = await self.store.commit(
                user, 'runs', identifier, updates, 'command.result', self._receipt(state | updates, receipt)
            )
            if action != 'stop' and receipt_status == 'accepted':
                control.deliver(command)
                if action == 'intervene':
                    await self.store.commit(
                        user,
                        'runs',
                        identifier,
                        {},
                        'interaction.resolved',
                        {'interaction_id': command['interaction_id'], 'status': 'RESOLVED'},
                    )
        if action == 'stop' and receipt_status != 'no_op' and cancel_task:
            task = self._tasks.get((user, identifier))
            if task:
                task.cancel()
                # _execute 负责等待在途记录提交并完成结算。
            else:
                await self._finish(user, identifier, 'cancelled')
            committed = await self.store.read_state(user, 'runs', identifier)
        return self._receipt(committed, committed['commands'][command_id])

    def reserve_subscription(self):
        if self.subscribers >= self._event_config['max_subscribers']:
            raise RunError('EVENT_SUBSCRIBERS_FULL', 429)
        self.subscribers += 1

    async def events(self, user, identifier, after_seq=0, *, managed=True):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._event_config['max_stream_seconds']
        if managed:
            self.reserve_subscription()
        try:
            first = True
            while not self.draining.is_set() and loop.time() < deadline:
                changed = self.store.change_event(user, 'runs', identifier)
                async with self.store.lock(user, 'runs', identifier):
                    state, events = await self.store.read_events(
                        user, 'runs', identifier, after_seq, limit=self._event_config.get('batch_size', 128)
                    )
                if state is None:
                    raise RunError('RUN_NOT_FOUND', 404)
                if first and not 0 <= after_seq <= state['last_agent_seq']:
                    raise RunError('EVENT_CURSOR_INVALID', 400)
                first = False
                for event in events:
                    after_seq = event['agent_seq']
                    yield event
                if after_seq < state['last_agent_seq']:
                    continue
                if state['can_release_activity']:
                    return
                waits = [asyncio.create_task(changed.wait()), asyncio.create_task(self.draining.wait())]
                try:
                    done, _ = await asyncio.wait(
                        waits,
                        timeout=min(self._event_config['heartbeat_seconds'], max(0, deadline - loop.time())),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        yield None
                finally:
                    for task in waits:
                        task.cancel()
                    await asyncio.gather(*waits, return_exceptions=True)
        finally:
            if managed:
                self.subscribers -= 1

    def begin_drain(self):
        self.accepting = False
        self.draining.set()
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._settling.values()):
            task.cancel()

    async def close(self, drain_timeout=0):
        self.begin_drain()
        await asyncio.gather(*self._operations, return_exceptions=True)
        await asyncio.gather(*self._tasks.values(), *self._settling.values(), return_exceptions=True)
        await self.agent.shutdown()
        self.store.lease.release()
