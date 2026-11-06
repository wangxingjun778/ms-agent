"""冻结 Agentic 输入及能力约束，不读取业务数据库或动态会话记忆。"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Protocol

from jsonschema import Draft202012Validator

from ms_agent.agent.protocol import PROTOCOL_VERSION
from ms_agent.session.run_store import canonical, digest, without_secrets


class CapabilityError(ValueError):
    def __init__(self, message, code='CAPABILITY_NOT_SUPPORTED'):
        super().__init__(message)
        self.code = code


class ConnectionResolver(Protocol):
    async def resolve(self, user_id: str, connection_id: str) -> dict: ...


class AssetResolver(Protocol):
    async def resolve(self, user_id: str, resource_ref: str, revision: str) -> dict: ...


def validate_asset(ref, snapshot, limit):
    for key in ('type', 'resource_ref', 'revision'):
        if snapshot.get(key) != ref[key]:
            raise CapabilityError('资产 revision/引用不一致')
    content = snapshot['content']
    if digest(content) != snapshot['content_digest'] or len(canonical(content).encode()) > limit:
        raise CapabilityError('资产摘要或大小无效')
    if ref['type'] == 'skill':
        if set(content) - {'instructions', 'variables_schema'} or not isinstance(content.get('instructions'), str):
            raise CapabilityError('Skill 只接受声明式说明及变量 Schema')
    else:
        required = {'schema_version', 'prompt_template', 'variables_schema', 'parameters'}
        if (
            not required.issubset(content)
            or content['schema_version'] != PROTOCOL_VERSION
            or not isinstance(content['prompt_template'], str)
            or not isinstance(content['parameters'], dict)
            or not isinstance(content.get('negative_prompt_template', ''), str)
        ):
            raise CapabilityError('Recipe 资产必须符合当前声明式复用格式')
        if set(content) - required - {'negative_prompt_template', 'dependencies'}:
            raise CapabilityError('Recipe 资产含未知执行字段')
    schema = content.get('variables_schema', {'type': 'object', 'additionalProperties': False})
    try:
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            raise CapabilityError('资产变量必须使用 object Schema')

        def check_refs(value):
            if isinstance(value, dict):
                if '$ref' in value and not str(value['$ref']).startswith('#'):
                    raise CapabilityError('资产 Schema 不允许远程引用')
                if '$id' in value or '$dynamicRef' in value:
                    raise CapabilityError('资产 Schema 不允许外部解析入口')
                for item in value.values():
                    check_refs(item)
            elif isinstance(value, list):
                for item in value:
                    check_refs(item)

        check_refs(schema)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(ref['variables'])
    except Exception as exc:
        raise CapabilityError('资产变量 Schema 或变量值无效') from exc
    return deepcopy(content)


def prepare_snapshot(request, config, registry, *, resolved_assets=None):
    snapshot = deepcopy(request['input_snapshot'])
    effective = snapshot['effective']
    capabilities = config['capabilities']
    limits = capabilities['context']
    if effective['model_source'] == 'byok':
        # 连接获取和受控路由不能靠客户端声明实现；具体 transport 尚未配置时失败关闭。
        raise CapabilityError('BYOK ConnectionResolver 尚未配置', 'DEPENDENCY_UNAVAILABLE')
    models = capabilities['models']
    selection = effective.get('custom_selection')
    allowed = effective['preferences']['enabled_model_refs'] or [
        name for name, model in models.items() if model['modality'] == effective['modality']
    ]
    if selection:
        allowed = [selection['model_ref']]
    if not allowed or any(name not in models or models[name]['modality'] != effective['modality'] for name in allowed):
        raise CapabilityError('模型或媒体能力不在服务白名单')
    selected = {name: deepcopy(models[name]) for name in allowed}
    if any(effective['action'] not in model['actions'] for model in selected.values()):
        raise CapabilityError('模型不支持当前操作')
    if effective['preferences']['auto_evaluate']:
        raise CapabilityError('自动评估工具未开放')
    if effective['action'] == 'regenerate':
        outputs = snapshot['source_outputs']
        if (
            len(outputs) != 1
            or not isinstance(outputs[0].get('actual_generation'), dict)
            or not {'prompt', 'model_ref'}.issubset(outputs[0]['actual_generation'])
        ):
            raise CapabilityError('换一批需要一个具有已确认实际提示词和模型的来源产物')
    for model in selected.values():
        if not registry.has_tool(model['tool_name']):
            raise CapabilityError('工具尚未配置')
    assets = snapshot['asset_snapshots'] if resolved_assets is None else resolved_assets
    by_ref = {(a['type'], a['resource_ref'], a['revision']): a for a in assets}
    if len(by_ref) != len(assets):
        raise CapabilityError('资产重复')
    if len(assets) != len(snapshot['asset_refs']):
        raise CapabilityError('资产必须提供完整固定快照', 'DEPENDENCY_UNAVAILABLE')
    instructions = []
    for ref in snapshot['asset_refs']:
        key = (ref['type'], ref['resource_ref'], ref['revision'])
        if key not in by_ref:
            raise CapabilityError('无法解析指定资产', 'DEPENDENCY_UNAVAILABLE')
        content = validate_asset(ref, by_ref[key], limits['max_asset_bytes'])
        text = content.get('instructions', content.get('prompt_template', ''))
        text = re.sub(
            r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}',
            lambda match: str(ref['variables'][match[1]]) if match[1] in ref['variables'] else match[0],
            text,
        )
        instructions.append(text)
        if ref['type'] == 'recipe' and content['parameters']:
            raise CapabilityError('Recipe 参数必须由后端渲染为 effective，不能暗中覆盖固定输入')
    history = []
    for message in snapshot['context']:
        text = '\n'.join(block['text'] for block in message['content'])
        if message['output_refs']:
            text += '\n已提交产物引用：' + canonical(message['output_refs'])
        history.append({'role': message['role'], 'content': text})
    prompt = effective['prompt']
    if snapshot['input_refs']:
        prompt += '\n参考素材（按原顺序）：\n' + '\n'.join(media['url'] for media in snapshot['input_refs'])
    system = config['agent']['system_prompt']
    system += '\n冻结约束（必须遵守，不能静默替换）：' + canonical(effective)
    if instructions:
        system += '\n已授权固定资产：\n' + '\n'.join(instructions)
    if snapshot['memory_snapshot']:
        system += '\n冻结参考记忆：\n' + '\n'.join(snapshot['memory_snapshot'])
    messages = [{'role': 'system', 'content': system}, *history, {'role': 'user', 'content': prompt}]
    if len(history) > limits['max_messages'] or len(canonical(messages)) > limits['max_chars']:
        raise CapabilityError('显式上下文超过服务预算，未自动裁剪', 'CONTEXT_TOO_LARGE')
    prepared = {
        'input_snapshot': snapshot,
        'messages': messages,
        'models': selected,
        'agent': deepcopy(config['agent']),
        'llm': without_secrets(config['llm']),
        'tools': without_secrets(config['tools']),
        'mode': effective['generation_mode'],
    }
    if selection:
        model = selected[selection['model_ref']]
        args = fixed_arguments(effective, snapshot, model)
        Draft202012Validator(registry.parameters(model['tool_name'])).validate(args)
        prepared['fixed_tool'] = {'name': model['tool_name'], 'arguments': args}
    else:
        # 明确的生成偏好必须在每个允许模型中有可执行映射。
        for model in selected.values():
            args = fixed_arguments(effective, snapshot, model)
            Draft202012Validator(registry.parameters(model['tool_name'])).validate(args)
    allowed_tools = {model['tool_name'] for model in selected.values()}
    if prepared['tools'].get('api'):
        prepared['tools']['api']['tools'] = [
            tool for tool in prepared['tools']['api']['tools'] if tool['name'] in allowed_tools
        ]
    return prepared


def fixed_arguments(effective, snapshot, model):
    preferences = effective['preferences']
    mapping = model['parameter_map']
    args = {'model': model['model_id'], 'prompt': effective['prompt']}
    if effective['modality'] == 'video' and preferences['image_count'] != 1:
        raise CapabilityError('视频模式不接受 image_count')
    values = {
        'negative_prompt': effective['negative_prompt'],
        'image_count': preferences['image_count'] if effective['modality'] == 'image' else None,
        'seed': preferences['seed'],
        'video_duration': preferences['video_duration'],
    }
    for key, value in values.items():
        if value is None or (key == 'negative_prompt' and value == ''):
            continue
        if key not in mapping:
            raise CapabilityError(f'模型不支持参数 {key}')
        args[mapping[key]] = value
    ratio, resolution = preferences['aspect_ratio'], preferences['resolution']
    if ratio or resolution:
        size = model['sizes'].get((ratio or '') + '|' + (resolution or ''))
        if size is None:
            raise CapabilityError('比例/清晰度组合没有已验证映射')
        args['size'] = size
    inputs = snapshot['input_refs']
    if inputs:
        if 'input_refs' not in mapping or any(media['type'] != 'image' for media in inputs):
            raise CapabilityError('模型不支持这些输入素材')
        args[mapping['input_refs']] = [media['url'] for media in inputs]
    custom = effective['custom_selection']
    if custom:
        for key, value in custom['params'].items():
            if key not in mapping:
                raise CapabilityError(f'模型不支持参数 {key}')
            args[mapping[key]] = value
        if custom['loras']:
            if 'loras' not in mapping:
                raise CapabilityError('模型未开放 LoRA 映射')
            args[mapping['loras']] = {item['model_ref']: item['weight'] for item in custom['loras']}
    return args


def constrain_arguments(prepared, name, args):
    snapshot = prepared['input_snapshot']
    effective = snapshot['effective']
    model = next(
        (m for m in prepared['models'].values() if m['tool_name'] == name and m['model_id'] == args.get('model')), None
    )
    if model is None:
        raise CapabilityError('规划器选择了未授权模型或工具')
    fixed = fixed_arguments(effective, snapshot, model)
    # Agent 可规划 prompt，但不改变明确模型范围、素材集合和其他冻结参数。
    for key, value in fixed.items():
        if key != 'prompt':
            args[key] = deepcopy(value)
    if prepared['mode'] == 'custom':
        return deepcopy(prepared['fixed_tool']['arguments'])
    if not isinstance(args.get('prompt'), str) or not args['prompt']:
        raise CapabilityError('规划器未提供有效 prompt')
    permitted = set(model['parameter_map'].values()) | {'model', 'prompt', 'size'}
    if set(args) - permitted:
        raise CapabilityError('规划器提交未开放参数')
    return args
