"""Agent 命令行与本地记录的衔接；模型配置参数仍由公共 CLI 读取。"""
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from .llm.models import ModelConfig, ModelRequest
from .llm.diagnostics import RequestRecordNotice, RequestTraceContext
from .storage.paths import LocalPaths
from .storage.request_records import RequestRecordStore
from .llm.providers.http import ProviderError
from .prompts import PromptConfig, load_prompt_config
from .runtime.agent.models import AgentCancelled, AgentEvent, AgentLimits, AgentResult
from .runtime.application import Runtime
from .runtime.runs import RunEvent, RunState, exception_diagnostics
from .runtime.session import RunTracker, SessionStore
from .tools.command.shells import ShellConfig, resolve_shell
from .tools.filesystem.search_config import SEARCH_TOOLS, SearchConfig, resolve_search_config


def print_agent_event(event: AgentEvent) -> None:
    """单轮和连续会话共用控制台通知；流式文本由独立回调输出。"""
    if event.kind != "text_delta":
        if event.attempt is not None and event.attempt.phase == "started":
            print(f"\n\n[回复尝试 {event.attempt.attempt}]\n", end="", flush=True)
        print(json.dumps(asdict(event), ensure_ascii=False), file=sys.stderr)


def print_agent_text(text: str) -> None:
    print(text, end="", flush=True)


def print_request_record(notice: RequestRecordNotice) -> None:
    """诊断通知与Agent状态分开，终端也不回显请求正文或鉴权信息。"""
    print(json.dumps({"request_record": asdict(notice)}, ensure_ascii=False), file=sys.stderr)


async def run_agent_cli(
    runtime: Runtime, config: ModelConfig, request: ModelRequest, *, workspace: Path | None,
    tool_names: list[str], limits: AgentLimits, record_dir: Path, stream: bool,
    prompt_config: PromptConfig | None = None,
    prompt_mode: str = "work",
    shell_config: ShellConfig | None = None,
    search_config: SearchConfig | None = None,
    record_requests: bool = False,
) -> AgentResult:
    """先保存起点，再运行；取消和失败也保存终态，不自动重新执行。"""
    workspace = workspace.expanduser().resolve() if workspace is not None else None
    record_dir = record_dir.expanduser().resolve()
    prompt_config = prompt_config if prompt_config is not None else load_prompt_config()
    if "run_command" in tool_names:
        shell_config = resolve_shell(shell_config if shell_config is not None else ShellConfig())
    if SEARCH_TOOLS.intersection(tool_names):
        search_config = resolve_search_config(search_config if search_config is not None else SearchConfig())
    tracker = RunTracker(SessionStore(record_dir), encode_result=_encode_result)
    run_id = uuid4().hex
    tracker.begin_run(
        run_id=run_id, plugin_id="runtime", action="run_agent", config=config,
        payload={
            "workspace": str(workspace) if workspace is not None else None, "tool_names": tool_names,
            "prompt_mode": prompt_mode,
            "limits": asdict(limits), "input": asdict(request),
            "prompt_config": asdict(prompt_config),
            "shell_config": asdict(shell_config) if shell_config is not None else None,
            "search_config": asdict(search_config) if search_config is not None else None,
        },
        stream=stream,
    )
    # runtime 是记录的来源标识，不会因此创建或调用名为 runtime 的业务插件。
    print(f"Agent 运行记录：{record_dir / (run_id + '.json')}", file=sys.stderr)

    try:
        async with RequestRecordStore(LocalPaths.default().request_records).recording(record_requests,
            RequestTraceContext(run_id), api_style=config.api_style, model=config.model,
            prompt_source={"origin": "cli_agent", "version": prompt_config.version,
                           "source": prompt_config.source, "mode": prompt_mode}, on_notice=print_request_record) as traces:
            result = await runtime.run_agent(
                request, workspace=workspace, tool_names=tool_names, limits=limits,
                on_text=print_agent_text if stream else None, on_event=print_agent_event,
                prompt_config=prompt_config, prompt_mode=prompt_mode,
                shell_config=shell_config, search_config=search_config, request_traces=traces,
            )
            if traces is not None:
                traces.set_outcome(result.stop_reason)
    except AgentCancelled as exc:
        _finish_record(tracker, run_id, RunState.CANCELLED, exc.result)
        raise
    except asyncio.CancelledError:
        result = AgentResult(request, stop_reason="cancelled")
        _finish_record(tracker, run_id, RunState.CANCELLED, result)
        raise
    except Exception as exc:
        # 配置、目录或连接作用域尚未进入循环时，也要结束已创建的记录。
        result = AgentResult(
            request, stop_reason="failed",
            error=str(exc) if isinstance(exc, (ProviderError, OSError, ValueError)) else f"Agent 入口发生内部错误（{type(exc).__name__}）。",
            diagnostics=exception_diagnostics(exc),
        )
        _finish_record(tracker, run_id, RunState.FAILED, result)
        return result
    state = RunState.SUCCEEDED if result.stop_reason == "completed" else RunState.FAILED
    _finish_record(tracker, run_id, state, result)
    return result


def _encode_result(result: AgentResult) -> dict[str, Any]:
    """终态记录同时保留原始步骤和统一汇总，旧历史无需按新字段反序列化。"""
    return {**asdict(result), "usage_summary": result.usage_summary, "http_attempts": result.http_attempts}


def _finish_record(tracker: RunTracker, run_id: str, state: RunState, result: AgentResult) -> None:
    """保存失败仅输出已有事实，不通过重跑模型或工具来生成记录。"""
    _, error = tracker.accept_event(RunEvent(
        run_id, "runtime", "run_agent", state, result=result,
        error=result.error, diagnostics=result.diagnostics,
    ))
    if error is not None:
        print(json.dumps({"agent_result": _encode_result(result), "save_error": error}, ensure_ascii=False), file=sys.stderr)
        raise OSError("Agent 已停止，但运行记录写入失败；已有终态已输出，请保存终端输出后检查记录目录。")
