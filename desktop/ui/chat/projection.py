"""把持久事实与本轮投影转成显示行；不改变有效历史或运行权限。"""
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime

from ...llm.messages import ImageContent, ToolResult
from ...tools.questions import questions_from_result
from ...llm.reasoning_output import public_reasoning
from ...runtime.agent.models import AgentResult, AgentStep, CompactionStatus
from ...runtime.conversations import Conversation
from ...runtime.conversations.models import ConversationInput
from ...runtime.conversations.revisions import MessageAnchor
from ..conversations.live_view import LiveSnapshot
from .tools.models import ToolView


@dataclass(frozen=True)
class DisplayRow:
    key: str
    label: str
    text: str
    markdown: bool = False
    images: tuple[ImageContent, ...] = ()
    turn_id: str | None = None
    tools: tuple[ToolView, ...] = ()
    activity: str | None = None
    timestamp: str | None = None
    copyable: bool = False
    process: "WorkProcessView | None" = None
    reasoning: bool = False
    reasoning_running: bool = False
    reasoning_kind: str = "reasoning"
    anchor: MessageAnchor | None = None
    editable: bool = False
    page_reference: tuple[str, str] | None = None
    regeneratable: bool = True
    rewindable: bool = True


@dataclass(frozen=True)
class WorkProcessView:
    rows: tuple[DisplayRow, ...]
    running: bool


@dataclass(frozen=True)
class TurnMarker:
    key: str
    user: str
    reply: str = ""


def _stop_text(reason: str) -> str:
    return {
        "tool_limit": "本批工具调用超过本轮剩余额度，整批未执行。",
        "step_limit": "已达到本轮模型步骤上限，后续工具未执行。",
        "cancelled": "本轮已停止。",
        "failed": "本轮运行失败。",
        "model_incomplete": "模型回复未完整结束。",
        "model_unknown": "模型结束状态无法确认。",
    }.get(reason, f"运行已停止（{reason}）。")


def _work_rows(run_id: str, rows: list[DisplayRow], seconds: int | None, *,
               running: bool, final_key: str | None) -> list[DisplayRow]:
    """整轮过程与最后答复分开显示；折叠只作用于投影，不裁剪持久历史。"""
    final = next((row for row in rows if row.key == final_key), None)
    process = tuple(row for row in rows if row.key != final_key)
    if final is not None:
        final = replace(final, label="模型", copyable=True)
    if seconds is None:
        title = "工作过程"
    else:
        hours, remainder = divmod(seconds, 3600)
        minutes, remainder = divmod(remainder, 60)
        elapsed = (f"{hours}小时" if hours else "") + (f"{minutes}分钟" if minutes else "") + f"{remainder}秒"
        title = ("正在处理 · " if running else "") + "用时 " + elapsed
    # 补充气泡始终在抽屉外；输入边界拆分过程，稳定键保留手动收展选择。
    output, segment, key = [], [], f"work:{run_id}"
    for row in process:
        if row.label in {"你", "问题"}:
            if segment:
                output.append(DisplayRow(key, "", "工作过程", process=WorkProcessView(tuple(segment), False)))
            output.append(row)
            segment, key = [], f"work:{run_id}:{row.key}"
        else:
            segment.append(row)
    if segment or not output:
        output.append(DisplayRow(key, "", title, copyable=False, process=WorkProcessView(tuple(segment), running)))
    if final is not None:
        output.append(final)
    return output


def input_row(item: ConversationInput, turn_id: str | None = None) -> DisplayRow:
    return DisplayRow(f"user:{item.input_id}", "你", item.message.text,
                      images=tuple(part for part in item.message.content if isinstance(part, ImageContent)),
                      turn_id=turn_id or item.origin_run_id, timestamp=item.accepted_at or None,
                      anchor=MessageAnchor(turn_id or item.origin_run_id, input_id=item.input_id))


def _attempt_key(run_id: str, step: int, request_id: str | None, attempt: int | None) -> str:
    return f"model:{run_id}:{step}:{request_id or ''}:{attempt or 0}"


def _append_tools(rows: list[DisplayRow], run_id: str, tools: tuple[ToolView, ...]) -> None:
    """相邻工具共用第一条调用的稳定ID，真实正文、错误或用户消息自然分段。"""
    if not tools:
        return
    if rows and rows[-1].tools:
        rows[-1] = replace(rows[-1], tools=rows[-1].tools + tools)
    else:
        rows.append(DisplayRow(f"tool-group:{run_id}:{tools[0].call_id}", "工具", "", tools=tools))
    for tool in tools:
        if tool.result is None or tool.status != "succeeded":
            continue
        for question in questions_from_result(ToolResult(tool.call_id, tool.name, tool.result)):
            options = "\n".join(f"{i + 1}. {option.label}" for i, option in enumerate(question.options))
            text = question.question + ("\n\n" + options if options else "")
            rows.append(DisplayRow(f"question:{run_id}:{tool.call_id}:{question.id}", "问题", text,
                                   copyable=False, rewindable=False, regeneratable=False))


def _extend_rows(rows: list[DisplayRow], additions: list[DisplayRow]) -> None:
    for row in additions:
        if row.tools and rows and rows[-1].tools:
            rows[-1] = replace(rows[-1], tools=rows[-1].tools + row.tools)
        else:
            rows.append(row)


def _step_rows(run_id: str, step: AgentStep) -> list[DisplayRow]:
    rows = []
    attempts = {}
    for event in step.attempts:
        attempts[event.request_id, event.attempt] = event
    partials = set()
    for event in attempts.values():
        if event.phase in {"failed", "cancelled"}:
            rows.extend(_reasoning_rows(_attempt_key(run_id, step.step, event.request_id, event.attempt),
                                        event.reasoning_blocks, event.reasoning_active, False))
            text = event.partial_text or event.error or "未收到完整回复。"
            partials.add(event.partial_text)
            rows.append(DisplayRow(_attempt_key(run_id, step.step, event.request_id, event.attempt),
                                   f"模型 · 第 {event.attempt} 次 · {'中断' if event.partial_text else '请求失败'}", text))
    last = next(reversed(attempts.values())) if attempts else None
    blocks = tuple(step.reasoning)
    active = step.reasoning_active
    replay = step.assistant.replay_state if step.assistant is not None else None
    if replay is not None and (not blocks or replay.api_style == "responses"):
        restored, evidence = public_reasoning(replay)
        if restored:
            blocks = restored
        active = evidence if evidence is not None else active
    if active is None and step.token_usage is not None and step.token_usage.reasoning_tokens is not None:
        active = step.token_usage.reasoning_tokens > 0
    if last is None or last.phase not in {"failed", "cancelled"}:
        rows.extend(_reasoning_rows(_attempt_key(run_id, step.step, last.request_id if last else None,
                                                last.attempt if last else None), blocks, active, False))
    if step.assistant is not None:
        if step.assistant.text:
            rows.append(DisplayRow(_attempt_key(run_id, step.step, last.request_id if last else None,
                                               last.attempt if last else None),
                                   "模型", step.assistant.text, True))
        tools = tuple(ToolView(
            call.call_id, call.name, call.arguments,
            "interrupted" if execution.status == "running" else execution.status,
            execution.result.content if execution.result is not None else None,
        ) for call, execution in zip(step.assistant.tool_calls, step.tools))
        _append_tools(rows, run_id, tools)
    elif step.partial_text and step.partial_text not in partials:
        rows.append(DisplayRow(f"partial:{run_id}:{step.step}", "模型 · 未完成", step.partial_text))
    return rows


def _reasoning_rows(key, blocks, active, running, running_blocks=None):
    if blocks:
        return [DisplayRow(f"reasoning:{key}:{block.block_id}", "", block.text,
                           reasoning=True, reasoning_running=running and (not running_blocks or block.block_id in running_blocks),
                           reasoning_kind=block.kind) for block in blocks]
    return [DisplayRow(f"reasoning:{key}:hidden", "", "", reasoning=True, reasoning_running=running)] if active is True else []


def _compaction_row(status: CompactionStatus, *, running: bool) -> DisplayRow:
    phase = status.status
    if phase == "running":
        text = "正在压缩上下文 · 可能需要几分钟" if running else "上下文压缩已停止，结果尚未确认保存"
        if not running:
            phase = "interrupted"
    elif phase == "succeeded":
        text = "上下文已自动压缩" if status.trigger == "budget" else "上下文已压缩"
    elif phase in {"cancelled", "interrupted"}:
        text = "上下文压缩已停止"
    else:
        text = "上下文压缩失败"
    if status.error:
        text += " · " + status.error
    return DisplayRow(f"compaction:{status.compaction_id}", "", text, activity=phase)


def restrict_revision_rows(rows, boundary):
    """成功压缩后只关闭改写原会话的操作，用户锚点仍用于分支。"""
    if boundary is None:
        return tuple(rows)
    output = []
    for row in rows:
        if row.process is not None:
            contents = restrict_revision_rows(row.process.rows, boundary)
            if contents != row.process.rows:
                row = replace(row, process=replace(row.process, rows=contents))
        if row.rewindable or row.editable or row.regeneratable:
            row = replace(row, rewindable=False, editable=False, regeneratable=False)
        output.append(row)
    return tuple(output)


def history_rows(conversation: Conversation, *, include_pending: bool = True, boundary=None) -> tuple[tuple[DisplayRow, ...], tuple[TurnMarker, ...]]:
    rows, markers = [], []
    boundary = boundary or conversation.latest_compaction
    # 仅投影创建时的前置示例，不伪造真正轮次或提供回退／重新执行操作。
    for index, message in enumerate(conversation.settings.prompt_config.prelude):
        assistant = message["role"] == "assistant"
        rows.append(DisplayRow(f"prelude:{index}", "模型" if assistant else "你", message["content"], markdown=assistant))
    by_id = {item.input_id: item for item in conversation.inputs}
    origins = {}
    owned = defaultdict(lambda: defaultdict(list))
    manual = iter(record for record in conversation.compactions if record.owner_run_id is None)
    pending = next(manual, None)
    for record in conversation.compactions:
        if record.owner_run_id is not None:
            owned[record.owner_run_id][record.after_step].append(record.display_status())
    for turn in conversation.turns:
        source = by_id.get(turn.source_input_id)
        timestamp = source.accepted_at if source is not None else turn.started_at
        origin = (turn.run_id, timestamp) if turn.user is not None else origins.get(turn.resumed_from_run_id)
        origins[turn.run_id] = origin
        while pending is not None and pending.started_at < turn.started_at:
            rows.append(_compaction_row(pending.display_status(), running=False))
            pending = next(manual, None)
        key = f"user:{turn.source_input_id or turn.run_id}"
        if turn.user is not None:
            images = tuple(part for part in turn.user.content if isinstance(part, ImageContent))
            rows.append(DisplayRow(key, "你", turn.user.text, images=images, turn_id=turn.run_id, timestamp=timestamp,
                                   anchor=MessageAnchor(turn.run_id)))
        seconds = None
        if turn.finished_at and turn.stop_reason != "rewound":
            try:
                seconds = max(0, int((datetime.fromisoformat(turn.finished_at) - datetime.fromisoformat(turn.started_at)).total_seconds()))
            except (TypeError, ValueError):
                seconds = None
        statuses = owned.get(turn.run_id, {})
        process = [_compaction_row(status, running=False) for status in statuses.get(0, ())]
        for step in turn.steps:
            if step.assistant is not None or any(event.phase == "started" for event in step.attempts):
                for identifier in step.input_ids:
                    process.append(input_row(by_id[identifier], turn.run_id))
            _extend_rows(process, _step_rows(turn.run_id, step))
            process.extend(_compaction_row(status, running=False) for status in statuses.get(step.step, ()))
        final_key = next((row.key for row in reversed(process) if row.label == "模型" and row.markdown), None) if (
            turn.stop_reason == "completed" and turn.steps and turn.steps[-1].completion_status == "completed"
            and turn.steps[-1].assistant is not None and turn.steps[-1].assistant.text) else None
        if final_key:
            regeneratable = origin is not None and boundary is None
            process = [replace(row, anchor=MessageAnchor(turn.run_id, step=turn.steps[-1].step), editable=True,
                               timestamp=turn.finished_at, regeneratable=regeneratable) if row.key == final_key else row for row in process]
        process.extend(input_row(by_id[identifier], turn.run_id) for identifier in turn.retained_input_ids)
        rows.extend(_work_rows(turn.run_id, process, seconds, running=False, final_key=final_key))
        reply = next((row.text for row in reversed(process) if row.key.startswith(("model:", "partial:"))), "")
        if turn.stop_reason not in {"completed", "rewound"}:
            state = "上次运行未完整结束" if turn.finished_at is None else _stop_text(turn.stop_reason)
            rows.append(DisplayRow(f"notice:{turn.run_id}", "运行状态", turn.error or state, copyable=False))
        if turn.user is not None:
            markers.append(TurnMarker(key, turn.user.text[:240] or "图片消息", reply[:240]))
        markers.extend(TurnMarker(row.key, row.text[:240] or "图片消息") for row in process if row.label == "你")
    while pending is not None:
        rows.append(_compaction_row(pending.display_status(), running=False))
        pending = next(manual, None)
    if include_pending:
        rows.extend(input_row(item) for item in conversation.pending_inputs())
    return restrict_revision_rows(rows, boundary), tuple(markers)


def live_rows(live: LiveSnapshot | None, result: AgentResult | None = None, *,
              running: bool = True) -> tuple[tuple[DisplayRow, ...], tuple[TurnMarker, ...]]:
    if live is None:
        return (), ()
    run_id, rows, markers = live.operation_id, [], []
    if live.kind == "send" and (live.user_text or live.user_images):
        key = f"user:{live.source_input_id or run_id}"
        source = next((item for item in live.inputs if item.input_id == live.source_input_id), None)
        timestamp = source.accepted_at if source is not None else live.started_at
        rows.append(DisplayRow(key, "你", live.user_text, images=live.user_images, turn_id=run_id, timestamp=timestamp,
                               anchor=MessageAnchor(run_id)))
        reply = result.text if result is not None else next((item.text for item in reversed(live.attempts) if item.channel == "model" and item.text), "")
        markers.append(TurnMarker(key, live.user_text[:240] or "图片消息", reply[:240]))
    compactions = defaultdict(list)
    for status in live.compactions:
        compactions[status.after_step].append(_compaction_row(status, running=running))
    process = list(compactions[0])
    inputs = {item.input_id: item for item in live.inputs}
    step_inputs = dict(live.step_inputs)
    if result is not None:
        for step in result.steps:
            process.extend(input_row(inputs[identifier], run_id) for identifier in step.input_ids if identifier in inputs)
            _extend_rows(process, _step_rows(run_id, step))
            process.extend(compactions[step.step])
        final_key = next((row.key for row in reversed(process) if row.label == "模型" and row.markdown), None) if (
            result.stop_reason == "completed" and result.steps and result.steps[-1].completion_status == "completed"
            and result.steps[-1].assistant is not None and result.steps[-1].assistant.text) else None
        if live.kind in {"send", "resume", "program"}:
            rows.extend(_work_rows(run_id, process, live.elapsed_seconds, running=False, final_key=final_key))
        else:
            rows.extend(process)
        if result.stop_reason != "completed":
            rows.append(DisplayRow(f"notice:{run_id}", "运行状态", result.error or _stop_text(result.stop_reason), copyable=False))
        return tuple(rows), tuple(markers)
    # 先分桶，避免每个步骤再次遍历全部请求和工具；不改变保存下来的顺序。
    attempts_by_step, tools_by_step = defaultdict(list), defaultdict(list)
    for attempt in live.attempts:
        if attempt.channel == "model":
            attempts_by_step[attempt.step].append(attempt)
    for tool in live.tools:
        tools_by_step[tool.step].append(tool)
    steps = sorted((attempts_by_step.keys() | tools_by_step.keys() | compactions.keys() | step_inputs.keys()) - {0})
    final_key = None
    for step in steps:
        process.extend(input_row(inputs[identifier], run_id) for identifier in step_inputs.get(step, ()) if identifier in inputs)
        for attempt in attempts_by_step[step]:
            key = _attempt_key(run_id, step, attempt.request_id, attempt.attempt)
            process.extend(_reasoning_rows(key, attempt.reasoning, attempt.reasoning_active,
                                          running and attempt.reasoning_running and attempt.phase not in {"failed", "cancelled"},
                                          attempt.reasoning_running_blocks))
            suffix = " · 回复中断" if attempt.phase in {"failed", "cancelled"} else ""
            label = "模型" if (attempt.attempt or 1) == 1 and not suffix else f"模型 · 第 {attempt.attempt or 1} 次{suffix}"
            if attempt.completion_status == "requires_tool" and not attempt.text and not attempt.error:
                # 纯工具请求没有助手正文，不能用占位文字把连续工具拆散。
                continue
            if attempt.completion_status == "requires_tool":
                fallback = "模型已请求工具，等待执行结果。"
            elif attempt.phase in {"failed", "cancelled"}:
                fallback = "请求未完成。"
            else:
                # 空请求状态由独立工作标记展示，不生成一条重复的助手占位消息。
                if not attempt.text and not attempt.error:
                    continue
                fallback = ""
            text = attempt.text or attempt.error or fallback
            key = _attempt_key(run_id, step, attempt.request_id, attempt.attempt)
            process.append(DisplayRow(key, label, text,
                                   attempt.completion_status in {"completed", "requires_tool"}))
            if step == steps[-1] and attempt.completion_status == "completed" and attempt.phase == "received":
                final_key = key
        tools = tuple(ToolView(
            tool.call.call_id, tool.call.name, tool.call.arguments,
            "interrupted" if tool.phase == "running" and not running else tool.phase,
            tool.result.content if tool.result is not None else None,
        ) for tool in tools_by_step[step])
        _append_tools(process, run_id, tools)
        process.extend(compactions[step])
    if live.kind in {"send", "resume", "program"}:
        rows.extend(_work_rows(run_id, process, live.elapsed_seconds, running=running, final_key=final_key))
    else:
        rows.extend(process)
    return tuple(rows), tuple(markers)
