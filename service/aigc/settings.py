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

from ms_agent.session.run_store import digest, without_secrets
from ms_agent.tools.api_registry import _resolve_env

ENV_PREFIX = 'AGENTIC_AIGC_'

# 环境变量重命名后不接受别名：旧名为 AIGC_* 时失败关闭，避免静默回退到默认值。
RENAMED_ENVIRONMENTS = {
    'AIGC_HOST': 'AGENTIC_AIGC_HOST',
    'AIGC_PORT': 'AGENTIC_AIGC_PORT',
    'AIGC_PUBLIC_PATH_PREFIX': 'AGENTIC_AIGC_PUBLIC_PATH_PREFIX',
    'AIGC_AUTH_HEADER': 'AGENTIC_AIGC_AUTH_HEADER',
    'AIGC_AUTH_SCHEME': 'AGENTIC_AIGC_AUTH_SCHEME',
    'AIGC_MAX_RUNS': 'AGENTIC_AIGC_MAX_RUNS',
    'AIGC_MAX_INFLIGHT': 'AGENTIC_AIGC_MAX_INFLIGHT',
    'AIGC_DRAIN_TIMEOUT': 'AGENTIC_AIGC_DRAIN_TIMEOUT',
    'AIGC_METRICS_ENABLED': 'AGENTIC_AIGC_METRICS_ENABLED',
    'AIGC_EAS_ENABLED': 'AGENTIC_AIGC_EAS_ENABLED',
    'AIGC_EAS_GATEWAY': 'AGENTIC_AIGC_EAS_GATEWAY',
    'AIGC_CONFIG': 'AGENTIC_AIGC_CONFIG',
    'AIGC_SERVICE_TOKEN': 'AGENTIC_AIGC_SERVICE_TOKEN',
}


def read_secret(name: str) -> str:
    path = os.environ.get(f'{name}_FILE')
    return Path(path).read_text(encoding='utf-8').strip() if path else os.environ.get(name, '')


class ConfigNode(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class ServerSettings(ConfigNode):
    workers: Literal[1] = 1
    host: str = '0.0.0.0'
    port: int = Field(default=8000, ge=1, le=65535)
    max_body_bytes: int = Field(default=1048576, ge=1024, le=20971520)
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
    max_active_runs: int = Field(default=64, ge=1, le=1024)
    max_inflight_runs: int = Field(default=500, ge=1, le=10000)
    per_user_active: int = Field(default=4, ge=1)
    per_user_inflight: int = Field(default=20, ge=1)
    interaction_timeout: float = Field(default=1800, ge=0)
    drain_timeout: float = Field(default=60, ge=0)
    retry_after: int = Field(default=2, ge=1)


class EventSettings(ConfigNode):
    heartbeat_seconds: float = Field(default=15, gt=0)
    max_stream_seconds: float = Field(default=540, gt=0)
    max_subscribers: int = Field(default=512, ge=1)
    batch_size: int = Field(default=128, ge=1, le=1024)


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
    settlement: PollUpstreamSettings = Field(default_factory=lambda: PollUpstreamSettings(concurrency=4, connections=8))


class ContextLimits(ConfigNode):
    max_messages: int = Field(default=200, ge=1)
    max_chars: int = Field(default=60000, ge=1)
    max_asset_bytes: int = Field(default=262144, ge=1024)


class ModelCapability(ConfigNode):
    model_id: str
    modality: Literal['image', 'video']
    tool_name: str
    actions: list[Literal['generate', 'continue_edit', 'regenerate', 'change_reference', 'rethink']]
    parameter_map: dict[str, str]
    sizes: dict[str, str] = Field(default_factory=dict)
    output_type: Literal['image', 'video'] = 'image'
    confirmed_path: str | None = None
    poster_path: str | None = None

    @model_validator(mode='after')
    def validate_mapping(self):
        if self.output_type != self.modality:
            raise ValueError('模型输出类型必须与 modality 一致')
        targets = list(self.parameter_map.values())
        if len(targets) != len(set(targets)) or {'model', 'prompt', 'size'} & set(targets):
            raise ValueError('参数映射不能重复或覆盖模型/提示词/尺寸')
        return self


class CapabilitiesSettings(ConfigNode):
    context: ContextLimits = Field(default_factory=ContextLimits)
    models: dict[str, ModelCapability] = Field(default_factory=dict)
    asset_transport: Literal['frozen_snapshot'] = 'frozen_snapshot'
    connection_transport: Literal['disabled'] = 'disabled'


class SettlementSettings(ConfigNode):
    concurrency: int = Field(default=8, ge=1)
    budget: float = Field(default=600, gt=0)
    query_timeout: float = Field(default=60, gt=0)
    retry_interval: float = Field(default=5, gt=0)
    cancel_enabled: Literal[False] = False


class AgentSettings(ConfigNode):
    """只保存服务端必须决定的规划参数，采样参数用上游模型默认值。"""

    max_chat_round: int = Field(default=20, ge=1, le=100)
    system_prompt: str = '你是 AIGC 助手。只使用已声明的 HTTP API 工具。'
    model: str = 'qwen-plus'
    base_url: str = 'https://dashscope.aliyuncs.com/compatible-mode/v1'


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


class LocalMockSettings(ConfigNode):
    llm_delay: float = Field(default=0.05, ge=0)
    task_delay: float = Field(default=0.2, gt=0)
    slow_task_delay: float = Field(default=2, gt=0)
    unknown_response_delay: float = Field(default=10, gt=0)
    tool_timeout: float = Field(default=0.5, gt=0)
    poll_interval: float = Field(default=0.1, gt=0)
    interaction_timeout: float = Field(default=3, gt=0)
    settlement_budget: float = Field(default=5, gt=0)


class LocalDeploymentSettings(ConfigNode):
    """仅影响本地生成物，不改变 EAS 运行参数或对外协议。"""

    profile: Literal['mock', 'live'] = 'mock'
    project_name: str = Field(default='agentic-aigc-local', pattern=r'^[a-z][a-z0-9-]*$')
    work_dir: str = 'temp/agentic_aigc/local_deploy'
    # live 服务、mock 服务、mock 上游各占一个端口，mock 与 live 可同时运行。
    host_port: int = Field(default=18080, ge=1024, le=65535)
    mock_service_host_port: int = Field(default=18082, ge=1024, le=65535)
    mock_host_port: int = Field(default=18081, ge=1024, le=65535)
    image: str = 'ms-agent-aigc:local'
    platform: Literal['linux/amd64', 'linux/arm64'] = 'linux/amd64'
    cpu: int = Field(default=2, ge=1)
    memory_mb: int = Field(default=4096, ge=512)
    test_timeout: float = Field(default=30, gt=0)
    restart_test: bool = True
    mock: LocalMockSettings = Field(default_factory=LocalMockSettings)

    @model_validator(mode='after')
    def validate_ports(self):
        ports = (self.host_port, self.mock_service_host_port, self.mock_host_port)
        if len(set(ports)) != len(ports):
            raise ValueError('live 服务、mock 服务与 mock 上游端口不能相同')
        return self


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
    service: str = f'{ENV_PREFIX}SERVICE_TOKEN'
    llm: str = f'{ENV_PREFIX}LLM_API_KEY'
    api: str = f'{ENV_PREFIX}API_TOKEN'


class ServiceCredentials(ConfigNode):
    """部署凭据与运行配置分离，禁止进入 YAML 与配置摘要。"""

    service: SecretStr
    llm: SecretStr
    api: SecretStr


class Settings(ConfigNode):
    server: ServerSettings = Field(default_factory=ServerSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    events: EventSettings = Field(default_factory=EventSettings)
    upstreams: UpstreamsSettings = Field(default_factory=UpstreamsSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    recipe: RecipeSettings = Field(default_factory=RecipeSettings)
    capabilities: CapabilitiesSettings = Field(default_factory=CapabilitiesSettings)
    settlement: SettlementSettings = Field(default_factory=SettlementSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    eas: EASSettings = Field(default_factory=EASSettings)
    local: LocalDeploymentSettings = Field(default_factory=LocalDeploymentSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    secrets: SecretsSettings = Field(default_factory=SecretsSettings)
    _credentials: ServiceCredentials | None = PrivateAttr(default=None)
    _digest: str | None = PrivateAttr(default=None)
    _environment_overrides: dict[str, str] = PrivateAttr(default_factory=dict)

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
        for upstream in (self.upstreams.llm, self.upstreams.submit, self.upstreams.poll, self.upstreams.settlement):
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
        return without_secrets(self.model_dump(mode='json'))

    @property
    def config_digest(self) -> str:
        if self._digest is None:
            self._digest = digest(self.effective_config())
        return self._digest

    @property
    def credentials(self) -> ServiceCredentials:
        if self._credentials is None:
            raise RuntimeError('服务凭据尚未加载')
        return self._credentials

    def bind_credentials(self, credentials: ServiceCredentials) -> 'Settings':
        self._credentials = credentials
        return self

    @classmethod
    def load(
        cls, path: str | Path | None = None, *, deployment: bool = False, credentials: ServiceCredentials | None = None
    ) -> 'Settings':
        path = Path(path or os.environ.get('AGENTIC_AIGC_CONFIG', Path(__file__).parent / 'configs' / 'service.yaml'))
        obsolete = {'API_TOOLS_CONFIG', 'AIGC_QUEUE_LIMIT'} & os.environ.keys()
        if obsolete:
            raise ValueError(f'已移除的配置入口：{", ".join(sorted(obsolete))}')
        renamed = sorted(
            f'{old} → {new}' for old, new in RENAMED_ENVIRONMENTS.items() if old in os.environ and new not in os.environ
        )
        if renamed:
            raise ValueError(f'环境变量已重命名为 {ENV_PREFIX}*：{", ".join(renamed)}')
        source = path.read_text(encoding='utf-8')
        raw = _resolve_env(yaml.safe_load(source) or {})
        effective = cls.model_validate(raw)
        references = {effective.secrets.service, effective.secrets.llm, effective.secrets.api}
        variables = set(re.findall(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}', source))
        if references & variables:
            raise ValueError('密钥只能通过 secrets 引用读取，不允许作为普通 YAML 占位符展开')
        effective._environment_overrides = {name: os.environ[name] for name in variables if name in os.environ}
        if not deployment:
            effective.bind_credentials(
                credentials
                or ServiceCredentials(
                    service=read_secret(effective.secrets.service),
                    llm=read_secret(effective.secrets.llm),
                    api=read_secret(effective.secrets.api),
                )
            )
            if not effective.credentials.service.get_secret_value():
                raise ValueError('服务凭据未配置')
            effective.to_agent_config()
        return effective

    def to_agent_config(self) -> dict[str, Any]:
        """只从已校验的新配置树派生 SDK 参数，不加载另一份默认 YAML。"""
        credentials = self.credentials
        if not credentials.llm.get_secret_value():
            raise ValueError('LLM 凭据未配置')
        if self.tools.api and self.tools.api['auth']['type'] != 'none' and not credentials.api.get_secret_value():
            raise ValueError('工具 API 凭据未配置')
        config = {
            'strict': True,
            'agent': {
                'mode': 'lightweight',
                'max_chat_round': self.agent.max_chat_round,
                'system_prompt': self.agent.system_prompt,
            },
            'llm': {
                'transport': 'http',
                'model': self.agent.model,
                'base_url': self.agent.base_url,
                'api_key': credentials.llm.get_secret_value(),
                'timeout': self.upstreams.llm.read_timeout,
                'upstream': self.upstreams.llm.model_dump(),
            },
            'session': {
                'base_dir': self.storage.data_dir,
                'cache_entries': self.storage.cache_entries,
                'cache_bytes': self.storage.cache_bytes,
            },
            'memory': {
                'base_dir': self.storage.data_dir,
                'storage_backend': 'file',
                'scope': 'session',
                'enabled': False,
                'projection': False,
            },
            'skills': {'enabled': False, 'dirs': []},
            'capabilities': self.capabilities.model_dump(),
            'settlement': self.settlement.model_dump(),
            'tools': {
                'builtin_enabled': False,
                'api': deepcopy(self.tools.api),
                'upstreams': {
                    'submit': self.upstreams.submit.model_dump(),
                    'poll': self.upstreams.poll.model_dump(),
                    'settlement': self.upstreams.settlement.model_dump(),
                },
            },
        }
        # 在内存中注入 token，不把 *_FILE 内容复制到环境或冻结快照。
        if config['tools']['api']:
            config['tools']['api']['auth']['token'] = credentials.api.get_secret_value()
        url = urlparse(config['llm']['base_url'])
        if url.scheme not in ('http', 'https') or not url.netloc or url.username:
            raise ValueError('LLM 地址无效')
        return config
