"""父子执行器共用文件锁；只协调应用内操作，不充当文件系统沙箱。"""
import asyncio
import errno
import os
from contextlib import asynccontextmanager
from pathlib import Path


@asynccontextmanager
async def mutation_gate(path: Path | None):
    if path is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    busy_errors = {errno.EACCES} if os.name == "nt" else {errno.EACCES, errno.EAGAIN}
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        while not acquired:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                # 只等待实际锁冲突；无效句柄、参数或锁资源故障须交给调用边界报告。
                if exc.errno not in busy_errors:
                    raise
                await asyncio.sleep(0.05)
        yield
    finally:
        try:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            # 解锁失败仍关闭句柄，不能留下持续占用的锁文件。
            handle.close()
