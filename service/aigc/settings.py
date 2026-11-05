"""服务配置与模型凭据从部署环境加载，不读取用户全局 SDK 配置。"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, model_validator

from ms_agent.config.lightweight import load_lightweight_config
from ms_agent.session.run_store import digest, without_secrets
from ms_agent.tools.api_registry import _resolve_env


def read_secret(name: str) -> str:
    path = os.environ.get(f'{name}_FILE')
    return Path(path).read_text(encoding='utf-8').strip() if path else os.environ.get(name, '')


class ConfigNode(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class ServerSettings(ConfigNode):
    workers: Literal[1] = 1
    host: str = '0.0.0.0'
    port: int = Field(default=8000, ge=1, le=65535)
    max_body_bytes: int = Field(default=2097152, ge=1024, le=20971520)
    keep_alive_timeout: int = Field(default=75, ge=1)
    graceful_shutdown_timeout: int = Field(default=30, ge=1)
    limit_concurrency: int = Field(default=1024, ge=1)
    public_path_prefix: str = ''
    loop: Literal['auto', 'asyncio', 'uvloop'] = 'auto'
    http: Literal['auto', 'h11', 'httptools'] = 'auto'
    access_log: bool = False

    @model_validator(mode='after')
    def prefix(self):
        if self.public_path_prefix and (
            not self.public_path_prefix.startswith('/')
            or '..' in self.public_path_prefix
            or any(c in self.public_path_prefix for c in '?#\\')
        ):
            raise ValueError('public_path_prefix 必须是安全绝对路径前缀')
        self.public_path_prefix = self.public_path_prefix.rstrip('/')
        return self


class AuthSettings(ConfigNode):
    header: str = Field(default='Authorization', pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')
    scheme: Literal['bearer', 'raw'] = 'bearer'


class StorageSettings(ConfigNode):
    data_dir: str = '/data'
    cache_entries: int = Field(default=256, ge=1)
    cache_bytes: int = Field(default=67108864, ge=1024)
    projection: Literal['never', 'terminal', 'always'] = 'terminal'
    lease_wait: float = Field(default=0, ge=0)
    lock_policy: Literal['warn', 'require'] = 'require'
    epoch_check_interval: float = Field(default=5, gt=0)
    io_workers: int = Field(default=8, ge=1, le=64)


class RuntimeSettings(ConfigNode):
    max_active_runs: int = Field(default=4, ge=1, le=1024)
    max_inflight_runs: int = Field(default=104, ge=1, le=10000)
    per_user_active: int = Field(default=4, ge=1)
    per_user_inflight: int = Field(default=104, ge=1)
    interaction_timeout: float = Field(default=0, ge=0)
    drain_timeout: float = Field(default=5, ge=0)
    retry_after: int = Field(default=2, ge=1)
    queue_limit: int | None = Field(default=None, ge=0)

    @model_validator(mode='before')
    @classmethod
    def legacy_queue(cls, data):
        data = deepcopy(data)
        if data.get('queue_limit') == '':
            data['queue_limit'] = None
        return data

    @model_validator(mode='after')
    def effective_capacity(self):
        if self.queue_limit is not None:
            self.max_inflight_runs = self.max_active_runs + self.queue_limit
        return self


class EventSettings(ConfigNode):
    heartbeat_seconds: float = Field(default=15, gt=0)
    max_stream_seconds: float = Field(default=540, gt=0)
    max_subscribers: int = Field(default=512, ge=1)


class RetrySettings(ConfigNode):
    attempts: int = Field(default=3, ge=1, le=10)
    initial: float = Field(default=1, gt=0)
    maximum: float = Field(default=30, gt=0)
    budget: float = Field(default=300, gt=0)


class UpstreamSettings(ConfigNode):
    concurrency: int = Field(default=32, ge=1)
    rpm: int = Field(default=0, ge=0)
    connections: int = Field(default=64, ge=1)
    connect_timeout: float = Field(default=10, gt=0)
    read_timeout: float = Field(default=300, gt=0)
    retry: RetrySettings = Field(default_factory=RetrySettings)


class PollRetrySettings(ConfigNode):
    initial: float = Field(default=1, gt=0)
    maximum: float = Field(default=30, gt=0)
    budget: float = Field(default=600, gt=0)


class PollUpstreamSettings(UpstreamSettings):
    retry: PollRetrySettings = Field(default_factory=PollRetrySettings)


class UpstreamsSettings(ConfigNode):
    llm: UpstreamSettings = Field(default_factory=UpstreamSettings)
    submit: UpstreamSettings = Field(default_factory=lambda: UpstreamSettings(concurrency=8))
    poll: PollUpstreamSettings = Field(default_factory=PollUpstreamSettings)


class HistorySettings(ConfigNode):
    max_messages: int = Field(default=0, ge=0)
    max_chars: int = Field(default=0, ge=0)


class AgentSettings(ConfigNode):
    max_chat_round: int = Field(default=20, ge=1, le=100)
    system_prompt: str = '你是 AIGC 助手。只使用已声明的 HTTP API 工具。'
    history: HistorySettings = Field(default_factory=HistorySettings)
    memory_enabled: bool = True
    memory_projection: bool = False
    memory_top_k: int = Field(default=5, ge=0)
    skill_dirs: list[str] = Field(default_factory=list)
    model: str = 'qwen-plus'
    base_url: str = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1)


class RecipeSettings(ConfigNode):
    concurrency: int = Field(default=1, ge=1, le=16)
    max_inflight: int = Field(default=100, ge=1)
    timeout: float = Field(default=120, gt=0, le=600)


class ObservabilitySettings(ConfigNode):
    metrics_enabled: bool = False
    metrics_interval: float = Field(default=10, gt=0)
    metrics_endpoint: str = 'http://127.0.0.1:8080/api/builtin/realtime_metrics'
    loop_sample_seconds: float = Field(default=0.2, gt=0)
    loop_window_samples: int = Field(default=1024, ge=1)
    metrics_timeout: float = Field(default=3, gt=0)
    json_logs: bool = True


class EASSettings(ConfigNode):
    enabled: bool = False
    gateway: Literal['none', 'shared', 'mse', 'alb', 'nlb'] = 'none'
    gateway_id: str = ''
    service_auth_header: str = Field(default='X-AIGC-Service-Token', pattern=r'^[A-Za-z][A-Za-z0-9_-]*$')
    service_auth_scheme: Literal['raw', 'bearer'] = 'raw'
    service_name: str = 'aigc'
    image: str = ''
    instance: Literal[1] = 1
    cpu: int = Field(default=2, ge=1)
    memory_mb: int = Field(default=4096, ge=1024)
    rpc_keepalive_ms: int = Field(default=600000, ge=1000)
    termination_grace_period: int = Field(default=120, ge=1)
    shutdown_margin: int = Field(default=10, ge=1)
    startup_budget: int = Field(default=300, ge=1)
    nas_server: str = ''
    nas_path: str = '/aigc'
    mount_path: str = '/data'


class ToolAuthSettings(ConfigNode):
    type: Literal['none', 'bearer', 'api_key'] = 'bearer'
    header_name: str = 'Authorization'
    header_prefix: str = 'Bearer'


class ToolEndpointSettings(ConfigNode):
    method: Literal['POST', 'GET'] = 'POST'
    path: str
    async_mode: bool = False
    poll_path: str | None = None
    timeout: float = Field(default=60, gt=0)
    poll_interval: float = Field(default=5, gt=0)
    poll_timeout: float = Field(default=600, gt=0)
    poll_max_interval: float = Field(default=20, gt=0)
    poll_factor: float = Field(default=1.5, ge=1)
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_poll(self):
        if self.async_mode and not self.poll_path:
            raise ValueError('异步工具必须配置 poll_path')
        return self


class ToolOutputSettings(ConfigNode):
    success_path: str | None = None
    error_path: str = 'errors.message'
    task_id_path: str = 'output.task_id'
    status_path: str = 'output.task_status'
    success_statuses: list[str] = Field(default_factory=lambda: ['SUCCEED'])
    failure_statuses: list[str] = Field(default_factory=lambda: ['FAILED', 'TIMEOUT'])


class ToolSettings(ConfigNode):
    name: str
    enabled: bool = True
    requires_approval: bool = True
    description: str
    api: ToolEndpointSettings
    parameters: dict[str, Any]
    output_mapping: ToolOutputSettings
    auth: ToolAuthSettings | None = None


class ToolGroupSettings(ConfigNode):
    base_url: str
    auth: ToolAuthSettings
    tools: list[ToolSettings]


class ToolsSettings(ConfigNode):
    api: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_api(self):
        if self.api:
            group = ToolGroupSettings.model_validate(self.api)
            self.api = group.model_dump(exclude_none=True)
            url = urlparse(group.base_url)
            if url.scheme not in ('http', 'https') or not url.netloc or url.username:
                raise ValueError('工具 API 地址无效')
            from jsonschema import Draft202012Validator

            for tool in group.tools:
                Draft202012Validator.check_schema(tool.parameters)
        return self


class SecretsSettings(ConfigNode):
    service: str = 'AIGC_SERVICE_TOKEN'
    llm: str = 'LLM_API_KEY'
    api: str = 'API_TOKEN'


class Settings(ConfigNode):
    server: ServerSettings = Field(default_factory=ServerSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    events: EventSettings = Field(default_factory=EventSettings)
    upstreams: UpstreamsSettings = Field(default_factory=UpstreamsSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    recipe: RecipeSettings = Field(default_factory=RecipeSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    eas: EASSettings = Field(default_factory=EASSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)
    service_token: SecretStr = Field(default='', exclude=True)
    agent_config: dict[str, Any] = Field(default_factory=dict, repr=False, exclude=True)
    _digest: str | None = PrivateAttr(default=None)
    _environment_overrides: dict[str, str] = PrivateAttr(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def sdk_compatibility(cls, value):
        # 保留程序化 Settings 构造兼容；文件加载不接受旧分散结构。
        data = deepcopy(value)
        aliases = {
            'data_dir': ('storage', 'data_dir'),
            'host': ('server', 'host'),
            'port': ('server', 'port'),
            'max_body_bytes': ('server', 'max_body_bytes'),
            'shutdown_timeout': ('server', 'graceful_shutdown_timeout'),
            'recipe_timeout': ('recipe', 'timeout'),
            'recipe_concurrency': ('recipe', 'concurrency'),
        }
        for old, (section, key) in aliases.items():
            if old in data:
                data.setdefault(section, {})[key] = data.pop(old)
        if 'max_runs' in data or 'queue_limit' in data:
            active = data.pop('max_runs', 4)
            queue = data.pop('queue_limit', 100)
            data.setdefault('runtime', {}).update(max_active_runs=active, max_inflight_runs=active + queue)
        return data

    @model_validator(mode='after')
    def validate_boundaries(self):
        if any(
            not re.fullmatch(r'[A-Z_][A-Z0-9_]*', value)
            for value in (self.secrets.service, self.secrets.llm, self.secrets.api)
        ):
            raise ValueError('凭据引用必须是环境变量名称')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', self.eas.service_name):
            raise ValueError('EAS service_name 必须为字母开头的字母数字下划线')
        if self.runtime.max_inflight_runs < self.runtime.max_active_runs:
            raise ValueError('在途容量不能小于活跃容量')
        for upstream in (self.upstreams.llm, self.upstreams.submit, self.upstreams.poll):
            if upstream.connections < upstream.concurrency:
                raise ValueError('连接池容量不能小于上游并发容量')
        if self.eas.enabled:
            if self.server.host != '0.0.0.0':
                raise ValueError('EAS 容器必须监听 0.0.0.0')
            if self.eas.service_auth_header.lower() == 'authorization':
                raise ValueError('应用服务凭据必须与 EAS 网关 Authorization 分离')
            if self.storage.lock_policy != 'require':
                raise ValueError('EAS 生产模式不允许绕过跨客户端锁校验')
            if self.storage.lease_wait >= self.eas.startup_budget:
                raise ValueError('等待租约时长必须小于启动探针预算')
            if self.server.port in (8080, 9090):
                raise ValueError('EAS 保留 8080/9090 端口')
            if self.eas.gateway in ('mse', 'shared', 'alb'):
                if self.server.max_body_bytes > 1048576:
                    raise ValueError('七层网关请求体上限按 1MiB 配置')
                if self.events.max_stream_seconds * 1000 >= self.eas.rpc_keepalive_ms:
                    raise ValueError('SSE 单流时长必须小于网关请求超时')
            idle = {'mse': 30, 'alb': 15, 'nlb': 900}.get(self.eas.gateway, 30)
            if self.server.keep_alive_timeout <= idle:
                raise ValueError('后端 keep-alive 必须大于网关回源空闲超时')
            budget = self.runtime.drain_timeout + self.server.graceful_shutdown_timeout + self.eas.shutdown_margin
            if budget > self.eas.termination_grace_period:
                raise ValueError('退出预算超过 EAS termination_grace_period')
        return self

    def effective_config(self) -> dict[str, Any]:
        data = self.model_dump(mode='json')
        data['resolved_agent'] = self.agent_config
        return without_secrets(data)

    @property
    def config_digest(self) -> str:
        if self._digest is None:
            self._digest = digest(self.effective_config())
        return self._digest

    @property
    def data_dir(self):
        return self.storage.data_dir

    @property
    def max_runs(self):
        return self.runtime.max_active_runs

    @property
    def queue_limit(self):
        return self.runtime.max_inflight_runs - self.runtime.max_active_runs

    @property
    def host(self):
        return self.server.host

    @property
    def port(self):
        return self.server.port

    @property
    def shutdown_timeout(self):
        return self.server.graceful_shutdown_timeout

    @property
    def max_body_bytes(self):
        return self.server.max_body_bytes

    @max_body_bytes.setter
    def max_body_bytes(self, value):
        self.server.max_body_bytes = value

    @property
    def recipe_timeout(self):
        return self.recipe.timeout

    @property
    def recipe_concurrency(self):
        return self.recipe.concurrency

    @classmethod
    def from_env(cls, *, deployment: bool = False) -> 'Settings':
        path = Path(os.environ.get('AIGC_CONFIG', Path(__file__).parent / 'configs' / 'service.yaml'))
        if 'API_TOOLS_CONFIG' in os.environ:
            raise ValueError('移除 API_TOOLS_CONFIG，改用统一 service.yaml 的 tools.api')
        if 'AIGC_QUEUE_LIMIT' in os.environ and 'AIGC_MAX_INFLIGHT' in os.environ:
            raise ValueError('AIGC_QUEUE_LIMIT 与 AIGC_MAX_INFLIGHT 不能同时覆盖容量')
        source = path.read_text(encoding='utf-8')
        raw = _resolve_env(yaml.safe_load(source) or {})
        if {'service', 'agent_config', 'service_token'} & raw.keys():
            raise ValueError('服务配置必须使用统一配置树，不接受旧 service/agent_config 或明文密钥')
        effective = cls.model_validate(raw)
        references = {effective.secrets.service, effective.secrets.llm, effective.secrets.api}
        variables = set(re.findall(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}', source))
        if references & variables:
            raise ValueError('密钥只能通过 secrets 引用读取，不允许作为普通 YAML 占位符展开')
        effective._environment_overrides = {name: os.environ[name] for name in variables if name in os.environ}
        effective.service_token = SecretStr('' if deployment else read_secret(effective.secrets.service))
        if not deployment and not effective.service_token.get_secret_value():
            raise ValueError('服务凭据未配置')
        config = {
            'strict': True,
            'agent': {
                'mode': 'lightweight',
                'max_chat_round': effective.agent.max_chat_round,
                'system_prompt': effective.agent.system_prompt,
                'history': effective.agent.history.model_dump(),
                'memory_top_k': effective.agent.memory_top_k,
            },
            'llm': {
                'transport': 'http',
                'model': effective.agent.model,
                'base_url': effective.agent.base_url,
                'api_key': '' if deployment else read_secret(effective.secrets.llm),
                'temperature': effective.agent.temperature,
                'max_tokens': effective.agent.max_tokens,
                'timeout': effective.upstreams.llm.read_timeout,
                'upstream': effective.upstreams.llm.model_dump(),
            },
            'session': {
                'base_dir': effective.data_dir,
                'optimized': True,
                'cache_entries': effective.storage.cache_entries,
                'cache_bytes': effective.storage.cache_bytes,
            },
            'memory': {
                'base_dir': effective.data_dir,
                'storage_backend': 'file',
                'scope': 'session',
                'enabled': effective.agent.memory_enabled,
                'projection': effective.agent.memory_projection,
                'top_k': effective.agent.memory_top_k,
            },
            'skills': {'enabled': bool(effective.agent.skill_dirs), 'dirs': effective.agent.skill_dirs},
            'tools': {
                'builtin_enabled': False,
                'api_tools_config': '',
                'api': deepcopy(effective.tools.api),
                'upstreams': {
                    'submit': effective.upstreams.submit.model_dump(),
                    'poll': effective.upstreams.poll.model_dump(),
                },
            },
        }
        # 在内存中注入 token，不把 *_FILE 内容复制到环境或冻结快照。
        api_token = '' if deployment else read_secret(effective.secrets.api)
        if config['tools']['api']:
            config['tools']['api'].setdefault('auth', {}).update(token=api_token, token_env=None)
        if deployment:
            effective.agent_config = config
        else:
            effective.agent_config = load_lightweight_config(config, strict=True)
        for key in ('base_url',):
            url = urlparse(config['llm'][key])
            if url.scheme not in ('http', 'https') or not url.netloc or url.username:
                raise ValueError('LLM 地址无效')
        return effective
