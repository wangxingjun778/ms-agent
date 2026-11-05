"""真实 HTTP 子进程的 SIGTERM/SIGKILL 与重启对账；不使用远端模型。"""

import asyncio
import os
import signal
import socket
import sys
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from conftest import request

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('shutdown_signal', [signal.SIGTERM, signal.SIGKILL])
async def test_process_restart_does_not_repeat_run(tmp_path, shutdown_signal):
    calls = []
    entered = asyncio.Event()

    async def complete(http_request):
        calls.append(await http_request.json())
        entered.set()
        await asyncio.sleep(20)
        return web.json_response({'choices': [{'message': {'role': 'assistant', 'content': '完成'}}]})

    mock = web.Application()
    mock.router.add_post('/v1/chat/completions', complete)
    runner = web.AppRunner(mock, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    mock_port = site._server.sockets[0].getsockname()[1]
    with socket.socket() as temporary:
        temporary.bind(('127.0.0.1', 0))
        port = temporary.getsockname()[1]
    root = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        'DATA_DIR': str(tmp_path),
        'AIGC_HOST': '127.0.0.1',
        'AIGC_PORT': str(port),
        'AIGC_SERVICE_TOKEN': 'test-service',
        'LLM_API_KEY': 'test-llm',
        'API_TOKEN': 'test-image',
        'LLM_BASE_URL': f'http://127.0.0.1:{mock_port}/v1',
        'API_BASE_URL': 'http://127.0.0.1:1',
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    for name in ('AIGC_CONFIG', 'API_TOOLS_CONFIG', 'AIGC_SERVICE_TOKEN_FILE', 'LLM_API_KEY_FILE', 'API_TOKEN_FILE'):
        env.pop(name, None)
    process = None

    async def launch():
        return await asyncio.create_subprocess_exec(
            sys.executable,
            '-m',
            'service.aigc',
            cwd=root,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def ready(client):
        async with asyncio.timeout(10):
            while True:
                if process.returncode is not None:
                    raise AssertionError(f'服务启动失败 {process.returncode}')
                try:
                    if (await client.get('/health/ready')).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)

    try:
        process = await launch()
        async with httpx.AsyncClient(
            base_url=f'http://127.0.0.1:{port}',
            timeout=2,
            headers={'Authorization': 'Bearer test-service', 'X-User-Id': 'u1'},
        ) as client:
            await ready(client)
            body = request()
            response = await client.post('/v1/aigc/runs', json=body)
            assert response.status_code == 202
            await asyncio.wait_for(entered.wait(), 5)
            process.send_signal(shutdown_signal)
            await asyncio.wait_for(process.wait(), 10)
            process = await launch()
            await ready(client)
            response = await client.get(f'/v1/aigc/runs/{body["run_uuid"]}')
            assert response.status_code == 200 and response.json()['status'] == 'interrupted'
            duplicate = await client.post('/v1/aigc/runs', json=body)
            assert duplicate.status_code == 200 and duplicate.json()['status'] == 'interrupted'
            assert len(calls) == 1
            replay = await client.get(f'/v1/aigc/runs/{body["run_uuid"]}/events')
            assert replay.status_code == 200 and 'interrupted' in replay.text
            process.terminate()
            await asyncio.wait_for(process.wait(), 10)
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        await runner.cleanup()
