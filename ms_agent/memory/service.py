"""Memory service with per-user isolation, no process-level singletons."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ms_agent.memory.unified.backends.file_based import FileBasedBackend
from ms_agent.memory.unified.config import MemoryConfig
from ms_agent.utils.atomic_file import atomic_write_json, atomic_write_text
from ms_agent.utils.logger import get_logger

__all__ = ['MemoryService']

logger = get_logger()

_INGEST_ROLES = {'user', 'assistant'}
_LEDGER_LIMIT = 4096


class MemoryService:
    """Provide isolated, local-disk memory for one user.

    ``memories.jsonl`` is the structured source of truth, while ``MEMORY.md``
    is a readable projection compatible with the existing file-based memory
    backend. All mutable state and synchronization are owned by this instance.
    """

    def __init__(
        self,
        base_dir: str,
        user_id: str,
        llm_client: Any = None,
    ) -> None:
        """Configure the service; backend and directories are initialized lazily."""
        self.user_id = self._validate_user_id(user_id)
        self.memory_dir = Path(base_dir).expanduser() / 'users' / self.user_id / 'memory'
        self.llm_client = llm_client
        self._lock = asyncio.Lock()
        self._backend: Optional[FileBasedBackend] = None
        self._closed = False

    async def _ensure_backend(self) -> FileBasedBackend:
        """Lazily construct and start the existing file-based backend."""
        async with self._lock:
            if self._closed:
                raise RuntimeError('MemoryService is closed')
            if self._backend is None:
                await asyncio.to_thread(self.memory_dir.mkdir, parents=True, exist_ok=True)
                config = MemoryConfig(
                    base_dir=str(self.memory_dir),
                    user_id=self.user_id,
                    storage_backend='file',
                    retrieval_strategy='full_dump',
                    extraction_strategy='tool_based',
                    security_scan=False,
                    char_limit=1_000_000,
                )
                backend = FileBasedBackend(config)
                start_kwargs = {'llm': self.llm_client} if self.llm_client is not None else {}
                await backend.start(**start_kwargs)
                self._backend = backend
            return self._backend

    async def recall(
        self,
        messages: List[Dict[str, Any]],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Recall memories matching the most recent conversational messages."""
        if self._closed or top_k <= 0 or not self.memory_dir.exists():
            return []
        recent: List[str] = []
        for message in reversed(messages):
            if len(recent) >= 5:
                break
            text = self._message_text(message)
            if text:
                recent.append(text)
        query = ' '.join(reversed(recent)).strip()
        if not query:
            return []
        return await self.search(query, top_k=top_k)

    async def ingest(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> None:
        """Persist unseen conversational content using a content-hash ledger.

        Hashes advance only after the JSONL append and the readable markdown
        projection both succeed. Existing JSONL hashes also suppress duplicates
        if a previous attempt persisted entries but failed before ledger commit.
        """
        if self._closed or not messages:
            return
        backend = await self._ensure_backend()
        async with self._lock:
            try:
                entries = await asyncio.to_thread(self._read_entries_sync)
                state = await asyncio.to_thread(self._read_state_sync)
                ledger = list(state.get('hashes') or [])
                seen: Set[str] = set(str(item) for item in ledger)
                seen.update(
                    str(entry.get('metadata', {}).get('content_hash'))
                    for entry in entries
                    if isinstance(entry.get('metadata'), dict) and entry['metadata'].get('content_hash')
                )

                batch_hashes: Set[str] = set()
                new_hashes: List[str] = []
                new_entries: List[Dict[str, Any]] = []
                for message in messages:
                    role = str(message.get('role', ''))
                    content = self._message_text(message)
                    if role not in _INGEST_ROLES or not content:
                        continue
                    content_hash = self._content_hash(role, content)
                    if content_hash in seen or content_hash in batch_hashes:
                        continue
                    batch_hashes.add(content_hash)
                    new_hashes.append(content_hash)
                    new_entries.append(
                        self._new_entry(
                            content=content,
                            category='conversation',
                            metadata={
                                'session_id': session_id,
                                'role': role,
                                'content_hash': content_hash,
                            },
                        )
                    )

                if new_entries:
                    await asyncio.to_thread(self._append_entries_sync, new_entries)
                    entries.extend(new_entries)

                if not batch_hashes:
                    return

                await asyncio.to_thread(self._write_memory_markdown_sync, entries)
                for content_hash in new_hashes:
                    if content_hash not in seen:
                        ledger.append(content_hash)
                        seen.add(content_hash)
                ledger = ledger[-_LEDGER_LIMIT:]

                sessions = state.get('sessions')
                if not isinstance(sessions, dict):
                    sessions = {}
                sessions[str(session_id)] = {
                    'hashes': new_hashes,
                    'updated_at': self._now(),
                }
                await asyncio.to_thread(
                    atomic_write_json,
                    self._state_path,
                    {
                        'version': 1,
                        'hashes': ledger,
                        'sessions': sessions,
                    },
                )
                backend.invalidate()
            except (OSError, TypeError, ValueError) as exc:
                logger.warning('Memory ingest failed for user %s: %s', self.user_id, exc)

    async def search(
        self,
        query: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Search structured memory entries using deterministic keyword scoring."""
        query = str(query or '').strip()
        if self._closed or not query or top_k <= 0 or not self._entries_path.exists():
            return []
        await self._ensure_backend()
        async with self._lock:
            entries = await asyncio.to_thread(self._read_entries_sync)
        return self._rank_entries(query, entries, top_k)

    async def add(
        self,
        content: str,
        category: str = 'general',
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Directly add one memory entry and refresh ``MEMORY.md``."""
        content = str(content or '').strip()
        if not content:
            raise ValueError('memory content must not be empty')
        backend = await self._ensure_backend()
        entry = self._new_entry(
            content=content,
            category=str(category or 'general'),
            metadata=dict(metadata or {}),
        )
        async with self._lock:
            await asyncio.to_thread(self._append_entries_sync, [entry])
            entries = await asyncio.to_thread(self._read_entries_sync)
            await asyncio.to_thread(self._write_memory_markdown_sync, entries)
            backend.invalidate()

    async def close(self) -> None:
        """Close backend resources; closing is idempotent and terminal."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            backend = self._backend
            self._backend = None
            if backend is not None:
                try:
                    await backend.close()
                except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                    logger.warning('Closing memory backend failed: %s', exc)

    @property
    def _entries_path(self) -> Path:
        return self.memory_dir / 'memories.jsonl'

    @property
    def _state_path(self) -> Path:
        return self.memory_dir / 'ingest_state.json'

    @property
    def _markdown_path(self) -> Path:
        return self.memory_dir / 'MEMORY.md'

    def _read_entries_sync(self) -> List[Dict[str, Any]]:
        if not self._entries_path.exists():
            return []
        entries: List[Dict[str, Any]] = []
        try:
            with self._entries_path.open('r', encoding='utf-8') as handle:
                for line_number, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        logger.warning('Ignoring malformed memory record %s:%s', self._entries_path, line_number)
                        continue
                    if isinstance(entry, dict) and isinstance(entry.get('content'), str):
                        entries.append(entry)
        except OSError as exc:
            logger.warning('Unable to read memory file %s: %s', self._entries_path, exc)
        return entries

    def _append_entries_sync(self, entries: List[Dict[str, Any]]) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        with self._entries_path.open('a', encoding='utf-8') as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    def _read_state_sync(self) -> Dict[str, Any]:
        if not self._state_path.exists():
            return {'version': 1, 'hashes': [], 'sessions': {}}
        try:
            state = json.loads(self._state_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning('Unable to read ingest state %s: %s', self._state_path, exc)
            return {'version': 1, 'hashes': [], 'sessions': {}}
        return (
            state
            if isinstance(state, dict)
            else {
                'version': 1,
                'hashes': [],
                'sessions': {},
            }
        )

    def _write_memory_markdown_sync(
        self,
        entries: List[Dict[str, Any]],
    ) -> None:
        grouped: Dict[str, List[str]] = {}
        for entry in entries:
            content = str(entry.get('content', '')).strip()
            if not content:
                continue
            category = str(entry.get('category') or 'general')
            grouped.setdefault(category, []).append(content)

        sections: List[str] = []
        for category, contents in grouped.items():
            lines = [f'## {category}']
            for content in contents:
                rendered = content.replace('\n', '\n  ')
                lines.append(f'- {rendered}')
            sections.append('\n'.join(lines))
        text = '\n\n'.join(sections)
        atomic_write_text(
            self._markdown_path,
            f'{text}\n' if text else '',
            fsync=True,
        )

    @classmethod
    def _rank_entries(
        cls,
        query: str,
        entries: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        normalized_query = query.casefold()
        terms = set(re.findall(r'[\w-]+', normalized_query, flags=re.UNICODE))
        scored = []
        for index, entry in enumerate(entries):
            content = str(entry.get('content', '')).casefold()
            category = str(entry.get('category', '')).casefold()
            if not content:
                continue
            score = 0
            if normalized_query in content:
                score += max(3, len(terms) * 2)
            score += sum(2 for term in terms if term in content)
            score += sum(1 for term in terms if term in category)
            if score:
                scored.append((score, index, entry))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [dict(item[2]) for item in scored[:top_k]]

    @staticmethod
    def _message_text(message: Dict[str, Any]) -> str:
        content = message.get('content', '')
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [str(item.get('text', '')).strip() for item in content if isinstance(item, dict)]
            return ' '.join(part for part in parts if part)
        return str(content or '').strip()

    @classmethod
    def _new_entry(
        cls,
        content: str,
        category: str,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            'id': str(uuid.uuid4()),
            'content': content,
            'category': category,
            'created_at': cls._now(),
            'metadata': metadata,
        }

    @staticmethod
    def _content_hash(role: str, content: str) -> str:
        raw = f'{role}\x1f{content}'
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _validate_user_id(user_id: str) -> str:
        value = str(user_id or '').strip()
        if (
            not value
            or value in {'.', '..'}
            or '/' in value
            or '\\' in value
            or os.sep in value
            or (os.altsep and os.altsep in value)
        ):
            raise ValueError('user_id must be a non-empty path component')
        return value
