"""Docker 容器重启、持久卷与安全基线的本地冒烟测试。"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def docker(*args, check=True):
    return subprocess.run(['docker', *args], check=check, capture_output=True, text=True)


def request(port, method, path, body=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    headers = {'Authorization': 'Bearer smoke-service', 'X-User-Id': 'smoke-user', 'Content-Type': 'application/json'}
    connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    return response.status, data


def wait_ready(port, name):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if docker('inspect', '-f', '{{.State.Running}}', name, check=False).stdout.strip() != 'true':
            raise RuntimeError(docker('logs', name, check=False).stdout)
        try:
            if request(port, 'GET', '/health/ready')[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError('容器未 ready')


def run(name, volume, port, secrets, *, read_only=True):
    args = [
        'run',
        '-d',
        '--name',
        name,
        '--init',
        '--cap-drop=ALL',
        '--security-opt=no-new-privileges',
        '--pids-limit=128',
        '--memory=1g',
        '--cpus=2',
        '-p',
        f'127.0.0.1:{port}:8000',
        '-v',
        f'{volume}:/data',
    ]
    if read_only:
        args += ['--read-only', '--tmpfs', '/tmp:size=64m,mode=1777']
    args += [
        '--add-host=host.docker.internal:host-gateway',
        '-e',
        'AGENTIC_AIGC_DATA_DIR=/data',
        '-e',
        'AGENTIC_AIGC_SERVICE_TOKEN_FILE=/run/secrets/service',
        '-e',
        'AGENTIC_AIGC_LLM_API_KEY_FILE=/run/secrets/llm',
        '-e',
        'AGENTIC_AIGC_API_TOKEN_FILE=/run/secrets/api',
        '-e',
        f'AGENTIC_AIGC_LLM_BASE_URL=http://host.docker.internal:{os.environ["AGENTIC_AIGC_SMOKE_LLM_PORT"]}/v1',
        '-e',
        'AGENTIC_AIGC_API_BASE_URL=http://127.0.0.1:1',
        '-v',
        f'{secrets}/service:/run/secrets/service:ro',
        '-v',
        f'{secrets}/llm:/run/secrets/llm:ro',
        '-v',
        f'{secrets}/api:/run/secrets/api:ro',
        'ms-agent-aigc:dev',
    ]
    docker(*args)


class SlowLLM(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        self.rfile.read(length)
        time.sleep(30)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18080)
    args = parser.parse_args()
    token = uuid.uuid4().hex[:8]
    name, rival, volume = f'aigc-smoke-{token}', f'aigc-rival-{token}', f'aigc-data-{token}'
    server = socketserver.TCPServer(('0.0.0.0', 0), SlowLLM)
    server_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        for file, value in [('service', 'smoke-service'), ('llm', 'smoke-llm'), ('api', 'smoke-api')]:
            (directory / file).write_text(value)
            os.chmod(directory / file, 0o444)  # 仅一次性 mock 凭据，供非 root 容器读取。
        try:
            os.environ['AGENTIC_AIGC_SMOKE_LLM_PORT'] = str(server_port)
            run(name, volume, args.port, directory)
            wait_ready(args.port, name)
            inspect = json.loads(docker('inspect', name).stdout)[0]
            assert inspect['Config']['User'] == '10001:10001' and inspect['HostConfig']['ReadonlyRootfs']
            assert inspect['HostConfig']['CapDrop'] == ['ALL']
            body = {
                'run_uuid': str(uuid.uuid4()),
                'schema_version': 1,
                'session_id': str(uuid.uuid4()),
                'user_message_uuid': str(uuid.uuid4()),
                'assistant_message_uuid': str(uuid.uuid4()),
                'parent_message_uuid': str(uuid.uuid4()),
                'execution_policy': {'deadline_at': (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()},
                'input_snapshot': {'effective': {'prompt': 'smoke'}, 'context_mode': 'explicit_snapshot'},
            }
            status, accepted = request(args.port, 'POST', '/v1/aigc/runs', body)
            assert status == 202
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                _, current = request(args.port, 'GET', f'/v1/aigc/runs/{body["run_uuid"]}')
                if current['status'] == 'running':
                    break
                time.sleep(0.05)
            assert current['status'] == 'running'
            docker('kill', name)
            docker('rm', name)
            run(name, volume, args.port, directory)
            wait_ready(args.port, name)
            status, recovered = request(args.port, 'GET', f'/v1/aigc/runs/{body["run_uuid"]}')
            assert status == 200 and recovered['status'] == 'failed', recovered
            assert recovered['execution_closed'] and recovered['can_release_activity']
            assert recovered['error']['code'] == 'SERVICE_INTERRUPTED'
            assert request(args.port, 'POST', '/v1/aigc/runs', body)[1]['request_digest'] == accepted['request_digest']
            assert request(args.port, 'POST', '/v2/aigc/runs', body)[0] == 404
            run(rival, volume, args.port + 1, directory, read_only=False)
            deadline = time.monotonic() + 10
            while (
                time.monotonic() < deadline
                and docker('inspect', '-f', '{{.State.Running}}', rival, check=False).stdout.strip() == 'true'
            ):
                time.sleep(0.2)
            assert docker('inspect', '-f', '{{.State.ExitCode}}', rival).stdout.strip() != '0'
            print(json.dumps({'image': 'ms-agent-aigc:dev', 'status': 'passed', 'recovered': recovered['status']}))
        finally:
            server.shutdown()
            server.server_close()
            for container in (name, rival):
                docker('rm', '-f', container, check=False)
            docker('volume', 'rm', volume, check=False)


if __name__ == '__main__':
    main()
