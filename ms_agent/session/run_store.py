"""Run 与 Recipe 的本地提交台账，JSONL 为权威来源，投影可以重建。"""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ms_agent.session.locking import FileLease, PathLocks, PersistenceError, durable_io, safe_path, validate_id
from ms_agent.utils.atomic_file import atomic_write_json, atomic_write_text, fsync_directory

TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'interrupted'})
_SECRET_KEYS = frozenset({'api_key', 'token', 'authorization', 'credentials', 'password', 'secret'})


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(data: Any) -> str:
    return hashlib.sha256(canonical(data).encode('utf-8')).hexdigest()


def without_secrets(data: Any, values: tuple[str, ...] = ()) -> Any:
    if isinstance(data, dict):
        return {key: without_secrets(value, values) for key, value in data.items() if key.lower() not in _SECRET_KEYS}
    if isinstance(data, list):
        return [without_secrets(item, values) for item in data]
    if isinstance(data, str):
        for value in values:
            if value:
                data = data.replace(value, '[redacted]')
    return data


class FileRunStore:
    """调用方在 read/commit/snapshot 的整个事务外持有 lock。"""

    def __init__(
        self,
        root: str | Path,
        locks: PathLocks | None = None,
        *,
        secret_values: tuple[str, ...] = (),
    ) -> None:
        self.root = Path(root).expanduser().absolute()
        self.locks = locks or PathLocks()
        self.lease = FileLease(self.root)
        self.healthy = True
        self.secret_values = secret_values

    def directory(self, user_id: str, kind: str, identifier: str) -> Path:
        if kind not in {'runs', 'recipes'}:
            raise ValueError('不支持的台账类型')
        return safe_path(self.root, 'users', validate_id(user_id, 'user_id'), kind, validate_id(identifier))

    def lock(self, user_id: str, kind: str, identifier: str):
        return self.locks.for_path(self.directory(user_id, kind, identifier))

    async def read(self, user_id: str, kind: str, identifier: str) -> tuple[dict[str, Any] | None, list[dict]]:
        return await self._io(self._read_sync, self.directory(user_id, kind, identifier))

    async def snapshot(self, user_id: str, kind: str, identifier: str, name: str, data: Any) -> None:
        if name not in {'input_snapshot.json', 'execution_context.json', 'request.json', 'result.json'}:
            raise ValueError('不支持的快照文件')
        directory = self.directory(user_id, kind, identifier)
        await self._io(self._snapshot_sync, directory, name, without_secrets(data, self.secret_values))

    async def read_snapshot(self, user_id: str, kind: str, identifier: str, name: str) -> Any:
        path = safe_path(self.directory(user_id, kind, identifier), name)
        return await self._io(lambda: json.loads(path.read_text(encoding='utf-8')))

    async def commit(
        self,
        user_id: str,
        kind: str,
        identifier: str,
        updates: dict[str, Any],
        event_type: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._io(self._commit_sync, self.directory(user_id, kind, identifier), updates, event_type, data)

    async def list_entries(self) -> list[tuple[str, str, str]]:
        def scan():
            users = safe_path(self.root, 'users')
            if not users.exists():
                return []
            result = []
            for user in users.iterdir():
                validate_id(user.name, 'user_id')
                safe_path(self.root, 'users', user.name)
                if not user.is_dir():
                    continue
                for kind in ('runs', 'recipes'):
                    parent = safe_path(self.root, 'users', user.name, kind)
                    if parent.exists():
                        for item in parent.iterdir():
                            self.directory(user.name, kind, item.name)
                            if item.is_dir():
                                result.append((user.name, kind, item.name))
            return result

        return await self._io(scan)

    async def recover(self) -> None:
        for user_id, kind, identifier in await self.list_entries():
            async with self.lock(user_id, kind, identifier):
                state, _ = await self.read(user_id, kind, identifier)
                if state is None:
                    continue
                if state.get('status') not in TERMINAL and state.get('status') != 'missing':
                    commands = deepcopy(state.get('commands', {}))
                    for receipt in commands.values():
                        if receipt['status'] == 'accepted':
                            receipt.update(status='rejected', reason_code='SERVICE_INTERRUPTED')
                    await self.commit(
                        user_id,
                        kind,
                        identifier,
                        {
                            'status': 'interrupted',
                            'pending_interaction': None,
                            'commands': commands,
                            'error': {'code': 'SERVICE_INTERRUPTED', 'message': '服务中断，未自动续跑'},
                            'requires_reconciliation': True,
                        },
                        'run_interrupted' if kind == 'runs' else None,
                        {'status': 'interrupted'},
                    )

    async def _io(self, function, *args):
        if not self.healthy:
            raise PersistenceError('台账已进入只读故障状态')
        try:
            return await durable_io(function, *args)
        except (OSError, ValueError, TypeError) as exc:
            self.healthy = False
            raise PersistenceError('台账读取或提交失败') from exc

    def _ensure_directory(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        cursor = directory
        while cursor != self.root:
            fsync_directory(cursor.parent)
            cursor = cursor.parent
        fsync_directory(self.root)

    def _snapshot_sync(self, directory: Path, name: str, data: Any) -> None:
        self._ensure_directory(directory)
        atomic_write_json(safe_path(directory, name), data, fsync=True, sync_directory=True)

    def _read_sync(self, directory: Path) -> tuple[dict[str, Any] | None, list[dict]]:
        path = safe_path(directory, 'journal.jsonl')
        if not path.exists():
            return None, []
        raw = path.read_bytes()
        lines = raw.splitlines(keepends=True)
        state: dict[str, Any] = {}
        events = []
        previous = ''
        valid = bytearray()
        for index, line in enumerate(lines):
            try:
                record = json.loads(line)
                if not line.endswith(b'\n'):
                    raise ValueError('未提交的尾行')
                payload = record['payload']
                if record['record_seq'] != index + 1 or record['previous'] != previous:
                    raise ValueError('日志顺序损坏')
                expected = digest({'record_seq': index + 1, 'previous': previous, 'payload': payload})
                if record['checksum'] != expected:
                    raise ValueError('日志校验失败')
                state.update(payload['updates'])
                if payload.get('event'):
                    event = payload['event']
                    if event['agent_seq'] != len(events) + 1:
                        raise ValueError('事件序号损坏')
                    events.append(event)
                previous = expected
                valid.extend(line)
            except (ValueError, KeyError, TypeError):
                if index != len(lines) - 1 or line.endswith(b'\n'):
                    raise ValueError('已提交日志损坏，不能丢弃幂等记录') from None
                backup = safe_path(directory, f'journal.corrupt-{uuid4().hex}.jsonl')
                with backup.open('xb') as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                atomic_write_text(path, valid.decode('utf-8'), fsync=True, sync_directory=True)
        return (state or None), events

    def _commit_sync(self, directory: Path, updates: dict, event_type: str | None, data: dict | None) -> dict:
        self._ensure_directory(directory)
        state, events = self._read_sync(directory)
        state = state or {}
        path = safe_path(directory, 'journal.jsonl')
        previous_record = None
        if path.exists():
            lines = path.read_bytes().splitlines()
            if lines:
                previous_record = json.loads(lines[-1])
        record_seq = previous_record['record_seq'] + 1 if previous_record else 1
        previous = previous_record['checksum'] if previous_record else ''
        event = None
        updates = deepcopy(without_secrets(updates, self.secret_values))
        updates['updated_at'] = now()
        if event_type:
            seq = len(events) + 1
            event = {
                'schema_version': 1,
                'run_uuid': directory.name,
                'agent_seq': seq,
                'event_type': event_type,
                'timestamp': now(),
                'data': without_secrets(data or {}, self.secret_values),
            }
            updates['last_agent_seq'] = seq
        payload = {'updates': updates, 'event': event}
        record = {'record_seq': record_seq, 'previous': previous, 'payload': payload}
        record['checksum'] = digest(record)
        serialized = (canonical(record) + '\n').encode('utf-8')
        with path.open('ab') as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(directory)
        state.update(updates)
        # journal 已成功提交；投影失败不撤销提交，但仍进入故障状态阻止后续副作用。
        atomic_write_json(safe_path(directory, 'state.json'), state, fsync=True, sync_directory=True)
        return deepcopy(state)
