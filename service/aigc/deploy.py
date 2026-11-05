"""从统一 service.yaml 派生 EAS 部署 JSON，不创建云端服务或计费资源。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

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
            'http_get': {'path': path, 'port': settings.port},
            'period_seconds': 10,
            'timeout_seconds': 3,
            'success_threshold': 1,
            'failure_threshold': math.ceil(eas.startup_budget / 10) if startup else 3,
        }

    env = {
        **settings._environment_overrides,
        'DATA_DIR': data_dir,
        'AIGC_PUBLIC_PATH_PREFIX': prefix,
        'AIGC_AUTH_HEADER': effective.auth.header,
        'AIGC_AUTH_SCHEME': effective.auth.scheme,
        'AIGC_EAS_ENABLED': 'true',
        'AIGC_EAS_GATEWAY': effective.eas.gateway,
        # Pod 与生成器读取同一份 YAML，配置挂载与可写数据目录分离。
        'AIGC_CONFIG': '/aigc-config/service.yaml',
        'AIGC_METRICS_ENABLED': 'true',
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
                'port': settings.port,
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=['eas'])
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--output', help='生成 JSON 文件（不部署）')
    args = parser.parse_args()
    settings = Settings.from_env(deployment=True)
    text = json.dumps(render_eas(settings, args.shard), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + '\n', encoding='utf-8')
    else:
        print(text)


if __name__ == '__main__':
    main()
