"""不创建 MCP/plugin 管理器的轻量配置加载。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from ms_agent.tools.api_registry import _resolve_env


def merge_config(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_lightweight_config(patch: dict[str, Any] | None = None, *, strict: bool = False) -> dict[str, Any]:
    defaults = yaml.safe_load(Path(__file__).with_name('lightweight.yaml').read_text(encoding='utf-8'))
    config = _resolve_env(merge_config(defaults, patch or {}))
    if strict:
        if config['agent']['mode'] != 'lightweight' or config['tools']['builtin_enabled']:
            raise ValueError('AIGC 服务只允许 lightweight 与 API tools')
        if any(config.get('modules', {}).values()):
            raise ValueError('AIGC 服务禁止启用非核心模块')
        if config['memory'].get('storage_backend') != 'file':
            raise ValueError('AIGC 服务只允许文件 memory')
        if config['llm'].get('transport') != 'http':
            raise ValueError('AIGC 服务必须显式选择 HTTP LLM transport')
        url = urlparse(config['llm']['base_url'])
        if url.scheme not in {'http', 'https'} or not url.netloc or url.username:
            raise ValueError('LLM 地址无效')
        if not config['llm'].get('model') or not config['llm'].get('api_key'):
            raise ValueError('LLM 模型或凭据缺失')
    return config
