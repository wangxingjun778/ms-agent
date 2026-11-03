"""Session store with user_id / session_id isolation."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ms_agent.session.session_log import SessionLog
from ms_agent.utils.atomic_file import atomic_write_json
from ms_agent.utils.logger import get_logger

__all__ = ['SessionStore']

logger = get_logger()

_SPECIAL_RECORD_TYPES = {
    'error',
    'loop_end',
    'permission',
    'compaction_event',
    'skill_invocation',
    'image_delivery',
}


class SessionStore:
    """Persist append-only session history under a single user's directory.

    File operations run in worker threads so the async service layer never
    blocks its event loop. A distinct instance-level lock serializes each
    session's read-modify-append and metadata update operations.
    """

    def __init__(self, base_dir: str, user_id: str) -> None:
        """Initialize an isolated store without creating any session files."""
        self.user_id = self._validate_id(user_id, 'user_id')
        self.base_path = Path(base_dir).expanduser() / 'users' / self.user_id / 'sessions'
        self._locks: Dict[str, asyncio.Lock] = {}

    async def load_history(self, session_id: str) -> List[Dict[str, Any]]:
        """Return conversational records from a session's append-only log.

        Malformed JSON lines and non-message event records are ignored. The
        session directory is created on demand, but a missing log remains
        missing until the first append or explicit session creation.
        """
        session_id = self._validate_id(session_id, 'session_id')
        lock = self._lock_for(session_id)
        async with lock:
            await asyncio.to_thread(
                self._session_dir(session_id).mkdir,
                parents=True,
                exist_ok=True,
            )
            return await asyncio.to_thread(self._read_history_sync, self._jsonl_path(session_id))

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
            await asyncio.to_thread(
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
            return await asyncio.to_thread(
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
            await asyncio.to_thread(
                self._update_metadata_sync,
                self._meta_path(session_id),
                session_id,
                dict(updates),
            )

    async def list_sessions(self) -> List[Dict[str, Any]]:
        """List metadata for every session directory owned by this user."""
        await asyncio.to_thread(self.base_path.mkdir, parents=True, exist_ok=True)
        session_ids = await asyncio.to_thread(self._list_session_ids_sync)
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
            await asyncio.to_thread(
                SessionLog,
                self._session_dir(session_id),
                session_id,
            )
        return session_id

    def _session_dir(self, session_id: str) -> Path:
        """Return the isolated directory for ``session_id``."""
        safe_id = self._validate_id(session_id, 'session_id')
        return self.base_path / safe_id

    def _jsonl_path(self, session_id: str) -> Path:
        """Return the append-only JSONL path for ``session_id``."""
        safe_id = self._validate_id(session_id, 'session_id')
        return self._session_dir(safe_id) / f'{safe_id}.jsonl'

    def _meta_path(self, session_id: str) -> Path:
        """Return the mutable metadata sidecar path for ``session_id``."""
        safe_id = self._validate_id(session_id, 'session_id')
        return self._session_dir(safe_id) / f'{safe_id}.meta.json'

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def _list_session_ids_sync(self) -> List[str]:
        try:
            return [path.name for path in self.base_path.iterdir() if path.is_dir()]
        except OSError as exc:
            logger.warning('Unable to list sessions in %s: %s', self.base_path, exc)
            return []

    @staticmethod
    def _append_sync(
        directory: Path,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> None:
        log = SessionLog(directory, session_key=session_id)
        log.append_messages(messages)

    @staticmethod
    def _read_history_sync(path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        messages: List[Dict[str, Any]] = []
        try:
            with path.open('r', encoding='utf-8') as handle:
                for line_number, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        logger.warning('Ignoring malformed session record %s:%s', path, line_number)
                        continue
                    if not isinstance(record, dict):
                        continue
                    record_type = record.get('_type')
                    if record_type in _SPECIAL_RECORD_TYPES:
                        continue
                    if not record.get('role'):
                        continue
                    messages.append(record)
        except OSError as exc:
            logger.warning('Unable to read session history %s: %s', path, exc)
        return messages

    @classmethod
    def _read_metadata_sync(
        cls,
        path: Path,
        session_id: str,
    ) -> Dict[str, Any]:
        defaults = cls._default_metadata(session_id)
        if not path.exists():
            return defaults
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning('Unable to read session metadata %s: %s', path, exc)
            return defaults
        if not isinstance(data, dict):
            return defaults
        result = {**defaults, **data}
        result['session_id'] = session_id
        result['session_key'] = session_id
        return result

    @classmethod
    def _update_metadata_sync(
        cls,
        path: Path,
        session_id: str,
        updates: Dict[str, Any],
    ) -> None:
        metadata = cls._read_metadata_sync(path, session_id)
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
        text = str(value or '').strip()
        if (
            not text
            or text in {'.', '..'}
            or '/' in text
            or '\\' in text
            or os.sep in text
            or (os.altsep and os.altsep in text)
        ):
            raise ValueError(f'{name} must be a non-empty path component')
        return text
