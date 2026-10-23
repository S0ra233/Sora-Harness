"""直接启动固定rg；消费分隔记录，主动早停与真实失败分开，取消时回收管道。"""
import asyncio
import codecs
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ...contracts import ToolContext, ToolError
from ..limits import MAX_RAW_BYTES, MAX_RECORD_BYTES
from ..search_output import search_stop_reason

@dataclass
class RGStream:
    reader: asyncio.StreamReader
    separator: bytes
    raw_bytes: int = 0
    eof: bool = False
    limit_reason: str | None = None

    async def records(self):
        pending = bytearray()
        while True:
            chunk = await self.reader.read(65536)
            if not chunk:
                self.eof = True
                if pending:
                    raise ToolError("search_output_invalid", "rg输出缺少完整记录结束符。")
                return
            self.raw_bytes += len(chunk)
            if self.raw_bytes > MAX_RAW_BYTES:
                self.limit_reason = "raw_output_limit"
                return
            pending.extend(chunk)
            cursor = 0
            while True:
                index = pending.find(self.separator, cursor)
                if index < 0:
                    break
                if index - cursor > MAX_RECORD_BYTES:
                    self.limit_reason = "record_limit"
                    return
                value = bytes(pending[cursor:index])
                cursor = index + len(self.separator)
                if value:
                    yield value
            del pending[:cursor]
            if len(pending) > MAX_RECORD_BYTES:
                self.limit_reason = "record_limit"
                return


async def _stderr(stream):
    """统计完整流，只保留首尾窗口；不从行中间剪掉诊断却假装它是行首。"""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    head, tail, chars, bytes_count, newlines, last = "", "", 0, 0, 0, ""
    while True:
        chunk = await stream.read(65536)
        text = decoder.decode(chunk, final=not chunk)
        bytes_count += len(chunk)
        chars += len(text)
        newlines += text.count("\n")
        if text:
            last = text[-1]
        head += text[:max(0, 1000 - len(head))]
        tail = (tail + text)[-1000:]
        if not chunk:
            break
    lines = newlines + int(chars > 0 and last != "\n")
    if chars <= 2000:
        preview = head + (tail[-(chars - len(head)):] if chars > len(head) else "")
        return {"text": preview, "bytes": bytes_count, "characters": chars, "lines": lines, "clipped_chars": 0, "truncated": False}
    left = head[:head.rfind("\n") + 1] if "\n" in head else head
    right = tail[tail.find("\n") + 1:] if "\n" in tail else tail
    clipped = chars - len(left) - len(right)
    marker = f"\n…[诊断共{lines}行，省略{clipped}字符；首尾超长行可能为片段]…\n"
    return {"text": left + marker + right, "bytes": bytes_count, "characters": chars, "lines": lines,
            "clipped_chars": clipped, "truncated": True}


async def _drain(stream):
    while await stream.read(65536):
        pass


async def _cleanup(spawn, process, readers, audit=None):
    try:
        child = process if process is not None else await spawn
    except (OSError, ValueError):
        return
    if child.returncode is None:
        try:
            child.kill()
            if audit is not None:
                audit["terminated_by_application"] = True
        except ProcessLookupError:
            pass
    for reader in readers:
        if not reader.done():
            reader.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    tasks = [asyncio.create_task(child.wait()), asyncio.create_task(_drain(child.stdout)), asyncio.create_task(_drain(child.stderr))]
    _, pending = await asyncio.wait(tasks, timeout=5)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if audit is not None:
        audit["raw_exit_code"] = child.returncode
    if pending:
        raise ToolError("search_cleanup_failed", "rg终止后未在清理期限内确认退出。")


async def run_rg(arguments, context: ToolContext, cwd: Path, consumer, *, separator=b"\n"):
    config = context.search_config
    if config is None or config.backend != "rg":
        raise ToolError("search_not_configured", "应用尚未确定rg后端。")
    executable = Path(config.executable) if config.executable is not None else None
    details = {"backend": "rg", "resolved_executable": str(executable) if executable else None, "cwd": str(cwd),
               "exists": executable.is_file() if executable else False,
               "probe_ref": "environment.file_search.probe"}
    if executable is None or not executable.is_file():
        raise ToolError("environment_missing", "固定rg路径不可用；请重新检测环境或新建Python后端会话。", details=details)
    try:
        details["executable_bytes"] = executable.stat().st_size
    except OSError as exc:
        raise ToolError("environment_missing", "固定rg路径无法读取；未自动切换后端。", details={**details,
                        "errno": exc.errno, "winerror": getattr(exc, "winerror", None)}) from exc
    excluded = {name.casefold() for name in context.excluded_env}
    environment = {name: value for name, value in os.environ.items() if name.casefold() not in excluded}
    audit = {"resolved_executable": str(executable), "cwd": str(cwd), "raw_exit_code": None,
             "terminated_by_application": False, "stop_reason": None}
    if context.audit_details is not None:
        context.audit_details["search_process"] = audit
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(str(executable), "--no-config", *arguments,
        cwd=cwd, env=environment, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})))
    process, readers = None, []
    try:
        async with asyncio.timeout(config.timeout_seconds):
            process = await asyncio.shield(spawn)
            stream = RGStream(process.stdout, separator)
            readers = [asyncio.create_task(consumer(stream)), asyncio.create_task(_stderr(process.stderr))]
            result = await asyncio.shield(readers[0])
            killed = False
            if not stream.eof and process.returncode is None:
                try:
                    process.kill()
                    killed = True
                    audit["terminated_by_application"] = True
                except ProcessLookupError:
                    pass
            code, errors = await asyncio.gather(process.wait(), asyncio.shield(readers[1]))
            audit.update(raw_exit_code=code, stderr=errors["text"],
                         stderr_summary={key: value for key, value in errors.items() if key != "text"})
        partial = code == 2 and bool(result.get("matches") or result.get("paths") or result.get("files_with_hits")) and not killed
        if code not in (0, 1) and not killed and not partial:
            raise ToolError("search_failed", "rg执行失败；诊断保留真实退出码。", details={**details,
                "exit_code": code, "stop_reason": "error", "stderr": errors["text"], "stderr_summary": {key: value for key, value in errors.items() if key != "text"}})
        coverage = result["coverage"]
        coverage["raw_output_bytes"] = stream.raw_bytes
        coverage["scan_complete"] = coverage["scan_complete"] and stream.eof
        if stream.limit_reason is not None:
            coverage["limit_reason"] = stream.limit_reason
            result["limit_reason"] = stream.limit_reason
        result["truncated"] = result["truncated"] or not stream.eof
        # 未消费到EOF的主动早停不能使用Windows kill返回的1表示“完整搜索无命中”。
        result["exit_code"] = code if stream.eof else None
        if partial:
            result["status"] = "partial"
            result["truncated"] = True
            coverage["scan_complete"] = False
            if coverage["limit_reason"] is None:
                coverage["limit_reason"] = result["limit_reason"] = "search_io_errors"
            result["warnings"] = [{"code": "search_partial", "message": "保留已校验的命中；部分路径读取失败，不能证明全树无其他命中。",
                "details": {"exit_code": code, "stderr": errors["text"],
                            "stderr_summary": {key: value for key, value in errors.items() if key != "text"}}}]
        reason = coverage["limit_reason"]
        result["stop_reason"] = (search_stop_reason(reason, io_errors=partial) if reason is not None or partial or stream.eof
                                 else "killed" if killed else "consumer_stopped")
        audit["stop_reason"] = result["stop_reason"]
        return result
    except ToolError:
        # ToolError继承ValueError，必须先透传，输出超限／退出失败不能误报为启动失败。
        if audit["stop_reason"] is None:
            audit["stop_reason"] = "error"
        raise
    except TimeoutError as exc:
        audit["stop_reason"] = "timeout"
        raise ToolError("search_timeout", "rg超过搜索时限，未返回不完整结果。", details={**details, "stop_reason": "timeout"}) from exc
    except asyncio.CancelledError:
        audit["stop_reason"] = "cancelled"
        if context.execution_details is not None:
            context.execution_details["stop_reason"] = "cancelled"
        raise
    except (OSError, ValueError) as exc:
        audit["stop_reason"] = "launch_failed"
        raise ToolError("search_launch_failed", "rg启动或管道读取失败；固定后端未切换。", details={**details,
            "stop_reason": "launch_failed", "errno": getattr(exc, "errno", None), "winerror": getattr(exc, "winerror", None), "reason": str(exc)[:1000]}) from exc
    finally:
        failure = sys.exception()
        cleanup = asyncio.create_task(_cleanup(spawn, process, readers, audit))
        cancellation = None
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                if cleanup.cancelled():
                    raise
                cancellation = exc
            except (ToolError, OSError) as exc:
                audit["cleanup_error"] = str(exc)
                if failure is not None:
                    failure.add_note(f"搜索回收未确认：{exc}")
                elif cancellation is not None:
                    cancellation.add_note(f"搜索回收未确认：{exc}")
                else:
                    raise
                break
        if process is not None:
            audit["raw_exit_code"] = process.returncode
        if cancellation is not None:
            audit["stop_reason"] = "cancelled"
            raise cancellation
