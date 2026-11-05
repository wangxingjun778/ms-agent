"""Run 与 Recipe 的本地提交台账，JSONL 为权威来源，投影可以重建。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import weakref
from collections import OrderedDict
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


class StorageCorrupted(PersistenceError):
    """已提交日志损坏；隔离记录且禁止使用其幂等键重新执行。"""


class FileRunStore:
    """调用方在 read/commit/snapshot 的整个事务外持有 lock。"""

    def __init__(
        self,
        root: str | Path,
        locks: PathLocks | None = None,
        *,
        secret_values: tuple[str, ...] = (),
        cache_entries: int = 256,
        cache_bytes: int = 67108864,
        projection: str = 'terminal',
    ) -> None:
        self.root = Path(root).expanduser().absolute()
        self.locks = locks or PathLocks()
        self.lease = FileLease(self.root)
        self.healthy = True
        self.secret_values = secret_values
        self.projection = projection
        self.cache_entries, self.cache_bytes = cache_entries, cache_bytes
        self._cache: OrderedDict[str, tuple[dict, list, int, str, int, int]] = OrderedDict()
        self._cache_lock = threading.RLock()
        self._cached_bytes = 0
        self._changes: weakref.WeakValueDictionary[str, asyncio.Event] = weakref.WeakValueDictionary()
        self.corrupted: set[str] = set()
        self.epoch = ''
        self._epoch_path = safe_path(self.root, '.coordinator.epoch')

    def change_event(self, user_id: str, kind: str, identifier: str) -> asyncio.Event:
        key = str(self.directory(user_id, kind, identifier))
        return self._changes.setdefault(key, asyncio.Event())

    def _remember(
        self, directory: Path, state: dict, events: list, seq: int, checksum: str, event_bytes: int | None = None
    ) -> None:
        key = str(directory)
        if event_bytes is None:
            event_bytes = sum(len(canonical(event).encode('utf-8')) for event in events)
        size = len(canonical(state).encode('utf-8')) + event_bytes
        with self._cache_lock:
            previous = self._cache.pop(key, None)
            if previous:
                self._cached_bytes -= previous[4]
            self._cache[key] = (deepcopy(state), list(events), seq, checksum, size, event_bytes)
            self._cached_bytes += size
            while len(self._cache) > self.cache_entries or self._cached_bytes > self.cache_bytes:
                _, removed = self._cache.popitem(last=False)
                self._cached_bytes -= removed[4]

    def _cached(self, directory: Path):
        with self._cache_lock:
            entry = self._cache.get(str(directory))
            if entry:
                self._cache.move_to_end(str(directory))
                return (deepcopy(entry[0]), list(entry[1]), *entry[2:])
        return None

    async def initialize_epoch(self) -> None:
        # 仅在已获得 flock 后写入，周期检查只检测意外接管，不作为互斥证明。
        self.epoch = uuid4().hex
        await self._io(atomic_write_text, self._epoch_path, self.epoch, fsync=True, sync_directory=True)

    async def check_epoch(self) -> None:
        value = await self._io(self._epoch_path.read_text)
        if value != self.epoch:
            self.healthy = False
            raise PersistenceError('协调器 epoch 已变化，停止写入')

    def directory(self, user_id: str, kind: str, identifier: str) -> Path:
        if kind not in {'runs', 'recipes'}:
            raise ValueError('不支持的台账类型')
        return safe_path(self.root, 'users', validate_id(user_id, 'user_id'), kind, validate_id(identifier))

    def lock(self, user_id: str, kind: str, identifier: str):
        return self.locks.for_path(self.directory(user_id, kind, identifier))

    async def read(self, user_id: str, kind: str, identifier: str) -> tuple[dict[str, Any] | None, list[dict]]:
        directory = self.directory(user_id, kind, identifier)
        if not self.healthy:
            raise PersistenceError('台账不可用')
        if str(directory) in self.corrupted:
            raise StorageCorrupted('已提交台账损坏')
        entry = self._cached(directory)
        if entry:
            return entry[0], deepcopy(entry[1])
        state, events = await self._io(self._read_sync, directory)
        return state, deepcopy(events)

    async def read_state(self, user_id: str, kind: str, identifier: str) -> dict | None:
        directory = self.directory(user_id, kind, identifier)
        if not self.healthy:
            raise PersistenceError('台账不可用')
        if str(directory) in self.corrupted:
            raise StorageCorrupted('已提交台账损坏')
        with self._cache_lock:
            entry = self._cache.get(str(directory))
            if entry:
                self._cache.move_to_end(str(directory))
                return deepcopy(entry[0])
        return (await self.read(user_id, kind, identifier))[0]

    async def read_events(self, user_id: str, kind: str, identifier: str, after_seq: int):
        state = await self.read_state(user_id, kind, identifier)
        directory = self.directory(user_id, kind, identifier)
        with self._cache_lock:
            entry = self._cache.get(str(directory))
            if entry:
                return state, deepcopy(entry[1][after_seq:])
        state, events = await self.read(user_id, kind, identifier)
        return state, events[after_seq:]

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
        # 在启动任何文件操作前检查序列化；请求数据错误不能熔断整个存储。
        canonical(updates)
        canonical(data)
        directory = self.directory(user_id, kind, identifier)
        if str(directory) in self.corrupted:
            raise StorageCorrupted('已提交台账损坏')
        state = await self._io(self._commit_sync, directory, updates, event_type, data)
        event = self._changes.pop(str(directory), None)
        if event:
            event.set()
        return state

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

    def _index_path(self, user_id: str, kind: str, identifier: str) -> Path:
        return safe_path(self.root, '.active', digest([user_id, kind, identifier]) + '.json')

    def _index_entries(self) -> list:
        directory = safe_path(self.root, '.active')
        if not directory.exists():
            return []
        entries = []
        for path in directory.iterdir():
            if path.suffix == '.json':
                item = json.loads(safe_path(directory, path.name).read_text())
                self.directory(*item)
                if path != self._index_path(*item):
                    raise ValueError('活跃索引归属无效')
                entries.append(tuple(item))
        return entries

    def _mark_active(self, directory: Path) -> None:
        user_id, kind, identifier = directory.parts[-3:]
        path = self._index_path(user_id, kind, identifier)
        if not path.exists():
            self._ensure_directory(path.parent)
            atomic_write_json(path, [user_id, kind, identifier], fsync=True, sync_directory=True)

    def _unmark_active(self, directory: Path) -> None:
        path = self._index_path(*directory.parts[-3:])
        # 对象由 directory() 校验派生，只能删除本台账的活跃标记。
        if path.exists():
            path.unlink()
            fsync_directory(path.parent)

    async def recover(self) -> None:
        manifest = safe_path(self.root, '.index-ready')
        initialize_index = not manifest.exists()
        entries = await self.list_entries() if initialize_index else await self._io(self._index_entries)
        for user_id, kind, identifier in entries:
            async with self.lock(user_id, kind, identifier):
                try:
                    state, _ = await self.read(user_id, kind, identifier)
                except StorageCorrupted:
                    continue
                if state is None:
                    continue
                if state.get('status') in TERMINAL:
                    await self._io(self._unmark_active, self.directory(user_id, kind, identifier))
                if initialize_index and state.get('status') not in TERMINAL and state.get('status') != 'missing':
                    await self._io(self._mark_active, self.directory(user_id, kind, identifier))
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
        if initialize_index:
            await self._io(atomic_write_text, manifest, '1', fsync=True, sync_directory=True)

    async def _io(self, function, *args, **kwargs):
        if not self.healthy:
            raise PersistenceError('台账已进入只读故障状态')
        try:
            return await durable_io(function, *args, _executor=self.locks.executor, **kwargs)
        except StorageCorrupted:
            raise
        except (OSError, ValueError, TypeError) as exc:
            self.healthy = False
            raise PersistenceError('台账读取或提交失败') from exc

    def _ensure_directory(self, directory: Path) -> None:
        if directory.exists():
            return
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
                    self.corrupted.add(str(directory))
                    raise StorageCorrupted('已提交日志损坏，不能丢弃幂等记录') from None
                backup = safe_path(directory, f'journal.corrupt-{uuid4().hex}.jsonl')
                with backup.open('xb') as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                atomic_write_text(path, valid.decode('utf-8'), fsync=True, sync_directory=True)
        if state:
            self._remember(directory, state, events, len(valid.splitlines()), previous)
        return (state or None), events

    def _commit_sync(self, directory: Path, updates: dict, event_type: str | None, data: dict | None) -> dict:
        self._ensure_directory(directory)
        entry = self._cached(directory)
        if entry is None:
            state, events = self._read_sync(directory)
            entry = self._cached(directory)
        state = (entry[0] if entry else state) or {}
        events = entry[1] if entry else events
        path = safe_path(directory, 'journal.jsonl')
        if entry:
            record_seq, previous = entry[2] + 1, entry[3]
        else:
            lines = path.read_bytes().splitlines() if path.exists() else []
            last = json.loads(lines[-1]) if lines else {}
            record_seq, previous = last.get('record_seq', 0) + 1, last.get('checksum', '')
        next_status = updates.get('status', state.get('status'))
        if next_status not in TERMINAL and next_status != 'missing':
            # 标记必须先于 journal 持久化，崩溃至多产生可恢复的冗余标记。
            self._mark_active(directory)
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
        if self.projection == 'always' or (self.projection == 'terminal' and state.get('status') in TERMINAL):
            atomic_write_json(safe_path(directory, 'state.json'), state, fsync=True, sync_directory=True)
        if state.get('status') in TERMINAL:
            self._unmark_active(directory)
        if event:
            events.append(event)
        event_bytes = entry[5] + (len(canonical(event).encode('utf-8')) if event else 0) if entry else None
        self._remember(directory, state, events, record_seq, record['checksum'], event_bytes)
        return deepcopy(state)
