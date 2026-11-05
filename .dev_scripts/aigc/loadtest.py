"""AIGC 服务单 Pod 压测：本地 mock 上游，服务以独立子进程运行并在子进程内打点。

示例：
    .venv-aigc/bin/python .dev_scripts/aigc/loadtest.py --users 20 --sessions 2 --runs 3 \\
        --image-ratio 0.3 --output /tmp/aigc-baseline.json

服务子进程只通过环境变量和 ``--config``（AIGC_CONFIG）接收配置；打点仅统计
``os.fsync`` 与 journal 全量读取次数，不修改服务代码。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE_MODEL = 'Qwen/Qwen-Image-2.1-Turbo'
TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted'}


def child_main(stats_path: str) -> None:
    """被测服务进程：打点后进入正常的 ``python -m service.aigc`` 启动路径。"""
    import atexit
    import threading

    sys.path.insert(0, str(ROOT))
    counters = {'fsync': 0, 'journal_reads': 0}
    guard = threading.Lock()
    original_fsync = os.fsync

    def counted_fsync(fd):
        with guard:
            counters['fsync'] += 1
        return original_fsync(fd)

    os.fsync = counted_fsync
    from ms_agent.session import run_store

    original_read = run_store.FileRunStore._read_sync

    def counted_read(self, *args, **kwargs):
        with guard:
            counters['journal_reads'] += 1
        return original_read(self, *args, **kwargs)

    run_store.FileRunStore._read_sync = counted_read

    def dump():
        with guard:
            snapshot = dict(counters)
        Path(stats_path).write_text(json.dumps(snapshot), encoding='utf-8')

    def periodic():
        while True:
            time.sleep(1)
            dump()

    threading.Thread(target=periodic, daemon=True).start()
    atexit.register(dump)
    from service.aigc.__main__ import main

    main()


class MockUpstream:
    """OpenAI 兼容 LLM 与异步图片 API；延迟和 429 注入均可配置。"""

    def __init__(self, args) -> None:
        self.args = args
        self.random = random.Random(args.seed + 1)
        self.tasks: dict[str, float] = {}
        self.counts = {'llm': 0, 'llm_429': 0, 'submit': 0, 'submit_429': 0, 'poll': 0, 'poll_429': 0}

    def _throttled(self, ratio: float, key: str) -> bool:
        if ratio > 0 and self.random.random() < ratio:
            self.counts[key] += 1
            return True
        return False

    async def chat(self, request):
        from aiohttp import web

        self.counts['llm'] += 1
        body = await request.json()
        if self._throttled(self.args.llm_429, 'llm_429'):
            return web.json_response({'error': 'rate limited'}, status=429)
        await asyncio.sleep(max(0.0, self.random.gauss(self.args.llm_latency, self.args.llm_latency * 0.1)))
        last = body['messages'][-1]
        usage = {'prompt_tokens': sum(len(str(m.get('content') or '')) for m in body['messages']) // 2}
        usage.update(completion_tokens=16, total_tokens=usage['prompt_tokens'] + 16)
        if last.get('role') == 'user' and '[image]' in str(last.get('content')):
            arguments = json.dumps({'model': IMAGE_MODEL, 'prompt': '一只戴生日帽的猫'}, ensure_ascii=False)
            message = {
                'role': 'assistant',
                'content': '',
                'tool_calls': [
                    {
                        'id': f'call_{uuid.uuid4().hex[:8]}',
                        'type': 'function',
                        'function': {'name': 'image_generation', 'arguments': arguments},
                    }
                ],
            }
        else:
            message = {'role': 'assistant', 'content': '完成。'}
        return web.json_response({'choices': [{'message': message}], 'usage': usage})

    async def submit(self, request):
        from aiohttp import web

        self.counts['submit'] += 1
        if self._throttled(self.args.submit_429, 'submit_429'):
            return web.json_response({'errors': {'message': 'rate limited'}}, status=429)
        task_id = uuid.uuid4().hex
        self.tasks[task_id] = time.monotonic() + self.args.image_seconds
        return web.json_response({'task_id': task_id, 'task_status': 'PENDING', 'request_id': uuid.uuid4().hex})

    async def poll(self, request):
        from aiohttp import web

        self.counts['poll'] += 1
        if self._throttled(self.args.poll_429, 'poll_429'):
            return web.json_response({'errors': {'message': 'rate limited'}}, status=429)
        ready_at = self.tasks.get(request.match_info['task_id'])
        if ready_at is None:
            return web.json_response({'task_status': 'FAILED', 'errors': {'message': 'unknown task'}})
        if time.monotonic() < ready_at:
            return web.json_response({'task_status': 'RUNNING'})
        return web.json_response(
            {'task_status': 'SUCCEED', 'output_images': [f'https://example.invalid/{uuid.uuid4().hex}.png']}
        )

    async def start(self):
        from aiohttp import web

        app = web.Application()
        app.router.add_post('/v1/chat/completions', self.chat)
        app.router.add_post('/v1/images/generations', self.submit)
        app.router.add_get('/v1/tasks/{task_id}', self.poll)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        return site._server.sockets[0].getsockname()[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))], 4)


def sample_process(pid: int) -> tuple[float, int] | None:
    """返回累计 CPU 秒与 RSS KiB；macOS/Linux 的 ps 输出均兼容。"""
    result = subprocess.run(['ps', '-o', 'time=,rss=', '-p', str(pid)], capture_output=True, text=True)
    fields = result.stdout.split()
    if len(fields) != 2:
        return None
    seconds = 0.0
    clock, _, fraction = fields[0].partition('.')
    for part in clock.replace('-', ':').split(':'):
        seconds = seconds * 60 + float(part)
    return seconds + float(f'0.{fraction}' if fraction else 0), int(fields[1])


class LoadTest:
    def __init__(self, args, base_url: str, pid: int) -> None:
        self.args, self.base_url, self.pid = args, base_url, pid
        token = 'loadtest-service'
        auth_value = f'Bearer {token}' if args.auth_scheme == 'bearer' else token
        self.auth = {args.auth_header: auth_value}
        self.runs: list[dict] = []
        self.health: list[float] = []
        self.resources: list[tuple[float, float, int]] = []
        self.http_errors: dict[str, int] = {}
        self.done = False
        self.approvals: set[asyncio.Task] = set()
        self.event_delays = []
        self.get_latencies = []
        self.server_stats = {}
        image_rng = random.Random(args.seed)
        self.images = {
            (u, s, i): image_rng.random() < args.image_ratio
            for u in range(args.users)
            for s in range(args.sessions)
            for i in range(args.runs)
        }

    def schedule_approval(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.approvals.add(task)
        task.add_done_callback(self.approvals.discard)

        def completed(t):
            if not t.cancelled() and t.exception():
                self.error('approval_error')

        task.add_done_callback(completed)

    def headers_for(self, user: str) -> dict[str, str]:
        return {**self.auth, 'X-User-Id': user}

    def error(self, key: str) -> None:
        self.http_errors[key] = self.http_errors.get(key, 0) + 1

    async def approve(self, client, user: str, run_uuid: str, interaction_id: str, record: dict) -> None:
        await asyncio.sleep(self.args.approve_delay)
        response = await client.post(
            f'/v1/aigc/runs/{run_uuid}/commands',
            headers=self.headers_for(user),
            json={'command_id': f'approve-{interaction_id}', 'type': 'approve', 'interaction_id': interaction_id},
        )
        record['approvals'] += 1
        if response.status_code >= 400:
            self.error(f'command_{response.status_code}')

    async def follow_sse(self, client, user: str, record: dict) -> None:
        cursor, approved = 0, set()
        while True:
            try:
                async with client.stream(
                    'GET',
                    f'/v1/aigc/runs/{record["run_uuid"]}/events',
                    headers={**self.headers_for(user), 'Accept': 'text/event-stream'},
                    params={'after_seq': cursor},
                ) as response:
                    if response.status_code != 200:
                        self.error(f'events_{response.status_code}')
                        await asyncio.sleep(1)
                        continue
                    async for line in response.aiter_lines():
                        if not line.startswith('data: '):
                            continue
                        event = json.loads(line[6:])
                        cursor = event['agent_seq']
                        record['events_seen'] += 1
                        stamp = datetime.fromisoformat(event['timestamp'])
                        self.event_delays.append((datetime.now(timezone.utc) - stamp).total_seconds())
                        if event['event_type'] == 'interaction_requested':
                            interaction = event['data']['interaction_id']
                            if interaction not in approved:
                                approved.add(interaction)
                                self.schedule_approval(
                                    self.approve(client, user, record['run_uuid'], interaction, record)
                                )
                        if event['event_type'] in {'run_terminal', 'run_interrupted', 'run_cancelled'}:
                            record['status'] = event['data']['status']
                            return
            except Exception as exc:  # noqa: BLE001 — 断流按游标重连
                self.error(f'events_{type(exc).__name__}')
                await asyncio.sleep(0.5)

    async def follow_poll(self, client, user: str, record: dict) -> None:
        approved = set()
        while True:
            await asyncio.sleep(self.args.poll_interval)
            response = await client.get(f'/v1/aigc/runs/{record["run_uuid"]}', headers=self.headers_for(user))
            if response.status_code != 200:
                self.error(f'get_{response.status_code}')
                continue
            view = response.json()
            pending = view.get('pending_interaction')
            if pending and pending['interaction_id'] not in approved:
                approved.add(pending['interaction_id'])
                self.schedule_approval(
                    self.approve(client, user, record['run_uuid'], pending['interaction_id'], record)
                )
            if view['status'] in TERMINAL:
                record['status'] = view['status']
                return

    async def one_run(self, client, user: str, session: str, index: int) -> None:
        image = self.images[(int(user.rsplit('-', 1)[1]), int(session.rsplit('-', 1)[1]), index)]
        record = {
            'run_uuid': str(uuid.uuid4()),
            'user': user,
            'session': session,
            'image': image,
            'status': None,
            'approvals': 0,
            'events_seen': 0,
        }
        self.runs.append(record)
        body = {
            'run_uuid': record['run_uuid'],
            'session_id': session,
            'messages': [{'role': 'user', 'content': f'第 {index} 轮{" [image]" if image else ""}'}],
        }
        record['submitted'] = time.monotonic()
        while True:
            response = await client.post('/v1/aigc/runs', headers=self.headers_for(user), json=body)
            record['accept_seconds'] = time.monotonic() - record['submitted']
            if response.status_code == 429:
                self.error('start_429')
                await asyncio.sleep(1 + random.random())
                continue
            break
        if response.status_code not in (200, 202):
            self.error(f'start_{response.status_code}')
            record['status'] = f'http_{response.status_code}'
            record['finished'] = time.monotonic()
            return
        if self.args.follow == 'sse':
            await self.follow_sse(client, user, record)
        else:
            await self.follow_poll(client, user, record)
        record['finished'] = time.monotonic()

    async def session_worker(self, client, user: str, session: str) -> None:
        if self.args.mode == 'burst':
            await asyncio.gather(*(self.one_run(client, user, session, i) for i in range(self.args.runs)))
        else:
            for index in range(self.args.runs):
                await self.one_run(client, user, session, index)

    async def sample_health(self, client) -> None:
        while not self.done:
            started = time.monotonic()
            try:
                await client.get('/health/live')
                self.health.append(time.monotonic() - started)
            except Exception:  # noqa: BLE001
                self.error('health_error')
            await asyncio.sleep(0.2)

    async def sample_resources(self) -> None:
        while not self.done:
            sample = await asyncio.to_thread(sample_process, self.pid)
            if sample:
                self.resources.append((time.monotonic(), *sample))
            await asyncio.sleep(1)

    async def run(self) -> dict:
        import httpx

        limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
        timeout = httpx.Timeout(60, read=None)
        async with httpx.AsyncClient(base_url=self.base_url, limits=limits, timeout=timeout) as client:
            samplers = [asyncio.create_task(self.sample_health(client)), asyncio.create_task(self.sample_resources())]
            started = time.monotonic()
            workers = [
                self.session_worker(client, f'user-{u:03d}', f'session-{s:02d}')
                for u in range(self.args.users)
                for s in range(self.args.sessions)
            ]
            try:
                await asyncio.wait_for(asyncio.gather(*workers), self.args.max_seconds)
                if self.approvals:
                    await asyncio.gather(*self.approvals, return_exceptions=True)
            finally:
                for task in self.approvals:
                    task.cancel()
                await asyncio.gather(*self.approvals, return_exceptions=True)
                self.done = True
                await asyncio.gather(*samplers)
            wall = time.monotonic() - started
            self.done = True
            await asyncio.gather(*samplers)
            events = 0
            for record in self.runs:
                get_started = time.monotonic()
                response = await client.get(
                    f'/v1/aigc/runs/{record["run_uuid"]}', headers=self.headers_for(record['user'])
                )
                self.get_latencies.append(time.monotonic() - get_started)
                if response.status_code == 200:
                    view = response.json()
                    events += int(view.get('last_agent_seq') or 0)
                    record['error'] = (view.get('error') or {}).get('code')
            stats = await client.get('/internal/stats', headers=self.headers_for('user-000'))
            if stats.status_code == 200:
                self.server_stats = stats.json()
        return {'wall_seconds': wall, 'events': events}


def summarize(args, test: LoadTest, outcome: dict, upstream: MockUpstream, counters: dict) -> dict:
    statuses: dict[str, int] = {}
    errors: dict[str, int] = {}
    for record in test.runs:
        statuses[str(record['status'])] = statuses.get(str(record['status']), 0) + 1
        if record.get('error'):
            errors[record['error']] = errors.get(record['error'], 0) + 1
    finished = [r for r in test.runs if r.get('finished')]
    durations = [r['finished'] - r['submitted'] for r in finished]
    accepts = [r['accept_seconds'] for r in test.runs if 'accept_seconds' in r]
    cpu = rss = None
    if len(test.resources) >= 2:
        (t0, c0, _), (t1, c1, _) = test.resources[0], test.resources[-1]
        cpu = round((c1 - c0) / max(t1 - t0, 1e-6) * 100, 1)
        rss = max(item[2] for item in test.resources) // 1024
    events = outcome['events'] or 1
    succeeded = statuses.get('succeeded', 0)
    return {
        'profile': {
            key: getattr(args, key)
            for key in (
                'users',
                'sessions',
                'runs',
                'mode',
                'follow',
                'image_ratio',
                'llm_latency',
                'image_seconds',
                'approve_delay',
                'llm_429',
                'submit_429',
                'poll_429',
                'max_active_runs',
                'max_inflight_runs',
                'config',
            )
        },
        'runs_total': len(test.runs),
        'statuses': statuses,
        'run_errors': errors,
        'http_errors': test.http_errors,
        'wall_seconds': round(outcome['wall_seconds'], 2),
        'throughput_runs_per_min': round(succeeded / outcome['wall_seconds'] * 60, 2),
        'run_seconds_p50': percentile(durations, 50),
        'run_seconds_p95': percentile(durations, 95),
        'accept_seconds_p50': percentile(accepts, 50),
        'accept_seconds_p95': percentile(accepts, 95),
        'health_latency_ms_p50': None if not test.health else round(percentile(test.health, 50) * 1000, 2),
        'health_latency_ms_p99': None if not test.health else round(percentile(test.health, 99) * 1000, 2),
        'service_cpu_percent_avg': cpu,
        'service_rss_mib_max': rss,
        'events_committed': outcome['events'],
        'fsync_total': counters.get('fsync'),
        'fsync_per_event': round(counters.get('fsync', 0) / events, 2),
        'journal_reads_total': counters.get('journal_reads'),
        'journal_reads_per_event': round(counters.get('journal_reads', 0) / events, 2),
        'upstream_calls': upstream.counts,
        'mean_run_seconds': round(statistics.fmean(durations), 3) if durations else None,
        'event_latency_ms_p95': None if not test.event_delays else round(percentile(test.event_delays, 95) * 1000, 2),
        'get_latency_ms_p95': None if not test.get_latencies else round(percentile(test.get_latencies, 95) * 1000, 2),
        'loop_lag_ms_p99': test.server_stats.get('loop_lag_ms_p99'),
        'source_root': str(ROOT),
    }


async def main_async(args) -> dict:
    upstream = MockUpstream(args)
    mock_port = await upstream.start()
    port = free_port()
    data_dir = tempfile.mkdtemp(prefix='aigc-loadtest-')
    stats_path = Path(data_dir) / 'loadtest-counters.json'
    env = {
        **os.environ,
        'DATA_DIR': str(Path(data_dir) / 'data'),
        'AIGC_HOST': '127.0.0.1',
        'AIGC_PORT': str(port),
        'AIGC_MAX_RUNS': str(args.max_active_runs),
        'AIGC_MAX_INFLIGHT': str(args.max_inflight_runs),
        'AIGC_SERVICE_TOKEN': 'loadtest-service',
        'LLM_API_KEY': 'loadtest-llm',
        'API_TOKEN': 'loadtest-image',
        'LLM_BASE_URL': f'http://127.0.0.1:{mock_port}/v1',
        'API_BASE_URL': f'http://127.0.0.1:{mock_port}',
        'PYTHONDONTWRITEBYTECODE': '1',
        'AIGC_DRAIN_TIMEOUT': '0',
    }
    for name in ('AIGC_CONFIG', 'AIGC_SERVICE_TOKEN_FILE', 'LLM_API_KEY_FILE', 'API_TOKEN_FILE'):
        env.pop(name, None)
    if args.config:
        env['AIGC_CONFIG'] = str(Path(args.config).absolute())
    log = open(Path(data_dir) / 'service.log', 'wb')
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), '--child', str(stats_path)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f'http://127.0.0.1:{port}'
    try:
        import httpx

        async with httpx.AsyncClient(base_url=base_url) as probe:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f'服务启动失败，日志：{log.name}')
                try:
                    if (await probe.get('/health/ready')).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError('服务 30 秒内未 ready')
                await asyncio.sleep(0.1)
        test = LoadTest(args, base_url, process.pid)
        before = json.loads(stats_path.read_text()) if stats_path.exists() else {}
        outcome = await test.run()
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            await asyncio.to_thread(process.wait, timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait)
        log.close()
        await upstream.runner.cleanup()
    after = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    counters = {key: after.get(key, 0) - before.get(key, 0) for key in after}
    report = summarize(args, test, outcome, upstream, counters)
    report['service_log'] = log.name
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--child', help=argparse.SUPPRESS)
    parser.add_argument('--max-seconds', type=float, default=300, help='压测最长时长，防止故障导致无限等待')
    parser.add_argument('--users', type=int, default=10)
    parser.add_argument('--sessions', type=int, default=2, help='每个用户的会话数')
    parser.add_argument('--runs', type=int, default=3, help='每个会话的 Run 数')
    parser.add_argument('--mode', choices=['closed', 'burst'], default='closed', help='closed：同会话串行提交')
    parser.add_argument('--follow', choices=['sse', 'poll'], default='sse')
    parser.add_argument('--poll-interval', type=float, default=1.0)
    parser.add_argument('--image-ratio', type=float, default=0.3)
    parser.add_argument('--llm-latency', type=float, default=1.0)
    parser.add_argument('--image-seconds', type=float, default=6.0)
    parser.add_argument('--approve-delay', type=float, default=1.0)
    parser.add_argument('--llm-429', type=float, default=0.0, help='LLM 返回 429 的概率')
    parser.add_argument('--submit-429', type=float, default=0.0)
    parser.add_argument('--poll-429', type=float, default=0.0)
    parser.add_argument('--max-active-runs', type=int, default=64, help='runtime.max_active_runs')
    parser.add_argument('--max-inflight-runs', type=int, default=500, help='runtime.max_inflight_runs')
    parser.add_argument('--config', help='替换服务配置（AIGC_CONFIG）')
    parser.add_argument('--auth-header', default='Authorization')
    parser.add_argument('--auth-scheme', choices=['bearer', 'raw'], default='bearer')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--output', help='报告 JSON 路径')
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    if args.child:
        child_main(args.child)
        return
    random.seed(args.seed)
    report = asyncio.run(main_async(args))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
