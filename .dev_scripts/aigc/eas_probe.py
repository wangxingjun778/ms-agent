"""EAS 平台行为实测：先以 ``serve`` 模式部署为临时 EAS 服务，再用 ``client`` 模式从调用方验证。

只依赖标准库。验证项：
- 网关是否把 ``Authorization`` 与自定义鉴权头透传给容器；
- 网关单请求体上限；
- SSE 长连接在网关上的实际存活时长；
- 后端 keep-alive 小于网关回源空闲超时时是否出现 502/连接重置；
- 挂载目录的文件系统类型、NFS 版本/锁参数、fsync 时延，以及跨副本 flock 是否互斥。

部署（示意）：镜像使用 python:3.12-slim，挂载与服务相同的 NAS 到 /data，运行命令
``python /code/eas_probe.py serve --port 8000 --keepalive 5``，副本数 2（用于 flock 互斥验证）。

调用：
    python eas_probe.py client --endpoint <EAS_ENDPOINT> --token <EAS_TOKEN> --output probe.json
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import socket
import ssl
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOSTNAME = socket.gethostname()
_HELD: dict[str, int] = {}


def mount_info(path: str) -> dict:
    """返回覆盖 path 的挂载点及其文件系统类型和选项（Linux mountinfo）。"""
    target = os.path.realpath(path)
    best = None
    try:
        lines = Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError as exc:
        return {'error': str(exc)}
    for line in lines:
        left, _, right = line.partition(' - ')
        fields, tail = left.split(), right.split()
        mount_point = fields[4]
        if target == mount_point or target.startswith(mount_point.rstrip('/') + '/'):
            if best is None or len(mount_point) > len(best['mount_point']):
                best = {
                    'mount_point': mount_point,
                    'mount_options': fields[5],
                    'fs_type': tail[0] if tail else '',
                    'source': tail[1] if len(tail) > 1 else '',
                    'super_options': tail[2] if len(tail) > 2 else '',
                }
    if best:
        options = set(best['mount_options'].split(',')) | set(best['super_options'].split(','))
        best['nfs_version'] = next((o.split('=', 1)[1] for o in options if o.startswith('vers=')), None)
        best['nolock'] = 'nolock' in options
        best['local_lock'] = next((o.split('=', 1)[1] for o in options if o.startswith('local_lock=')), None)
    return best or {'error': 'mount not found'}


def fsync_latency(path: str, count: int) -> dict:
    directory = Path(path) / f'.eas-probe-{HOSTNAME}'
    directory.mkdir(parents=True, exist_ok=True)
    file_times, dir_times = [], []
    target = directory / 'journal.jsonl'
    with target.open('ab') as handle:
        for _ in range(count):
            handle.write(b'{"probe":true}\n')
            handle.flush()
            started = time.perf_counter()
            os.fsync(handle.fileno())
            file_times.append(time.perf_counter() - started)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            started = time.perf_counter()
            os.fsync(fd)
            dir_times.append(time.perf_counter() - started)
            os.close(fd)

    def summary(values):
        ordered = sorted(values)
        return {
            'p50_ms': round(statistics.median(ordered) * 1000, 3),
            'p95_ms': round(ordered[int(0.95 * (len(ordered) - 1))] * 1000, 3),
            'max_ms': round(ordered[-1] * 1000, 3),
        }

    return {'file_fsync': summary(file_times), 'dir_fsync': summary(dir_times), 'count': count}


def lock(path: str, hold: float) -> dict:
    """尝试独占 flock；成功且 hold>0 时在后台持有该锁 hold 秒。"""
    lock_path = Path(path) / '.eas-probe.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        return {'acquired': False, 'error': type(exc).__name__, 'host': HOSTNAME}
    if hold <= 0:
        os.close(fd)
        return {'acquired': True, 'held_seconds': 0, 'host': HOSTNAME}
    _HELD[str(lock_path)] = fd

    def release():
        time.sleep(hold)
        os.close(_HELD.pop(str(lock_path)))

    threading.Thread(target=release, daemon=True).start()
    return {'acquired': True, 'held_seconds': hold, 'host': HOSTNAME}


class ProbeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    keepalive = 5.0

    def setup(self):
        super().setup()
        self.connection.settimeout(self.keepalive)

    def log_message(self, format, *args):  # noqa: A002
        return

    def reply(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        url = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if url.path.endswith('/echo'):
            # 不把网关凭据原文返回给调用方或写入报告。
            hashes = {k.lower(): hashlib.sha256(v.encode()).hexdigest() for k, v in self.headers.items()}
            return self.reply({'host': HOSTNAME, 'path': self.path, 'header_hashes': hashes})
        if url.path.endswith('/health'):
            return self.reply({'status': 'ok', 'host': HOSTNAME})
        if url.path.endswith('/mount'):
            return self.reply({'host': HOSTNAME, **mount_info(query.get('path', '/data'))})
        if url.path.endswith('/fsync'):
            return self.reply(
                {'host': HOSTNAME, **fsync_latency(query.get('path', '/data'), int(query.get('count', '50')))}
            )
        if url.path.endswith('/lock'):
            return self.reply(lock(query.get('path', '/data'), float(query.get('hold', '0'))))
        if url.path.endswith('/sse'):
            seconds, interval = float(query.get('seconds', '60')), float(query.get('interval', '10'))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            started = time.monotonic()
            try:
                while time.monotonic() - started < seconds:
                    elapsed = round(time.monotonic() - started, 1)
                    self.wfile.write(f'data: {{"elapsed": {elapsed}}}\n\n'.encode())
                    self.wfile.flush()
                    time.sleep(interval)
                self.wfile.write(b'event: done\ndata: {}\n\n')
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True
            return None
        return self.reply({'error': 'not found'}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get('Content-Length') or 0)
        received = 0
        while received < length:
            chunk = self.rfile.read(min(65536, length - received))
            if not chunk:
                break
            received += len(chunk)
        return self.reply({'received_bytes': received, 'host': HOSTNAME})


def serve(args) -> None:
    ProbeHandler.keepalive = args.keepalive
    server = ThreadingHTTPServer(('0.0.0.0', args.port), ProbeHandler)
    server.daemon_threads = True
    print(f'eas_probe serving on :{args.port} keepalive={args.keepalive}s host={HOSTNAME}', flush=True)
    server.serve_forever()


class Client:
    def __init__(self, endpoint: str, token: str, timeout: float) -> None:
        parts = urlsplit(endpoint.rstrip('/'))
        self.https = parts.scheme == 'https'
        self.host, self.prefix, self.token, self.timeout = parts.netloc, parts.path, token, timeout

    def connect(self, timeout: float | None = None):
        if self.https:
            return http.client.HTTPSConnection(
                self.host, timeout=timeout or self.timeout, context=ssl.create_default_context()
            )
        return http.client.HTTPConnection(self.host, timeout=timeout or self.timeout)

    def headers(self, extra: dict | None = None) -> dict:
        return {'Authorization': self.token, **(extra or {})}

    def call(self, method: str, path: str, body: bytes | None = None, extra: dict | None = None, connection=None):
        own = connection is None
        connection = connection or self.connect()
        try:
            connection.request(method, self.prefix + path, body=body, headers=self.headers(extra))
            response = connection.getresponse()
            data = response.read()
            try:
                parsed = json.loads(data)
            except ValueError:
                parsed = data[:200].decode(errors='replace')
            return response.status, parsed
        finally:
            if own:
                connection.close()


def check_headers(client: Client) -> dict:
    status, data = client.call('GET', '/echo', extra={'X-AIGC-Service-Token': 'probe-token', 'X-User-Id': 'probe-user'})
    headers = data.get('header_hashes', {}) if isinstance(data, dict) else {}

    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    return {
        'status': status,
        'authorization_forwarded': 'authorization' in headers,
        'authorization_value_equals_token': headers.get('authorization') == digest(client.token),
        'custom_token_forwarded': headers.get('x-aigc-service-token') == digest('probe-token'),
        'user_id_forwarded': headers.get('x-user-id') == digest('probe-user'),
        'path_seen_by_container': data.get('path') if isinstance(data, dict) else None,
    }


def check_body(client: Client, sizes: list[int]) -> list[dict]:
    results = []
    for size in sizes:
        try:
            status, data = client.call('POST', '/body', body=b'x' * size, extra={'Content-Type': 'text/plain'})
            results.append(
                {
                    'bytes': size,
                    'status': status,
                    'received': data.get('received_bytes') if isinstance(data, dict) else None,
                }
            )
        except OSError as exc:
            results.append({'bytes': size, 'error': type(exc).__name__})
    return results


def check_sse(client: Client, seconds: float, interval: float) -> dict:
    connection = client.connect(timeout=interval * 3 + 30)
    started = time.monotonic()
    last_elapsed, completed, error = None, False, None
    try:
        connection.request(
            'GET', client.prefix + f'/sse?seconds={seconds}&interval={interval}', headers=client.headers()
        )
        response = connection.getresponse()
        while True:
            line = response.readline()
            if not line:
                break
            text = line.decode().strip()
            if text.startswith('event: done'):
                completed = True
            if text.startswith('data: ') and 'elapsed' in text:
                last_elapsed = json.loads(text[6:])['elapsed']
        status = response.status
    except OSError as exc:
        status, error = None, type(exc).__name__
    finally:
        connection.close()
    return {
        'requested_seconds': seconds,
        'status': status,
        'completed': completed,
        'last_server_elapsed': last_elapsed,
        'client_seconds': round(time.monotonic() - started, 1),
        'error': error,
    }


def check_keepalive(client: Client, gaps: list[float]) -> list[dict]:
    """同一客户端连接上按间隔复用；后端 keep-alive 较短时网关回源连接可能已被关闭。"""
    results = []
    for gap in gaps:
        failures, statuses = 0, []
        connection = client.connect()
        for _ in range(3):
            try:
                status, _ = client.call('GET', '/health', connection=connection)
                statuses.append(status)
                if status >= 500:
                    failures += 1
            except OSError as exc:
                failures += 1
                statuses.append(type(exc).__name__)
                connection.close()
                connection = client.connect()
            time.sleep(gap)
        connection.close()
        results.append({'gap_seconds': gap, 'statuses': statuses, 'failures': failures})
    return results


def check_storage(client: Client, path: str, hold: float) -> dict:
    _, mount = client.call('GET', f'/mount?path={path}')
    _, fsync = client.call('GET', f'/fsync?path={path}&count=50')
    _, first = client.call('GET', f'/lock?path={path}&hold={hold}')
    attempts = []
    deadline = time.monotonic() + min(hold, 20)
    while time.monotonic() < deadline and len(attempts) < 20:
        _, result = client.call('GET', f'/lock?path={path}&hold=0')
        attempts.append(result)
        time.sleep(0.5)
    other_hosts = [a for a in attempts if isinstance(a, dict) and a.get('host') != first.get('host')]
    return {
        'mount': mount,
        'fsync': fsync,
        'lock_holder': first,
        'lock_attempts_from_other_replicas': other_hosts,
        'cross_replica_lock_exclusive': None if not other_hosts else all(not a['acquired'] for a in other_hosts),
    }


def client_main(args) -> None:
    client = Client(args.endpoint, args.token, args.timeout)
    report = {'endpoint': args.endpoint, 'headers': check_headers(client)}
    report['body'] = check_body(client, [900 * 1024, 1024 * 1024 - 512, 1024 * 1024 + 512, 2 * 1024 * 1024])
    report['keepalive'] = check_keepalive(client, [float(g) for g in args.keepalive_gaps.split(',')])
    report['storage'] = check_storage(client, args.data_path, args.lock_hold)
    report['sse'] = [check_sse(client, float(s), args.sse_interval) for s in args.sse_seconds.split(',')]
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + '\n', encoding='utf-8')
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    serve_parser = sub.add_parser('serve', help='在 EAS 容器内运行的探针服务')
    serve_parser.add_argument('--port', type=int, default=8000)
    serve_parser.add_argument('--keepalive', type=float, default=5.0, help='模拟后端 keep-alive 空闲超时（秒）')
    client_parser = sub.add_parser('client', help='从调用方执行验证')
    client_parser.add_argument('--endpoint', required=True, help='含 /api/predict/<service> 的服务地址')
    client_parser.add_argument('--token', required=True, help='EAS 服务 Token')
    client_parser.add_argument('--timeout', type=float, default=30)
    client_parser.add_argument('--data-path', default='/data')
    client_parser.add_argument('--lock-hold', type=float, default=20)
    client_parser.add_argument('--keepalive-gaps', default='2,10,20,40')
    client_parser.add_argument('--sse-seconds', default='60,300,590,700')
    client_parser.add_argument('--sse-interval', type=float, default=15)
    client_parser.add_argument('--output')
    args = parser.parse_args()
    if args.command == 'serve':
        serve(args)
    else:
        client_main(args)


if __name__ == '__main__':
    main()
