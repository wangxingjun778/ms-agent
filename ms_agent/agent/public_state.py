"""同一持久提交中的公开 reducer；公开数据不包含原始工具结果或私有提示词。"""

from __future__ import annotations

from copy import deepcopy

from ms_agent.agent.protocol import PROTOCOL_VERSION

FINAL = {'succeeded', 'partial_succeeded', 'failed', 'cancelled'}
PUBLIC_FIELDS = (
    'run_uuid',
    'session_id',
    'user_message_uuid',
    'assistant_message_uuid',
    'parent_message_uuid',
    'status',
    'state_revision',
    'snapshot_agent_seq',
    'completion',
    'settlement',
    'execution_closed',
    'can_release_activity',
    'error',
)


def initial_public(state):
    return {
        'schema_version': PROTOCOL_VERSION,
        'behavior_preview': None,
        'steps': [],
        'reasoning_summaries': [],
        'reply': '',
        'outputs': [],
        'result_actions': [],
        'pending_interactions': [],
        'commands': [],
        **{key: deepcopy(state.get(key)) for key in PUBLIC_FIELDS},
    }


def upsert(items, value, key):
    for i, previous in enumerate(items):
        if previous[key] == value[key]:
            items[i] = deepcopy(value)
            return
    items.append(deepcopy(value))


def public_patch(previous, state, *, commands_changed=False):
    """只把变化的公共生命周期事实写入事件，支持不依赖内部 journal 的回放。"""
    patch = {
        key: deepcopy(state.get(key))
        for key in PUBLIC_FIELDS
        if key != 'snapshot_agent_seq' and (not previous or previous.get(key) != state.get(key))
    }
    if commands_changed:
        patch['commands'] = [
            {key: deepcopy(value) for key, value in receipt.items() if key not in ('digest', 'request')}
            for receipt in state.get('commands', {}).values()
        ]
    return patch


def reduce_public(previous, state, event):
    if previous and event['agent_seq'] <= previous['snapshot_agent_seq']:
        return deepcopy(previous)
    public = deepcopy(previous) if previous else initial_public(state or {})
    kind = event['event_type']
    value = {key: value for key, value in event['payload'].items() if key != 'run_state'}
    if kind == 'behavior.preview':
        public['behavior_preview'] = deepcopy(value)
    elif kind == 'step.upsert':
        upsert(public['steps'], value, 'step_id')
    elif kind == 'summary.delta':
        summary = next((s for s in public['reasoning_summaries'] if s['summary_id'] == value['summary_id']), None)
        if summary:
            summary['text'] += value['text']
        else:
            public['reasoning_summaries'].append(deepcopy(value))
    elif kind == 'reply.delta':
        public['reply'] += value['text']
    elif kind == 'output.upsert':
        upsert(public['outputs'], value, 'output_id')
        public['result_actions'] = [
            action for output in public['outputs'] for action in output.get('result_actions', [])
        ]
    elif kind == 'interaction.required':
        upsert(public['pending_interactions'], value, 'interaction_id')
    elif kind == 'interaction.resolved':
        public['pending_interactions'] = [
            item for item in public['pending_interactions'] if item['interaction_id'] != value['interaction_id']
        ]
    elif kind == 'command.result':
        upsert(public['commands'], value, 'command_id')
    public.update(deepcopy(event['payload']['run_state']))
    public['snapshot_agent_seq'] = event['agent_seq']
    # 所有回执随相同水位公开，避免 receipt 与 Run.status 的时间漂移。
    for receipt in public['commands']:
        receipt.update(
            run_status=public['status'],
            state_revision=public['state_revision'],
            execution_closed=public['execution_closed'],
            can_release_activity=public['can_release_activity'],
            settlement_status=public['settlement']['status'],
            remote_task_may_continue=any(
                t['status'] in ('intent', 'submitted', 'unknown') for t in public['settlement']['tasks']
            ),
        )
    return public


def settlement_for(tasks):
    if not tasks:
        return {'status': 'none', 'tasks': [], 'error': None}
    unknown = any(t['status'] == 'unknown' for t in tasks)
    active = any(t['status'] in ('intent', 'submitted') for t in tasks)
    status = 'unknown' if unknown else 'tracking' if active else 'resolved'
    fields = ('invocation_id', 'tool_name', 'task_id', 'request_id', 'status', 'remote_cancel_supported')
    return {'status': status, 'tasks': [{key: task.get(key) for key in fields} for task in tasks], 'error': None}
