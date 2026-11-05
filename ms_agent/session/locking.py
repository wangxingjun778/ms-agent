"""本地单协调器使用的路径校验、锁与取消安全文件操作。"""

from __future__ import annotations

import asyncio
import os
import re
import time
import weakref
from functools import partial
from pathlib import Path
from typing import Any, Callable

_ID = re.compile(r'[A-Za-z0-9_.:@-]{1,128}\Z')


class PersistenceError(OSError):
    """台账无法提交，必须阻止后续副作用。"""


def validate_id(value: str, name: str = 'id') -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in {'.', '..'}:
        raise ValueError(f'{name} 必须是长度不超过 128 的安全路径标识')
    return value


def safe_path(root: Path, *parts: str) -> Path:
    """拒绝根目录下的任何符号链接，避免不同用户路径指向同一文件。"""
    root = root.absolute()
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('路径超出授权目录')
    cursor = root
    for part in target.relative_to(root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError('数据目录不允许符号链接')
    return target


async def durable_io(function: Callable[..., Any], *args: Any, _executor=None, **kwargs: Any) -> Any:
    """取消时仍等待工作线程完成，调用方锁不会早于实际写入释放。"""

    async def execute():
        if _executor is None:
            return await asyncio.to_thread(function, *args, **kwargs)
        return await asyncio.get_running_loop().run_in_executor(_executor, partial(function, *args, **kwargs))

    task = asyncio.create_task(execute())
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
        except PersistenceError:
            raise
        except OSError as exc:
            raise PersistenceError('文件操作失败') from exc
    if cancelled:
        raise asyncio.CancelledError
    return result


class PathLocks:
    """由服务生命周期持有，不创建进程级可变单例。"""

    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self.executor = None

    def for_path(self, path: Path | str) -> asyncio.Lock:
        key = str(Path(path).absolute())
        return self._locks.setdefault(key, asyncio.Lock())


class FileLease:
    """仅适用 POSIX 本地文件系统的协调器独占租约。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def check_filesystem(self, policy: str = 'require') -> dict:
        """拒绝会把 flock 降为本地锁的远端挂载；周期 epoch 不构成 fencing。"""
        mountinfo = Path('/proc/self/mountinfo')
        if not mountinfo.exists():
            return {'fs_type': 'local', 'options': ''}
        target = str(self.root.resolve())
        matches = []
        for line in mountinfo.read_text().splitlines():
            left, _, right = line.partition(' - ')
            fields, tail = left.split(), right.split()
            point = fields[4].replace('\\040', ' ')
            if target == point or target.startswith(point.rstrip('/') + '/'):
                matches.append((len(point), tail[0], fields[5] + ',' + tail[2]))
        _, kind, options = max(matches, default=(0, 'local', ''))
        unsafe = kind in ('nfs', 'nfs4') and (
            not ('vers=4' in options or kind == 'nfs4')
            or 'nolock' in options
            or any(flag in options for flag in ('local_lock=all', 'local_lock=flock'))
        )
        if unsafe and policy == 'require':
            raise RuntimeError('NAS 必须使用跨客户端 flock 的 NFSv4 挂载，不允许本地锁退化')
        if kind.startswith('fuse'):
            raise RuntimeError('不支持把对象存储/FUSE 挂载用于权威文件台账')
        return {'fs_type': kind, 'options': options, 'unsafe_lock': unsafe}

    def acquire(self, wait: float = 0) -> None:
        import fcntl

        self.root.mkdir(parents=True, exist_ok=True)
        path = safe_path(self.root, '.coordinator.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except BaseException:
            os.close(fd)
            raise RuntimeError('数据目录已由另一个协调器占用') from None
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
