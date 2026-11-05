"""服务配置与模型凭据从部署环境加载，不读取用户全局 SDK 配置。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ms_agent.config.lightweight import load_lightweight_config


def read_secret(name: str) -> str:
    path = os.environ.get(f'{name}_FILE')
    return Path(path).read_text(encoding='utf-8').strip() if path else os.environ.get(name, '')


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    data_dir: str = '/data'
    host: str = '0.0.0.0'
    port: int = Field(default=8000, ge=1, le=65535)
    max_runs: int = Field(default=4, ge=1, le=64)
    queue_limit: int = Field(default=100, ge=0, le=10000)
    recipe_timeout: float = Field(default=120, gt=0, le=600)
    recipe_concurrency: int = Field(default=1, ge=1, le=16)
    shutdown_timeout: int = Field(default=30, ge=1, le=120)
    max_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    service_token: SecretStr
    agent_config: dict[str, Any] = Field(repr=False)

    @classmethod
    def from_env(cls) -> 'Settings':
        config_path = Path(os.environ.get('AIGC_CONFIG', Path(__file__).parent / 'configs' / 'service.yaml'))
        raw = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
        service = raw.get('service', {})
        for field, env in (
            ('data_dir', 'DATA_DIR'),
            ('host', 'AIGC_HOST'),
            ('port', 'AIGC_PORT'),
            ('max_runs', 'AIGC_MAX_RUNS'),
            ('queue_limit', 'AIGC_QUEUE_LIMIT'),
        ):
            if env in os.environ:
                service[field] = os.environ[env]
        data_dir = str(service.get('data_dir', '/data'))
        agent = raw.get('agent_config', {})
        llm = agent.setdefault('llm', {})
        llm['api_key'] = read_secret('LLM_API_KEY')
        tools_path = Path(os.environ.get('API_TOOLS_CONFIG', config_path.parent / 'api_tools.yaml')).absolute()
        agent.setdefault('tools', {})['api_tools_config'] = str(tools_path)
        agent.setdefault('session', {})['base_dir'] = data_dir
        agent.setdefault('memory', {}).update(base_dir=data_dir, scope='session')
        api_token = read_secret('API_TOKEN')
        if api_token:
            # 用密钥文件时也提供给只在调用阶段读取环境的 AuthConfig。
            os.environ['API_TOKEN'] = api_token
        config = load_lightweight_config(agent, strict=True)
        config['strict'] = True
        token = read_secret('AIGC_SERVICE_TOKEN')
        if not token:
            raise ValueError('AIGC_SERVICE_TOKEN 未配置')
        return cls(**service, service_token=token, agent_config=config)
