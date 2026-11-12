"""从统一 service.yaml 派生 EAS 部署 JSON，不创建云端服务或计费资源。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from ms_agent.agent.protocol import API_PREFIX
from service.aigc.settings import Settings

METRIC_NAMES = ('inflight', 'active', 'waiting', 'active_ratio', 'llm_inflight', 'sse_subscribers')


def render_eas(settings: Settings, shard: int) -> dict:
    if shard < 0:
        raise ValueError('shard 必须非负')
    eas = settings.eas
    if not eas.image or not eas.nas_server:
        raise ValueError('先在 service.yaml 中填写 eas.image 和 eas.nas_server')
    if eas.gateway in ('mse', 'alb') and not eas.gateway_id:
        raise ValueError('专属网关需要 eas.gateway_id')
    if eas.gateway == 'nlb':
        raise ValueError('当前生成器不自动配置 NLB 接入，请单独审批部署方式')
    name = f'{eas.service_name}_{shard}'
    prefix = f'/api/predict/{name}'
    data_dir = eas.mount_path
    effective = settings.model_copy(deep=True)
    effective.eas.enabled = True
    effective.eas.gateway = eas.gateway if eas.gateway != 'none' else 'shared'
    effective.server.public_path_prefix = prefix
    effective.storage.data_dir = data_dir
    effective.auth.header, effective.auth.scheme = eas.service_auth_header, eas.service_auth_scheme
    Settings.model_validate(effective.model_dump())

    def probe(path: str, *, startup=False):
        return {
            'http_get': {'path': path, 'port': settings.server.port},
            'period_seconds': 10,
            'timeout_seconds': 3,
            'success_threshold': 1,
            'failure_threshold': math.ceil(eas.startup_budget / 10) if startup else 3,
        }

    env = {
        **settings._environment_overrides,
        'AGENTIC_AIGC_DATA_DIR': data_dir,
        'AGENTIC_AIGC_PUBLIC_PATH_PREFIX': prefix,
        'AGENTIC_AIGC_AUTH_HEADER': effective.auth.header,
        'AGENTIC_AIGC_AUTH_SCHEME': effective.auth.scheme,
        'AGENTIC_AIGC_EAS_ENABLED': 'true',
        'AGENTIC_AIGC_EAS_GATEWAY': effective.eas.gateway,
        # Pod 与生成器读取同一份 YAML，配置挂载与可写数据目录分离。
        'AGENTIC_AIGC_CONFIG': '/aigc-config/service.yaml',
        'AGENTIC_AIGC_METRICS_ENABLED': 'true',
    }
    # 密钥由部署方提供在独立、只读的目录中，绝不将真实凭据渲染进 JSON。
    for secret in (settings.secrets.service, settings.secrets.llm, settings.secrets.api):
        env[f'{secret}_FILE'] = f'/aigc-secrets/{secret}'
    metadata = {
        'instance': 1,
        'cpu': eas.cpu,
        'memory': eas.memory_mb,
        'rpc': {'enable_sigterm': True},
        'rolling_strategy': {'max_surge': 0, 'max_unavailable': 1},
        'eas.termination_grace_period': eas.termination_grace_period,
    }
    if effective.eas.gateway != 'alb':
        metadata['rpc']['keepalive'] = eas.rpc_keepalive_ms
    deployment = {
        'name': name,
        'metadata': metadata,
        'containers': [
            {
                'image': eas.image,
                'command': '/opt/venv/bin/python -m service.aigc',
                'port': settings.server.port,
                'env': [{'name': key, 'value': value} for key, value in env.items()],
                'startup_check': probe('/health/ready', startup=True),
                'health_check': probe('/health/ready'),
                'liveness_check': probe('/health/live'),
            }
        ],
        'storage': [
            {
                'nfs': {
                    'server': eas.nas_server,
                    'path': f'{eas.nas_path.rstrip("/")}/shard-{shard}',
                    'readOnly': False,
                },
                'mount_path': eas.mount_path,
            },
            {
                'nfs': {'server': eas.nas_server, 'path': f'{eas.nas_path.rstrip("/")}/config', 'readOnly': True},
                'mount_path': '/aigc-config',
            },
            {
                'nfs': {'server': eas.nas_server, 'path': f'{eas.nas_path.rstrip("/")}/secrets', 'readOnly': True},
                'mount_path': '/aigc-secrets',
            },
        ],
        'metrics': [{'name': name} for name in METRIC_NAMES],
    }
    if eas.gateway_id:
        deployment['networking'] = {'gateway': eas.gateway_id}
    return deployment


def render_local(settings: Settings, workspace: str | Path) -> dict:
    """从同源配置生成独立本地环境；不会把本地 override 写回 EAS 配置。"""
    root = Path(workspace).resolve()
    local = settings.local
    directory = (root / local.work_dir).resolve()
    if not directory.is_relative_to(root):
        raise ValueError('本地生成目录必须在仓库工作区内')
    directory = directory / local.profile
    effective = settings.model_copy(deep=True)
    effective.eas.enabled, effective.eas.gateway = False, 'none'
    effective.observability.metrics_enabled = False
    effective.server.host = '0.0.0.0'
    effective.server.public_path_prefix = ''
    effective.storage.data_dir = '/data'
    if local.profile == 'mock':
        options = local.mock
        effective.agent.base_url = 'http://mock:8000/v1'
        effective.tools.api['base_url'] = 'http://mock:8000'
        effective.runtime.interaction_timeout = options.interaction_timeout
        effective.events.heartbeat_seconds = min(options.interaction_timeout / 2, 1)
        effective.settlement.budget = options.settlement_budget
        effective.settlement.query_timeout = options.tool_timeout
        effective.settlement.retry_interval = options.poll_interval
        for upstream in (effective.upstreams.llm, effective.upstreams.submit):
            upstream.retry.initial, upstream.retry.maximum = 0.01, 0.05
        for tool in effective.tools.api.get('tools', []):
            tool['api'].update(
                timeout=options.tool_timeout,
                poll_interval=options.poll_interval,
                poll_max_interval=options.poll_interval,
                poll_factor=1.0,
            )
    effective = Settings.model_validate(effective.model_dump())
    project = f'{local.project_name}-{local.profile}'
    port = effective.server.port
    credentials = {role: getattr(settings.secrets, role) for role in ('service', 'llm', 'api')}
    environment = {'AGENTIC_AIGC_CONFIG': '/config/service.yaml'}
    for name in credentials.values():
        environment[name + '_FILE'] = '/run/secrets/' + name
    common = {
        'image': local.image,
        'platform': local.platform,
        'init': True,
        'read_only': True,
        'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'],
        'cpus': local.cpu,
        'mem_limit': f'{local.memory_mb}m',
        'pids_limit': 128,
        'tmpfs': ['/tmp:size=64m,mode=1777'],
    }
    health = {
        'test': [
            'CMD',
            'python',
            '-c',
            f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/health/ready', timeout=3)",
        ],
        'interval': '2s',
        'timeout': '4s',
        'start_period': '10s',
        'retries': 10,
    }
    aigc = {
        **common,
        'build': {'context': str(root), 'dockerfile': 'docker/aigc.Dockerfile'},
        'restart': 'unless-stopped',
        'environment': environment,
        'ports': [f'127.0.0.1:{local.host_port}:{port}'],
        'volumes': ['aigc_data:/data', f'{directory}/service.yaml:/config/service.yaml:ro'],
        'secrets': list(credentials.values()),
        'healthcheck': health,
        'stop_grace_period': f'{effective.runtime.drain_timeout + effective.server.graceful_shutdown_timeout + 10:g}s',
    }
    services = {'aigc': aigc}
    if local.profile == 'mock':
        services['mock'] = {
            **common,
            'entrypoint': ['python', '/harness/mock_upstream.py'],
            'environment': environment,
            'ports': [f'127.0.0.1:{local.mock_host_port}:8000'],
            'volumes': [
                f'{directory}/service.yaml:/config/service.yaml:ro',
                f'{directory.parent}/mock_upstream.py:/harness/mock_upstream.py:ro',
            ],
            'secrets': list(credentials.values()),
            'healthcheck': {
                **health,
                'test': [
                    'CMD',
                    'python',
                    '-c',
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)",
                ],
            },
        }
        aigc['depends_on'] = {'mock': {'condition': 'service_healthy'}}
    compose = {
        'name': project,
        'services': services,
        'volumes': {'aigc_data': {}},
        # internal 网络会禁用宿主发布端口；本地验证使用 bridge + loopback 绑定。
        'networks': {'default': {'driver': 'bridge'}},
        'secrets': {name: {'file': str(directory / 'secrets' / name)} for name in credentials.values()},
    }
    client = {
        'profile': local.profile,
        'project_name': project,
        'base_url': f'http://127.0.0.1:{local.host_port}',
        'api_prefix': API_PREFIX,
        'mock_url': f'http://127.0.0.1:{local.mock_host_port}' if local.profile == 'mock' else None,
        'service_config': str(directory / 'service.yaml'),
        'compose_file': str(directory / 'compose.yaml'),
        'token_file': str(directory / 'secrets' / settings.secrets.service),
        'config_digest': effective.config_digest,
    }
    return {
        'directory': str(directory),
        'service': effective.model_dump(mode='json'),
        'compose': compose,
        'client': client,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=['eas', 'local'])
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--output', help='生成 JSON 文件（不部署）')
    args = parser.parse_args()
    settings = Settings.load(deployment=True)
    rendered = render_eas(settings, args.shard) if args.target == 'eas' else render_local(settings, Path.cwd())
    text = json.dumps(rendered, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + '\n', encoding='utf-8')
    else:
        print(text)


if __name__ == '__main__':
    main()
