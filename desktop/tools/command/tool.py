"""执行应用已授权的 Shell 命令，保留完整输出并返回有界结果。"""
import asyncio
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO

from ...llm.messages import ToolSchema
from ..artifacts import ToolArtifacts
from ..contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError
from .processes import ProcessTree
from .shells import command_argv
from .output import CommandOutput, MAX_OUTPUT_CHARS


def create_command_tool() -> ToolDefinition:
    return ToolDefinition(
        ToolSchema(
            name="run_command",
            description=(
                ("在当前会话绑定的本地 Shell（PowerShell 或 Git Bash，非交互模式、无用户 Profile、已关闭 Git 分页器）中执行命令，并由系统进程树（Job Object）统一管理生命周期与超时"
            "回收。使用时请注意：\n1. **无跨调用状态**：每次 `run_command` 都从工作区根目录启动新进程，上一次命令中的 `cd` 或临时环境变量不会保留到下一次调用；如需在子目录运行，请在同一次命令内组"
            "合执行。\n2. **输出模式（`output_mode`）**：默认为 `compact`（成功时自动折叠连续重复的非空行，并保留有界首尾窗口，大幅节省上下文占用）；如需查看未折叠的末尾原始输出可设为 `raw"
            "`。无论哪种模式，完整原始字节均已写入收据日志，需要排查被省略的中间输出时，用返回的 `record_id` 调用 `read_tool_record` 续读。\n3. **安全护栏与副作用核对**：严禁用命令执"
            "行删除（如 `rm`、`del`、`Remove-Item`）、清空已有文件或绕过工作区路径限制。命令若超时、失败或被中断，先根据退出码、已返回日志和实际文件状态核实副作用，不要假定環境已回滚而盲目重跑。此工具"
            "不构成文件访问沙箱，系统命令可能影响工作区之外的文件；实际时限以environment.command_shell为准，只能通过timeout_seconds缩短。长脚本先写文件再执行。")
            ),
            parameters={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "command": {"type": "string", "description": "所选 Shell 的命令文本"},
                    "timeout_seconds": {"type": "number", "description": "正数；可缩短到应用规定上限以内"},
                    "output_mode": {"type": "string", "enum": ["compact", "raw"], "default": "compact"},
                }, "required": ["command"],
            },
        ), execute=run_command, timeout_seconds=None,
        display=ToolDisplay("command", "terminal", "运行", "运行了{n}条命令", "command", "Shell", ("command",)),
    )


async def _cleanup_process(process: asyncio.subprocess.Process, tree: ProcessTree, reader: asyncio.Task | None) -> None:
    """先结束树，再收齐已进入管道的数据；清理等待有限，避免取消后永久挂起。"""
    tree_error = None
    try:
        tree.close()
    except OSError as exc:
        tree_error = exc
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if process.stdin is not None:
            process.stdin.close()
    pending = [asyncio.create_task(process.wait())]
    if reader is not None:
        pending.append(reader)
    _, remaining = await asyncio.wait(pending, timeout=5)
    for task in remaining:
        task.cancel()
    # 消费清理任务异常，主路径保留原始失败；管道读取错误须由正常等待报告。
    await asyncio.gather(*pending, return_exceptions=True)
    if remaining:
        raise OSError("命令已请求终止，但进程或输出管道未在清理期限内结束。")
    if tree_error is not None:
        raise tree_error


async def _execute_command(argv: list[str], context: ToolContext, timeout: float, output: CommandOutput, target: BinaryIO) -> int:
    """启动采用 shield；取消即使落在创建进程期间也能取到进程并回收。"""
    tree = ProcessTree()
    excluded = {name.casefold() for name in context.excluded_env}
    environment = {name: value for name, value in os.environ.items() if name.casefold() not in excluded}
    environment.update(GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0", PAGER="cat", NO_COLOR="1", TERM="dumb")
    if context.shell.kind == "git_bash" and os.name == "nt":
        executable = Path(context.shell.executable)
        root = executable.parent.parent.parent if executable.parent.parent.name == "usr" else executable.parent.parent
        # 不读取启动配置，也能找到 Git 自带的命令；继承的系统 PATH 仍保留。
        environment["PATH"] = os.pathsep.join(str(root / folder) for folder in ("mingw64/bin", "usr/bin", "bin")) + os.pathsep + environment.get("PATH", "")
        environment["MSYSTEM"] = "MINGW64"
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, cwd=context.workspace, env=environment,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}),
    ))
    process, reader = None, None
    try:
        async with asyncio.timeout(timeout):
            process = await asyncio.shield(spawn)
            tree.attach(process.pid)
            reader = asyncio.create_task(output.read(process.stdout, target))
            process.stdin.write(b"start\n")
            await process.stdin.drain()
            process.stdin.close()
            await asyncio.gather(process.wait(), asyncio.shield(reader))
            return process.returncode
    finally:
        # 使用独立清理任务，外层取消不会立即打断结束子进程的动作。
        failure = sys.exception()
        async def cleanup() -> None:
            try:
                child = process if process is not None else await spawn
            except (OSError, ValueError):
                tree.close()
                return
            await _cleanup_process(child, tree, reader)
        cleanup_task = asyncio.create_task(cleanup())
        cancellation = None
        while True:
            try:
                await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError as exc:
                if cleanup_task.cancelled():
                    raise
                cancellation = exc
            except OSError as exc:
                if isinstance(failure, asyncio.CancelledError):
                    failure.add_note(f"进程回收未确认：{exc}")
                elif cancellation is not None:
                    cancellation.add_note(f"进程回收未确认：{exc}")
                else:
                    raise
                break
        if cancellation is not None:
            raise cancellation


async def run_command(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """只联网模型可重试；命令无论退出码、超时或取消都只执行一次。"""
    if context.shell is None:
        raise ToolError("command_not_configured", "应用尚未配置命令执行器。")
    if context.shell.executable is None or not Path(context.shell.executable).is_file():
        raise ToolError("environment_missing", f"所选 {context.shell.kind} 程序不可用；请检查环境或让用户安装后重新检测。",
                        details={"program": context.shell.kind, "executable": context.shell.executable})
    command = arguments.get("command")
    timeout = arguments.get("timeout_seconds", context.shell.timeout_seconds)
    mode = arguments.get("output_mode", "compact")
    if arguments.keys() - {"command", "timeout_seconds", "output_mode"} or not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ToolError("invalid_arguments", "command必须是非空文本，仅支持command、timeout_seconds和output_mode。")
    if not isinstance(mode, str) or mode not in {"compact", "raw"}:
        raise ToolError("invalid_arguments", "output_mode须为compact或raw。")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= context.shell.timeout_seconds:
        raise ToolError("invalid_arguments", f"timeout_seconds 须大于 0，且不超过应用上限 {context.shell.timeout_seconds}。")
    argv = command_argv(context.shell, command)
    if os.name == "nt" and len(subprocess.list2cmdline(argv)) >= 32760:
        raise ToolError("command_too_large", "命令超过 Windows 启动长度上限；请先编辑脚本文件，再调用脚本。")
    artifacts = ToolArtifacts(context, "run_command", command=command, shell=context.shell.kind, timeout_seconds=timeout)
    output_path = artifacts.directory / "output.log"
    output, started, exit_code, status = CommandOutput(collect_compact=mode == "compact"), time.monotonic(), None, "failed"
    try:
        artifacts.update("running", full_output_path=str(output_path))
        with output_path.open("xb") as target:
            try:
                exit_code = await _execute_command(argv, context, timeout, output, target)
                status = "succeeded" if exit_code == 0 else "failed"
            except TimeoutError:
                status = "timed_out"
            except asyncio.CancelledError:
                status = "cancelled"
                raise
    finally:
        failure = sys.exception()
        try:
            artifacts.update(status, exit_code=exit_code, duration_seconds=time.monotonic() - started,
                             output_bytes=output.bytes, error_type=type(failure).__name__ if failure is not None else None,
                             notes=getattr(failure, "__notes__", None))
        except OSError as exc:
            if isinstance(failure, asyncio.CancelledError):
                failure.add_note(f"命令已取消，但记录更新失败：{artifacts.receipt}")
            else:
                raise ToolError("command_record_error", "命令已停止，但终态记录保存失败；请核对输出，不能直接重跑。",
                                details={"command_status": status, **artifacts.references()}) from exc
    result = {
        "shell": context.shell.kind, "status": status, "exit_code": exit_code,
        **output.preview(compact=mode == "compact" and status == "succeeded"),
        "output_encoding": "utf-8 (invalid bytes replaced)", "full_output_path": str(output_path),
        "duration_seconds": time.monotonic() - started, **artifacts.references(),
    }
    if status != "succeeded":
        raise ToolError("command_timeout" if status == "timed_out" else "command_failed",
                        "命令超时或退出失败；查看输出和记录，已有副作用不会自动撤销或重跑。", details=result)
    return result
