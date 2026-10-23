"""子进程只在启动屏障后运行；stdin控制，stdout只输出结构化状态。"""
import asyncio
import json
import sys
import threading
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path

from ...application.conversations import ConversationApplication, ConversationCancelled
from ...runtime.conversations import ConversationSettings, ConversationStore, TurnOptions
from ...runtime.conversations.service import ConversationSaveError
from ...runtime.conversations.control import ConversationControl
from ...runtime.conversations.models import ConversationInput
from ...llm.messages import user_message


async def run(start):
    record, directory = start["record"], Path(start["directory"])
    child_id, run_id = record["child_id"], record["run_id"]
    output_lock = threading.Lock()
    def emit(kind, **data):
        with output_lock:
            sys.__stdout__.write(json.dumps({"type": kind, "child_id": child_id, **data}, ensure_ascii=False) + "\n")
            sys.__stdout__.flush()
    from ...mcp.settings import McpSettings, restore_delegated
    from ...storage.paths import LocalPaths
    app = ConversationApplication(directory, enable_subagents=False,
        reference_scope_id=record["parent_id"],
        mutation_lock=Path(record["mutation_lock"]), delegated_records=record.get("record_ids", ()),
        delegated_calls=record.get("record_calls", ()), excluded_env=record.get("excluded_env", ()),
        enabled_plugins=frozenset(record.get("enabled_plugins", ())), skill_roots=record.get("skill_roots", ()),
        skill_manual_invocation=False, plugin_snapshot=record.get("plugin_snapshot"),
        mcp_snapshot=restore_delegated(McpSettings(LocalPaths(directory).mcp), record.get("mcp_snapshot", ()))
                     if record.get("mcp_snapshot") else ())
    app.store = ConversationStore(directory / "subagents" / "conversations")
    settings = ConversationSettings.from_dict(record["settings"])
    control = ConversationControl(child_id, run_id)
    control.on_notice = lambda notice: emit("input", input_id=notice.item.input_id,
                                             accepted=notice.accepted, error=notice.error)
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    def commands():
        for line in sys.stdin:
            value = None
            try:
                value = json.loads(line)
                if value["type"] == "cancel":
                    loop.call_soon_threadsafe(task.cancel)
                elif value["type"] == "message":
                    item = ConversationInput(value["input_id"], "steer", user_message(value["text"]), run_id)
                    if not control.submit(item):
                        emit("input", input_id=item.input_id, accepted=False, error="子运行已结束。")
            except (ValueError, KeyError, TypeError) as exc:
                emit("input", input_id=value.get("input_id") if isinstance(value, dict) else None,
                     accepted=False, error=f"控制参数不正确（{type(exc).__name__}）。")
        loop.call_soon_threadsafe(task.cancel)
    threading.Thread(target=commands, daemon=True).start()
    try:
        if not app.store.path(child_id).exists():
            app.store.create(settings, conversation_id=child_id)
        conversation = app.store.load(child_id)
        options = TurnOptions(settings.reasoning_effort, settings.model_target, settings.prompt_config)
        arguments = dict(tool_names=settings.tool_names, run_id=run_id, options=options, control=control,
                         on_event=lambda event: emit("progress", event=asdict(event)))
        if conversation.resumable_turn is not None:
            update = await app.resume_message(child_id, conversation.resumable_turn.run_id, start["message"], **arguments)
        else:
            update = await app.send_message(child_id, start["message"], **arguments)
        result = update.result
        emit("finished", status="completed" if result.stop_reason == "completed" else "failed",
             text=result.text, usage=result.total_usage_summary, error=result.error or (None if result.stop_reason == "completed" else result.stop_reason))
    except ConversationCancelled as exc:
        result = exc.update.result
        emit("finished", status="interrupted", text=result.text if result else "", usage=result.total_usage_summary if result else None,
             error="子运行已停止，检查点保留。")
    except ConversationSaveError as exc:
        emit("finished", status="save_failed", text=exc.result.text, usage=exc.result.total_usage_summary,
             error="子运行检查点保存未确认；仅重试保存，不重新请求模型。",
             pending=exc.pending, pending_result=asdict(exc.result))
    except Exception as exc:
        emit("finished", status="save_failed" if isinstance(exc, OSError) else "failed", text="",
             error=f"子运行入口失败（{type(exc).__name__}）：{exc}")


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    start = json.loads(sys.stdin.readline())
    if start.get("type") != "start":
        raise ValueError("缺少启动屏障。")
    # 插件普通打印进入日志，避免污染结构化状态通道。
    with redirect_stdout(sys.stderr):
        asyncio.run(run(start))


if __name__ == "__main__":
    main()
