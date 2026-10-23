"""读取时的文件事实；stat版本不是全文哈希，变化复核或显式请求才流式计算全文。"""
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone

from .limits import CHUNK_BYTES, MAX_READ_REVALIDATION_SECONDS
from ..contracts import ToolError


def file_version(stat) -> str:
    values = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return hashlib.sha256(json.dumps(values).encode("ascii")).hexdigest()


def file_evidence(path, stat=None) -> dict:
    stat = stat if stat is not None else path.stat()
    return {"resolved_path": str(path), "bytes": stat.st_size,
            "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "file_version": file_version(stat), "version_basis": "stat"}


def stat_details(value) -> dict:
    """保留判据本身；调用者能区分文件标识、尺寸和时间变化，不只能看哈希串。"""
    return {"bytes": value.st_size, "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns,
            "device": value.st_dev, "file_id": value.st_ino, "file_version": file_version(value)}


async def _hash_stream(source) -> str:
    digest = hashlib.sha256()
    before, position = os.fstat(source.fileno()), source.tell()
    try:
        source.seek(0)
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
            await asyncio.sleep(0)
        if file_version(os.fstat(source.fileno())) != file_version(before):
            raise ToolError("file_changed", "计算哈希期间原句柄内容变化。",
                            details={"before": stat_details(before), "after": stat_details(os.fstat(source.fileno()))})
        return digest.hexdigest()
    finally:
        source.seek(position)


async def validate_read_snapshot(source, path, before, path_before):
    """同源前后比较；原句柄稳定且目标有变化时才核对内容，不给每页强制全文哈希。"""
    after = os.fstat(source.fileno())
    details = {"resolved_path": str(path), "handle_before": stat_details(before), "handle_after": stat_details(after),
               "path_before": stat_details(path_before), "path_after": None,
               "before_sha256": None, "after_sha256": None, "sha256_scope": "full_file"}
    try:
        path_after = path.stat()
    except OSError as exc:
        raise ToolError("file_changed", "读取期间目标路径无法重新确认。", details={**details, "errno": exc.errno}) from exc
    details["path_after"] = stat_details(path_after)
    if file_version(before) != file_version(after):
        raise ToolError("file_changed", "原句柄在读取期间变化，不返回可能混读的窗口。", details=details)
    with path.open("rb") as current:
        current_before = os.fstat(current.fileno())
        if file_version(before) == file_version(current_before) and file_version(path_before) == file_version(path_after):
            return path_after, False
        details["current_handle"] = stat_details(current_before)
        try:
            async with asyncio.timeout(MAX_READ_REVALIDATION_SECONDS):
                details["before_sha256"] = await _hash_stream(source)
                details["after_sha256"] = await _hash_stream(current)
        except ToolError as exc:
            raise ToolError(exc.code, str(exc), details={**details, "hash_error": exc.details}) from exc
        except TimeoutError as exc:
            raise ToolError("file_changed", "目标元数据变化，未在内容核对时限内确认一致；请重新读取。",
                            details={**details, "revalidation": "time_limit"}) from exc
        if file_version(os.fstat(source.fileno())) != file_version(before):
            raise ToolError("file_changed", "原句柄在内容核对期间变化，不返回早先窗口。",
                            details={**details, "handle_after_revalidation": stat_details(os.fstat(source.fileno()))})
        path_final = path.stat()
        if file_version(path_final) != file_version(path_after):
            raise ToolError("file_changed", "内容核对期间路径再次变化。",
                            details={**details, "path_after_revalidation": stat_details(path_final)})
        if details["before_sha256"] != details["after_sha256"]:
            raise ToolError("file_changed", "稳定的原句柄与当前目标内容不同；请重新读取。", details=details)
    return path_after, True


async def hash_file(path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        before, path_before = os.fstat(source.fileno()), path.stat()
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
            await asyncio.sleep(0)
        after, path_after = os.fstat(source.fileno()), path.stat()
        if file_version(before) != file_version(after) or file_version(path_before) != file_version(path_after):
            raise ToolError("file_changed", "文件在计算哈希时发生变化，请重新读取。",
                            details={"resolved_path": str(path), "operation": "hash_file",
                                     "handle_before": stat_details(before), "handle_after": stat_details(after),
                                     "path_before": stat_details(path_before), "path_after": stat_details(path_after)})
    return digest.hexdigest()
