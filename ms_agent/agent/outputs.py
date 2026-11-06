"""工具输出的事实归一化：submitted 不冒充 confirmed，不下载资源。"""

from __future__ import annotations

from copy import deepcopy

from ms_agent.agent.protocol import Output
from ms_agent.session.run_store import digest


def extract(data, path):
    value = data
    for part in (path or '').split('.'):
        if not part or not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def tool_outputs(prepared, invocation, result, assistant_uuid):
    snapshot = prepared['input_snapshot']
    effective = snapshot['effective']
    arguments = invocation['arguments']
    model = next((m for m in prepared['models'].values() if m['model_id'] == arguments.get('model')), None)
    if model is None:
        return []
    raw = result.get('raw_data') or {}
    if not result['success']:
        tool = next(t for t in prepared['tools']['api']['tools'] if t['name'] == invocation['tool_name'])
        mapping = tool['output_mapping']
        if (
            raw.get('_http_status', 200) >= 400
            or extract(raw, mapping['status_path']) not in mapping['failure_statuses']
        ):
            return []
    urls = raw.get('output_images') if model['output_type'] == 'image' else raw.get('output_videos')
    if not isinstance(urls, list):
        urls = []
    confirmed = extract(raw, model.get('confirmed_path')) if model.get('confirmed_path') else None
    if not isinstance(confirmed, dict):
        confirmed = None
    if confirmed:
        # 只暴露明确允许的实际字段；下游额外原始数据不进入公开协议。
        confirmed = {
            key: deepcopy(confirmed[key])
            for key in ('prompt', 'negative_prompt', 'model_ref', 'loras', 'params')
            if key in confirmed
        }
    outputs = []
    for index, url in enumerate(urls):
        if not isinstance(url, str) or not url.startswith(('https://', 'http://')):
            continue
        identifier = invocation['invocation_id'] + ':out:' + str(index)
        refs = [{'assistant_message_uuid': assistant_uuid, 'output_id': identifier}]
        actions = [{'action': 'save_recipe', 'label': '保存配方', 'output_refs': refs, 'suggestions': []}]
        if (
            confirmed
            and all(confirmed.get(key) for key in ('prompt', 'model_ref'))
            and 'regenerate' in model['actions']
        ):
            actions.append({'action': 'regenerate', 'label': '换一批', 'output_refs': refs, 'suggestions': []})
        if model['output_type'] == 'image' and 'input_refs' in model['parameter_map']:
            for action, label in (('continue_edit', '继续修改'), ('change_reference', '换参考')):
                if action in model['actions']:
                    actions.append({'action': action, 'label': label, 'output_refs': refs, 'suggestions': []})
        if effective['generation_mode'] == 'agent' and 'rethink' in model['actions']:
            actions.append({'action': 'rethink', 'label': '换个思路', 'output_refs': refs, 'suggestions': []})
        value = {
            'output_id': identifier,
            'type': model['output_type'],
            'content': None,
            'url': url,
            'poster_url': extract(raw, model.get('poster_path')) if model.get('poster_path') else None,
            'reference_inputs': snapshot['input_refs'],
            'asset_refs': snapshot['asset_refs'],
            'generation': {'requested': effective, 'submitted': arguments, 'confirmed': confirmed},
            'actual_generation': confirmed,
            'result_actions': actions,
        }
        outputs.append(Output.model_validate(value).model_dump())
    return outputs


def completion_for(state):
    snapshot = state['prepared']['input_snapshot']
    target = snapshot['effective']['preferences']['image_count'] if snapshot['effective']['modality'] == 'image' else 1
    outputs = [o for o in state['public_snapshot']['outputs'] if o['type'] != 'text']
    records = state.get('tool_records', [])
    if state['prepared']['mode'] == 'agent' and not records and state['public_snapshot']['reply']:
        return {
            'completed_items': [
                {'item_id': 'reply', 'output_ids': ['final_text'], 'reason_code': None, 'message': None}
            ],
            'incomplete_items': [],
        }
    completed = [
        {'item_id': f'output-{i}', 'output_ids': [o['output_id']], 'reason_code': None, 'message': None}
        for i, o in enumerate(outputs)
    ]
    incomplete = [
        {
            'item_id': f'output-{i}',
            'output_ids': [],
            'reason_code': 'OUTPUT_UNAVAILABLE',
            'message': '请求的产物未确认交付',
        }
        for i in range(len(outputs), target)
    ]
    return {'completed_items': completed, 'incomplete_items': incomplete}
