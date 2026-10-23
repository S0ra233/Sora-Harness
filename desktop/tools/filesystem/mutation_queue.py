"""按实际路径串行修改；线程锁允许不同GUI工作线程的事件循环共用。"""
import asyncio
import os
import threading
from contextlib import asynccontextmanager


_guard = threading.Lock()
_entries: dict[str, tuple[threading.Lock, int]] = {}


@asynccontextmanager
async def mutation_queue(*paths):
    """多路径按固定顺序获取，取消等待不会遗留锁或覆盖另一次修改。"""
    keys = sorted({os.path.normcase(str(path)) for path in paths})
    locks, acquired = [], []
    with _guard:
        for key in keys:
            lock, users = _entries.get(key, (threading.Lock(), 0))
            _entries[key] = lock, users + 1
            locks.append((key, lock))
    try:
        for _, lock in locks:
            while not lock.acquire(blocking=False):
                await asyncio.sleep(.025)
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()
        with _guard:
            for key, lock in locks:
                _, users = _entries[key]
                if users == 1:
                    del _entries[key]
                else:
                    _entries[key] = lock, users - 1
