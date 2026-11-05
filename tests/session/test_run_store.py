"""文件台账的崩溃、隔离、校验与协调器边界。"""

import asyncio
import errno
import json
import threading

import pytest

from ms_agent.memory.service import MemoryService
from ms_agent.session.locking import PathLocks, PersistenceError, durable_io
from ms_agent.session.run_store import FileRunStore, StorageCorrupted
from ms_agent.session.store import HistoryCache, SessionStore


@pytest.mark.asyncio
async def test_recover_incomplete_run_and_preserve_terminal(tmp_path):
    store = FileRunStore(tmp_path)
    for identifier, status in [('r1', 'running'), ('r2', 'succeeded')]:
        await store.commit(
            'u1',
            'runs',
            identifier,
            {
                'status': status,
                'run_uuid': identifier,
                'commands': {},
                'result': {'content': '已提交'},
                'last_agent_seq': 0,
            },
            'run_started',
            {},
        )
    await store.recover()
    first, _ = await store.read('u1', 'runs', 'r1')
    second, _ = await store.read('u1', 'runs', 'r2')
    assert first['status'] == 'interrupted' and first['result']['content'] == '已提交'
    assert second['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_torn_tail_recovery_and_middle_corruption(tmp_path):
    store = FileRunStore(tmp_path)
    await store.commit('u1', 'runs', 'r1', {'status': 'running'}, 'run_started', {})
    path = store.directory('u1', 'runs', 'r1') / 'journal.jsonl'
    with path.open('ab') as handle:
        handle.write(b'{"partial":')
    store = FileRunStore(tmp_path)  # 模拟崩溃后重启，从持久日志恢复。
    state, events = await store.read('u1', 'runs', 'r1')
    assert state['status'] == 'running' and len(events) == 1
    assert list(path.parent.glob('journal.corrupt-*.jsonl'))
    await store.commit('u1', 'runs', 'r1', {'status': 'succeeded'}, 'run_terminal', {})
    lines = path.read_text().splitlines()
    lines[0] = lines[0].replace('running', 'broken')
    path.write_text('\n'.join(lines) + '\n')
    store = FileRunStore(tmp_path)
    with pytest.raises(StorageCorrupted):
        await store.read('u1', 'runs', 'r1')
    assert store.healthy and str(path.parent) in store.corrupted


@pytest.mark.asyncio
async def test_disk_full_fails_closed(tmp_path, monkeypatch):
    store = FileRunStore(tmp_path)

    def fail(*args):
        raise OSError(errno.ENOSPC, 'disk full')

    monkeypatch.setattr(store, '_commit_sync', fail)
    with pytest.raises(PersistenceError):
        await store.commit('u1', 'runs', 'r1', {'status': 'queued'})
    assert not store.healthy
    with pytest.raises(PersistenceError):
        await store.read('u1', 'runs', 'r1')


def test_exclusive_coordinator_and_symlinks(tmp_path):
    first, second = FileRunStore(tmp_path), FileRunStore(tmp_path)
    first.lease.acquire()
    try:
        with pytest.raises(RuntimeError, match='占用'):
            second.lease.acquire()
    finally:
        first.lease.release()
    second.lease.acquire()
    second.lease.release()
    (tmp_path / 'users').mkdir(exist_ok=True)
    (tmp_path / 'users' / 'u2').mkdir()
    (tmp_path / 'users' / 'u1').symlink_to(tmp_path / 'users' / 'u2', target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        first.directory('u1', 'runs', 'r1')
    with pytest.raises(ValueError):
        first.directory('../escape', 'runs', 'r1')


@pytest.mark.asyncio
async def test_shared_session_locks_and_memory_isolation(tmp_path):
    locks = PathLocks()
    cache = HistoryCache()
    stores = [SessionStore(str(tmp_path), 'u1', locks, cache=cache) for _ in range(2)]
    await asyncio.gather(
        *(stores[index % 2].append('s1', [{'role': 'user', 'content': str(index)}]) for index in range(20))
    )
    history = await stores[0].load_history('s1')
    assert len(history) == 20 and len({record['seq'] for record in history}) == 20
    memories = [
        MemoryService(str(tmp_path), 'u1', session_id='s1', locks=locks),
        MemoryService(str(tmp_path), 'u1', session_id='s1', locks=locks),
    ]
    await asyncio.gather(*(memory.ingest('s1', [{'role': 'user', 'content': '专属记忆 alpha'}]) for memory in memories))
    rows = memories[0]._entries_path.read_text().splitlines()
    assert len(rows) == 1
    isolated = MemoryService(str(tmp_path), 'u1', session_id='s2', locks=locks)
    foreign = MemoryService(str(tmp_path), 'u2', session_id='s1', locks=locks)
    assert not await isolated.search('alpha') and not await foreign.search('alpha')
    assert await memories[0].search('alpha')
    for memory in [*memories, isolated, foreign]:
        await memory.close()


@pytest.mark.asyncio
async def test_cancelled_thread_finishes_before_lock_release():
    started, release = threading.Event(), threading.Event()
    lock = asyncio.Lock()
    finished = []

    def write():
        started.set()
        release.wait(2)
        finished.append(True)

    async def transaction():
        async with lock:
            await durable_io(write)

    task = asyncio.create_task(transaction())
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.01)
    assert lock.locked() and not finished
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished and not lock.locked()
