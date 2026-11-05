"""Session store with user_id / session_id isolation."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ms_agent.session.locking import PathLocks, PersistenceError, durable_io, safe_path, validate_id
from ms_agent.session.session_log import SessionLog
from ms_agent.utils.atomic_file import atomic_write_json, fsync_directory

__all__ = ['SessionStore']

_SPECIAL_RECORD_TYPES = {
    'error',
    'loop_end',
    'permission',
    'compaction_event',
    'skill_invocation',
    'image_delivery',
}


class HistoryCache(OrderedDict):
    """由 AgentService 生命周期共享的有界会话缓存。"""

    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()


class SessionStore:
    """Persist append-only session history under a single user's directory.

    File operations run in worker threads so the async service layer never
    blocks its event loop. A distinct instance-level lock serializes each
    session's read-modify-append and metadata update operations.
    """

    def __init__(
        self,
        base_dir: str,
        user_id: str,
        locks: Optional[PathLocks] = None,
        *,
        cache: HistoryCache | None = None,
        cache_entries: int = 256,
        cache_bytes: int = 67108864,
    ) -> None:
        """Initialize an isolated store without creating any session files."""
        self.user_id = self._validate_id(user_id, 'user_id')
        self._root = Path(base_dir).expanduser().absolute()
        self.base_path = safe_path(self._root, 'users', self.user_id, 'sessions')
        self._locks = locks or PathLocks()
        self._cache = cache if cache is not None else HistoryCache()
        self._cache_entries, self._cache_bytes = cache_entries, cache_bytes
        self._cache_lock = self._cache.lock

    async def _io(self, function, *args, **kwargs):
        return await durable_io(function, *args, _executor=self._locks.executor, **kwargs)

    async def load_history(self, session_id: str) -> List[Dict[str, Any]]:
        """读取已校验的会话历史；损坏记录失败关闭，缺少日志返回空集合。"""
        session_id = self._validate_id(session_id, 'session_id')
        lock = self._lock_for(session_id)
        async with lock:
            await self._io(
                self._session_dir(session_id).mkdir,
                parents=True,
                exist_ok=True,
            )
            if not self._jsonl_path(session_id).exists():
                return []
            return deepcopy((await self._io(self._entry, session_id))['history'])

    async def append(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> None:
        """Append messages with monotonic sequence numbers and durable flushes."""
        session_id = self._validate_id(session_id, 'session_id')
        if not messages:
            return
        snapshot = [dict(message) for message in messages]
        lock = self._lock_for(session_id)
        async with lock:
            await self._io(
                self._append_sync,
                self._session_dir(session_id),
                session_id,
                snapshot,
            )

    async def get_metadata(self, session_id: str) -> Dict[str, Any]:
        """Read mutable session metadata, returning defaults when unavailable."""
        session_id = self._validate_id(session_id, 'session_id')
        lock = self._lock_for(session_id)
        async with lock:
            return await self._io(
                self._read_metadata_sync,
                self._meta_path(session_id),
                session_id,
            )

    async def update_metadata(
        self,
        session_id: str,
        updates: Dict[str, Any],
    ) -> None:
        """Atomically merge fields into a session's metadata sidecar."""
        session_id = self._validate_id(session_id, 'session_id')
        lock = self._lock_for(session_id)
        async with lock:
            await self._io(
                self._update_metadata_sync,
                self._meta_path(session_id),
                session_id,
                dict(updates),
            )

    async def list_sessions(self) -> List[Dict[str, Any]]:
        """List metadata for every session directory owned by this user."""
        await self._io(self.base_path.mkdir, parents=True, exist_ok=True)
        session_ids = await self._io(self._list_session_ids_sync)
        metadata = [await self.get_metadata(session_id) for session_id in session_ids]
        return sorted(
            metadata,
            key=lambda item: str(item.get('updated_at') or item.get('created_at') or ''),
            reverse=True,
        )

    async def create_session(self, session_id: Optional[str] = None) -> str:
        """Create a session log and metadata sidecar, then return its ID."""
        session_id = self._validate_id(session_id or str(uuid.uuid4()), 'session_id')
        lock = self._lock_for(session_id)
        async with lock:
            self._jsonl_path(session_id)
            self._meta_path(session_id)
            await self._io(self._create_sync, self._session_dir(session_id), session_id)
        return session_id

    def _session_dir(self, session_id: str) -> Path:
        """Return the isolated directory for ``session_id``."""
        safe_id = self._validate_id(session_id, 'session_id')
        return safe_path(self._root, 'users', self.user_id, 'sessions', safe_id)

    def _jsonl_path(self, session_id: str) -> Path:
        """Return the append-only JSONL path for ``session_id``."""
        safe_id = self._validate_id(session_id, 'session_id')
        return safe_path(self._session_dir(safe_id), f'{safe_id}.jsonl')

    def _meta_path(self, session_id: str) -> Path:
        """Return the mutable metadata sidecar path for ``session_id``."""
        safe_id = self._validate_id(session_id, 'session_id')
        return safe_path(self._session_dir(safe_id), f'{safe_id}.meta.json')

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.for_path(self._session_dir(session_id))

    def _list_session_ids_sync(self) -> List[str]:
        try:
            return [path.name for path in self.base_path.iterdir() if path.is_dir()]
        except OSError as exc:
            raise PersistenceError('无法读取会话目录') from exc

    def _read_validated_records(self, directory: Path, session_id: str) -> list[dict]:
        try:
            records = []
            log_path = safe_path(directory, f'{session_id}.jsonl')
            meta_path = safe_path(directory, f'{session_id}.meta.json')
            if log_path.exists():
                previous = -1
                for line in log_path.read_text(encoding='utf-8').splitlines():
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError('会话日志记录不是对象')
                    seq = record.get('seq')
                    if seq is not None:
                        if not isinstance(seq, int) or seq <= previous:
                            raise ValueError('会话日志 seq 损坏')
                        previous = seq
                    records.append(record)
            if meta_path.exists() and not isinstance(json.loads(meta_path.read_text(encoding='utf-8')), dict):
                raise ValueError('会话元数据不是对象')
            return records
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PersistenceError('会话文件损坏') from exc

    def _create_sync(self, directory: Path, session_id: str) -> None:
        if not self._jsonl_path(session_id).exists():
            SessionLog(directory, session_key=session_id)
            with self._jsonl_path(session_id).open('rb') as handle:
                os.fsync(handle.fileno())
            fsync_directory(directory)
            fsync_directory(directory.parent)
        self._entry(session_id)

    def _entry(self, session_id: str) -> dict:
        path = self._jsonl_path(session_id)
        signature = (path.stat().st_size, path.stat().st_mtime_ns)
        key = str(path)
        # 冷会话 IO 不占用全局缓存锁；相同会话由外层 PathLock 串行。
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached:
                if cached['signature'] != signature:
                    self._cache.pop(key, None)
                    raise PersistenceError('会话文件被协调器之外的写者修改')
                self._cache.move_to_end(key)
                return cached
        records = self._read_validated_records(self._session_dir(session_id), session_id)
        history = [r for r in records if r.get('role') and r.get('_type') not in _SPECIAL_RECORD_TYPES]
        entry = {
            'history': history,
            'seq': max((r.get('seq', -1) for r in records), default=-1) + 1,
            'signature': signature,
            'bytes': signature[0],
        }
        with self._cache_lock:
            self._cache[key] = entry
            self._trim_cache()
        return entry

    def _trim_cache(self):
        with self._cache_lock:
            while self._cache and (
                len(self._cache) > self._cache_entries
                or sum(e['bytes'] for e in self._cache.values()) > self._cache_bytes
            ):
                self._cache.popitem(last=False)

    def _append_sync(self, directory: Path, session_id: str, messages: List[Dict[str, Any]]) -> None:
        self._create_sync(directory, session_id)
        entry = self._entry(session_id)
        records = [
            {
                **message,
                'seq': entry['seq'] + i,
                'timestamp': message.get('timestamp') or datetime.now(timezone.utc).isoformat(),
            }
            for i, message in enumerate(messages)
        ]
        payload = ''.join(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n' for record in records)
        path = self._jsonl_path(session_id)
        try:
            with path.open('a', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            with self._cache_lock:
                self._cache.pop(str(path), None)
            raise
        entry['history'].extend(records)
        entry['seq'] += len(records)
        entry['signature'] = (path.stat().st_size, path.stat().st_mtime_ns)
        entry['bytes'] = entry['signature'][0]
        self._trim_cache()

    def _read_metadata_sync(self, path: Path, session_id: str) -> Dict[str, Any]:
        defaults = self._default_metadata(session_id)
        if not path.exists():
            return defaults
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise PersistenceError('会话元数据损坏') from exc
        if not isinstance(data, dict):
            raise PersistenceError('会话元数据不是对象')
        result = {**defaults, **data}
        result['session_id'] = session_id
        result['session_key'] = session_id
        return result

    def _update_metadata_sync(self, path: Path, session_id: str, updates: Dict[str, Any]) -> None:
        metadata = self._read_metadata_sync(path, session_id)
        metadata.update(updates)
        metadata['session_id'] = session_id
        metadata['session_key'] = session_id
        metadata['updated_at'] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(path, metadata, fsync=True)

    @staticmethod
    def _default_metadata(session_id: str) -> Dict[str, Any]:
        return {
            'session_id': session_id,
            'session_key': session_id,
            'created_at': '',
            'updated_at': '',
            'last_consolidated': 0,
            'round': 0,
            'title': '',
            'status': 'idle',
        }

    @staticmethod
    def _validate_id(value: str, name: str) -> str:
        return validate_id(value, name)
