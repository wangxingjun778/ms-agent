"""单协调器多用户公平调度，等待交互时归还可计算槽位。"""

from __future__ import annotations

import asyncio
from collections import Counter, deque


class FairScheduler:
    def __init__(self, capacity: int, per_user: int) -> None:
        self.capacity, self.per_user = capacity, per_user
        self.active: Counter[str] = Counter()
        self._queues: dict[str, deque[asyncio.Future]] = {}
        self._users: deque[str] = deque()
        self._last_user: str | None = None

    async def acquire(self, user: str, *, resume: bool = False) -> None:
        future = asyncio.get_running_loop().create_future()
        if user not in self._queues:
            self._queues[user] = deque()
            self._users.append(user)
        queue = self._queues[user]
        queue.appendleft(future) if resume else queue.append(future)
        self._dispatch()
        try:
            await future
        except BaseException:
            if future.done() and not future.cancelled():
                self.release(user)
            else:
                future.cancel()
                self._dispatch()
            raise

    def release(self, user: str) -> None:
        if self.active[user] <= 0:
            raise RuntimeError('调度槽位重复归还')
        self.active[user] -= 1
        if not self.active[user]:
            del self.active[user]
        self._dispatch()

    def _dispatch(self) -> None:
        # 每轮每个用户最多获取一个槽；受 per-user 限制的用户不阻塞其他用户。
        while self._users and sum(self.active.values()) < self.capacity:
            if len(self._users) > 1 and self._users[0] == self._last_user:
                self._users.rotate(-1)
            progressed = False
            for _ in range(len(self._users)):
                user = self._users.popleft()
                queue = self._queues[user]
                while queue and queue[0].cancelled():
                    queue.popleft()
                if queue and self.active[user] < self.per_user and sum(self.active.values()) < self.capacity:
                    self.active[user] += 1
                    self._last_user = user
                    queue.popleft().set_result(None)
                    progressed = True
                if queue:
                    self._users.append(user)
                else:
                    del self._queues[user]
            if not progressed:
                break

    def permit(self, user: str) -> 'RunPermit':
        return RunPermit(self, user)


class RunPermit:
    def __init__(self, scheduler: FairScheduler, user: str) -> None:
        self.scheduler, self.user, self.held = scheduler, user, False

    async def __aenter__(self):
        await self.scheduler.acquire(self.user)
        self.held = True
        return self

    async def __aexit__(self, *_):
        self.release()

    def release(self) -> None:
        if self.held:
            self.held = False
            self.scheduler.release(self.user)

    async def resume(self) -> None:
        if not self.held:
            await self.scheduler.acquire(self.user, resume=True)
            self.held = True
