"""连续会话 CLI；运行摘要引用会话日志，不再次保存整段请求历史。"""
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .agent_cli import print_agent_event, print_agent_text, print_request_record
from .llm.messages import ImageContent, user_message
from .llm.models import ModelConfig, ModelRequest
from .llm.diagnostics import RequestTraceContext
from .storage.paths import LocalPaths
from .storage.request_records import RequestRecordStore
from .llm.providers.http import ProviderError
from .runtime.agent.models import AgentCancelled, AgentLimits, AgentResult
from .runtime.application import Runtime
from .runtime.conversations import (
    CompactionRecord, CompactionSaveError, ConversationSaveError, ConversationService,
    ConversationSettings, ConversationStore,
)
from .runtime.runs import RunEvent, RunState, exception_diagnostics
from .runtime.session import RunTracker, SessionStore


async def run_conversation_cli(
    runtime: Runtime, config: ModelConfig, prompt: str, *, conversation_id: str | None,
    settings: ConversationSettings | None, tool_names: list[str], limit_overrides: dict[str, int],
    conversation_dir: Path, record_dir: Path, stream: bool,
    images: list[ImageContent] | None = None,
    record_requests: bool = False,
) -> AgentResult:
    store = ConversationStore(conversation_dir)
    service = ConversationService(runtime, store, config, tool_names=tool_names)
    if conversation_id is None:
        if settings is None:
            raise ValueError("新建会话需要配置。")
        conversation = service.create(settings)
    else:
        conversation = store.load(conversation_id)
    conversation_id = conversation.conversation_id
    limits = AgentLimits(**{**asdict(conversation.settings.limits), **limit_overrides})
    input_message = user_message(prompt, images)
    run_id = uuid4().hex
    print(f"会话编号：{conversation_id}\n会话文件：{store.path(conversation_id)}", file=sys.stderr)

    def encode_result(result: AgentResult) -> dict:
        return {
            "conversation_id": conversation_id, "run_id": run_id, "text": result.text,
            "stop_reason": result.stop_reason, "model_steps": len(result.steps),
            "tool_attempts": result.tool_attempts, "usage_summary": result.usage_summary,
            "http_attempts": result.http_attempts,
            "error": result.error, "diagnostics": result.diagnostics,
            "compactions": result.compactions,
            "compaction_usage_summary": result.compaction_usage_summary,
            "total_usage_summary": result.total_usage_summary,
            "request_tokens_estimate": result.request_tokens_estimate,
        }

    tracker = RunTracker(SessionStore(record_dir.expanduser().resolve()), encode_result=encode_result)
    tracker.begin_run(
        run_id=run_id, plugin_id="runtime", action="conversation_turn", config=config,
        payload={"conversation_id": conversation_id, "conversation_file": str(store.path(conversation_id))},
        stream=stream,
    )

    def finish(state: RunState, result: AgentResult, error: str | None = None) -> None:
        _, save_error = tracker.accept_event(RunEvent(
            run_id, "runtime", "conversation_turn", state, result=result,
            error=error if error is not None else result.error, diagnostics=result.diagnostics,
        ))
        if save_error is not None:
            print(json.dumps({"run_summary": encode_result(result), "save_error": save_error}, ensure_ascii=False), file=sys.stderr)
            raise OSError("会话执行已停止，但运行摘要未保存；摘要已输出，不能通过重跑会话补存。")

    try:
        saved = conversation.settings
        async with RequestRecordStore(LocalPaths.default().request_records).recording(record_requests,
            RequestTraceContext(run_id, conversation_id), api_style=config.api_style, model=config.model,
            prompt_source={"origin": "conversation_snapshot", "version": saved.prompt_config.version,
                           "source": saved.prompt_config.source, "mode": saved.prompt_mode},
            on_notice=print_request_record) as traces:
            result = await service.send_message(
                conversation_id, prompt, run_id=run_id, limits=limits,
                on_text=print_agent_text if stream else None, on_event=print_agent_event,
                images=images, request_traces=traces,
            )
            if traces is not None:
                traces.set_outcome(result.stop_reason)
    except ConversationSaveError as exc:
        # 只输出本轮新增事实和待存项，不把旧历史再复制进错误输出。
        print(json.dumps({
            "conversation_id": exc.conversation_id, "run_id": exc.run_id,
            "stop_reason": exc.result.stop_reason, "steps": [asdict(step) for step in exc.result.steps],
            "compactions": exc.result.compactions,
            "pending_event": exc.pending, "save_error": str(exc),
            "save_error_type": type(exc.__cause__).__name__,
            "save_errno": getattr(exc.__cause__, "errno", None),
            "save_diagnostics": exception_diagnostics(exc.__cause__ or exc),
        }, ensure_ascii=False), file=sys.stderr)
        failed = exc.result if exc.run_id == run_id else AgentResult(
            ModelRequest([input_message]), stop_reason="failed",
            error="上一轮中断事实未能保存，本次新消息没有进入模型执行。",
        )
        finish(RunState.FAILED, failed, str(exc))
        raise
    except AgentCancelled as exc:
        finish(RunState.CANCELLED, exc.result)
        raise
    except asyncio.CancelledError:
        result = AgentResult(ModelRequest([input_message]), stop_reason="cancelled")
        finish(RunState.CANCELLED, result)
        raise
    except Exception as exc:
        result = AgentResult(
            ModelRequest([input_message]), stop_reason="failed",
            error=str(exc) if isinstance(exc, (OSError, ValueError, ProviderError)) else f"会话入口错误（{type(exc).__name__}）。",
            diagnostics=exception_diagnostics(exc),
        )
        finish(RunState.FAILED, result)
        return result
    finish(RunState.SUCCEEDED if result.stop_reason == "completed" else RunState.FAILED, result)
    return result


async def run_compaction_cli(
    runtime: Runtime, config: ModelConfig, conversation_id: str, *, tool_names: list[str], conversation_dir: Path,
    record_requests: bool = False,
) -> CompactionRecord:
    """摘要结果与用量进入同一会话日志，不创建假用户轮次或 Agent 运行摘要。"""
    store = ConversationStore(conversation_dir)
    service = ConversationService(runtime, store, config, tool_names=tool_names)
    print(f"会话编号：{conversation_id}\n会话文件：{store.path(conversation_id)}", file=sys.stderr)
    try:
        saved = store.load_settings(conversation_id) if record_requests else None
        async with RequestRecordStore(LocalPaths.default().request_records).recording(record_requests,
            RequestTraceContext(uuid4().hex, conversation_id, purpose="compaction"),
            api_style=config.api_style, model=config.model,
            prompt_source={"origin": "conversation_snapshot", "version": saved.prompt_config.version,
                           "source": saved.prompt_config.source, "mode": saved.prompt_mode} if saved else None,
            on_notice=print_request_record) as traces:
            result = await service.compact(conversation_id, on_event=print_agent_event, request_traces=traces)
            if traces is not None:
                traces.set_outcome(result.status)
            return result
    except (CompactionSaveError, ConversationSaveError) as exc:
        print(json.dumps({
            "conversation_id": conversation_id, "pending_event": exc.pending,
            "save_error": str(exc), "save_diagnostics": exception_diagnostics(exc.__cause__ or exc),
        }, ensure_ascii=False), file=sys.stderr)
        raise
