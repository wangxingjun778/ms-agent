"""生命周期内共享的上游并发/RPM 限流；不使用全局单例。"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class UpstreamLimiter:
    def __init__(self, concurrency: int, rpm: int = 0) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self.rpm = rpm
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.inflight = 0
        self.requests = 0
        self.throttles = 0

    async def __aenter__(self):
        await self._semaphore.acquire()
        try:
            if self.rpm:
                while True:
                    async with self._lock:
                        now = asyncio.get_running_loop().time()
                        while self._timestamps and now - self._timestamps[0] >= 60:
                            self._timestamps.popleft()
                        if len(self._timestamps) < self.rpm:
                            self._timestamps.append(now)
                            break
                        delay = 60 - (now - self._timestamps[0])
                    await asyncio.sleep(max(0, delay))
            self.inflight += 1
            self.requests += 1
            return self
        except BaseException:
            self._semaphore.release()
            raise

    async def __aexit__(self, *_):
        self.inflight -= 1
        self._semaphore.release()


def retry_delay(headers: dict, attempt: int, config: dict) -> float:
    value = headers.get('Retry-After', '')
    try:
        return max(0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            base = min(config['maximum'], config['initial'] * (2**attempt))
            return random.uniform(base / 2, base)
