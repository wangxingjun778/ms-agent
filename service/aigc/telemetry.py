"""EAS 监控与租约检测，所有后台任务归 lifespan 所有。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque

from ms_agent.session.locking import PersistenceError


class JSONFormatter(logging.Formatter):
    def __init__(self, secrets=()):
        super().__init__()
        self.secrets = tuple(value for value in secrets if value)

    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message += '\n' + self.formatException(record.exc_info)
        for value in self.secrets:
            message = message.replace(value, '[redacted]')
        return json.dumps(
            {
                'level': record.levelname,
                'logger': record.name,
                'message': message,
                'request_id': getattr(record, 'request_id', None),
                'run_uuid': getattr(record, 'run_uuid', None),
            },
            ensure_ascii=False,
        )


def configure_logging(secrets):
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter(secrets))
    logger = logging.getLogger('ms_agent')
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    service = logging.getLogger('service.aigc')
    service.handlers = [handler]
    service.setLevel(logging.INFO)
    service.propagate = False


class Telemetry:
    def __init__(self, settings, runtime):
        self.settings, self.runtime = settings, runtime
        self.lags = deque(maxlen=settings.observability.loop_window_samples)
        self.http_requests = 0
        self.http_errors = 0
        self.task = None

    def stats(self):
        runtime = self.runtime
        limiters = runtime.agent.limiters
        return {
            'inflight': len(runtime._tasks) + len(runtime._reservations),
            'active': sum(runtime._scheduler.active.values()),
            'waiting': sum(control.pending is not None for control in runtime._controls.values()),
            'active_ratio': sum(runtime._scheduler.active.values()) / runtime.max_runs,
            'llm_inflight': limiters.get('llm').inflight if limiters.get('llm') else 0,
            'sse_subscribers': runtime.subscribers,
            'loop_lag_ms_p99': round(sorted(self.lags)[int(len(self.lags) * 0.99)] * 1000, 3) if self.lags else 0,
            'http_requests': self.http_requests,
            'http_errors': self.http_errors,
            'storage_corrupted': len(runtime.store.corrupted),
            'cache_bytes': runtime.store._cached_bytes,
            'upstreams': {
                name: {'inflight': limiter.inflight, 'requests': limiter.requests, 'throttles': limiter.throttles}
                for name, limiter in limiters.items()
            },
            'config_digest': self.settings.config_digest,
        }

    async def run(self):
        import aiohttp

        options = self.settings.observability
        loop = asyncio.get_running_loop()
        next_epoch, next_report = loop.time(), loop.time()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=options.metrics_timeout)) as client:
            while True:
                start = loop.time()
                await asyncio.sleep(options.loop_sample_seconds)
                self.lags.append(max(0, loop.time() - start - options.loop_sample_seconds))
                if loop.time() >= next_epoch:
                    next_epoch = loop.time() + self.settings.storage.epoch_check_interval
                    try:
                        await self.runtime.store.check_epoch()
                    except PersistenceError:
                        self.runtime.begin_drain()
                        for running in list(self.runtime._tasks.values()):
                            running.cancel()
                        return
                if options.metrics_enabled and loop.time() >= next_report:
                    next_report = loop.time() + options.metrics_interval
                    values = self.stats()
                    payload = [
                        {'name': name, 'value': values[name]}
                        for name in ('inflight', 'active', 'waiting', 'active_ratio', 'llm_inflight', 'sse_subscribers')
                    ]
                    try:
                        async with client.post(options.metrics_endpoint, json=payload) as response:
                            await response.read()
                            if response.status >= 400:
                                logging.getLogger(__name__).warning('EAS 指标上报返回 HTTP %s', response.status)
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        logging.getLogger(__name__).warning('EAS 指标上报失败，不影响执行')

    def start(self):
        self.task = asyncio.create_task(self.run())

        def completed(task):
            if not task.cancelled() and task.exception():
                # 租约检测任务意外退出不能继续按健康协调器服务。
                self.runtime.store.healthy = False
                self.runtime.begin_drain()
                for running in list(self.runtime._tasks.values()):
                    running.cancel()
                logging.getLogger(__name__).error('协调器监控任务异常退出')

        self.task.add_done_callback(completed)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
