"""从有序记录还原事实，再生成模型可续接的消息；读取不会执行工具。"""
from copy import deepcopy
from dataclasses import replace
from typing import Any
from math import isfinite
from uuid import UUID

from ...llm.messages import Message, ModelMessage, ToolResult, message_from_dict, user_message, validate_tool_results, expand_skill_messages
from ...llm.models import ModelConfig, ModelRequest
from ...llm.history_projection import bind_origin, project_messages
from ...llm.attempts import ModelAttemptEvent
from ...llm.usage import TokenUsage
from ...llm.reasoning_output import ReasoningBlock
from ..agent.models import AgentLimits, AgentResult, AgentStep, ToolExecution
from ...tools.registry import error_result
from .models import CompactionRecord, Conversation, ConversationInput, ConversationTurn, TurnOptions


def _execution(data: dict[str, Any]) -> ToolExecution:
    values = dict(data)
    if values.get("result") is not None:
        values["result"] = message_from_dict(values["result"])
        if not isinstance(values["result"], ToolResult):
            raise ValueError("工具执行结果必须是 ToolResult。")
    execution = ToolExecution(**values)
    for duration in (execution.elapsed_seconds, execution.handler_seconds):
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not isfinite(duration) or duration < 0):
            raise ValueError("工具耗时须为非负有限数或null。")
    if not isinstance(execution.handler_started, bool):
        raise ValueError("工具进入状态必须是布尔值。")
    if not isinstance(execution.call_id, str) or not execution.call_id:
        raise ValueError("工具执行记录需要调用 ID。")
    return execution


def _step(data: dict[str, Any]) -> AgentStep:
    values = dict(data)
    values["assistant"] = message_from_dict(data["assistant"]) if data["assistant"] is not None else None
    values["tools"] = [_execution(value) for value in data["tools"]]
    values["token_usage"] = TokenUsage(**data["token_usage"]) if data["token_usage"] is not None else None
    values["attempts"] = [ModelAttemptEvent(**event) for event in data.get("attempts", [])]
    values["context_messages"] = _context_messages(data.get("context_messages", []))
    values["reasoning"] = [ReasoningBlock(**value) for value in data.get("reasoning", [])]
    step = AgentStep(**values)
    _validate_program_refs(step)
    if step.reasoning_active is not None and type(step.reasoning_active) is not bool:
        raise ValueError("模型步骤的思考状态须为布尔值或null。")
    if not isinstance(step.completion_status, str) or not isinstance(step.partial_text, str):
        raise ValueError("模型步骤状态与片段须为文本。")
    if step.assistant is not None:
        if not isinstance(step.assistant, ModelMessage) or step.assistant.role != "assistant":
            raise ValueError("模型检查点必须保存 assistant 消息。")
        if step.completion_status not in ("completed", "requires_tool"):
            raise ValueError("未完成响应不能作为正式 assistant 消息。")
        calls = step.assistant.tool_calls
        if [call.call_id for call in calls] != [item.call_id for item in step.tools]:
            raise ValueError("模型步骤的工具执行记录与调用 ID 不对应。")
        if bool(calls) != (step.completion_status == "requires_tool"):
            raise ValueError("完整模型响应状态与工具调用不一致。")
        for call, item in zip(calls, step.tools):
            if item.result is not None and (item.result.call_id, item.result.name) != (call.call_id, call.name):
                raise ValueError("模型检查点中的工具结果与调用不对应。")
    elif step.tools:
        raise ValueError("没有完整 assistant 消息的步骤不能带工具执行。")
    return step


def _context_messages(values) -> list[ModelMessage]:
    """程序资料独立于用户原文；旧检查点没有此字段时返回空列表。"""
    if not isinstance(values, list):
        raise ValueError("请求上下文检查点须为消息数组。")
    messages = [message_from_dict(value) for value in values]
    if any(not isinstance(message, ModelMessage) or message.role != "user" or not message.runtime_context_id
           or message.runtime_context_kind not in {"environment_update", "request_state", "subagent_notice"} for message in messages):
        raise ValueError("请求上下文必须是程序环境更新或额度资料。")
    return messages


def _validate_program_refs(step: AgentStep) -> None:
    identifiers = step.program_event_ids
    if (not isinstance(identifiers, list) or any(not isinstance(item, str) for item in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError("程序事件引用须为不重复的编号数组。")
    for identifier in identifiers:
        if UUID(identifier).hex != identifier:
            raise ValueError("程序事件引用须为稳定UUID。")
    if identifiers != [message.runtime_context_id for message in step.context_messages
                       if message.runtime_context_kind == "subagent_notice"]:
        raise ValueError("程序事件编号与保存的子结果资料不对应。")


def apply_event(conversation: Conversation, event: dict[str, Any]) -> None:
    """文件中的事件只按顺序还原；未知类型、断序和错误关联明确拒绝。"""
    seq, kind, data = event["seq"], event["type"], event["data"]
    if kind not in (
        "turn_started", "turn_finished", "model_started", "model_finished", "tool_started", "tool_finished",
        "compaction_started", "compaction_finished",
        "model_attempt", "input_submitted",
        "history_revised", "history_seed", "prompt_applied",
    ):
        raise ValueError(f"不支持会话事件类型：{kind}。")
    if type(seq) is not int or seq != conversation.last_seq + 1 or not isinstance(data, dict):
        raise ValueError("会话事件序号不连续或 data 不是对象。")
    run_id, timestamp = event["run_id"], event["time"]
    if UUID(run_id).hex != run_id or not isinstance(timestamp, str):
        raise ValueError("运行编号或时间格式不正确。")
    if kind == "history_seed":
        from .revisions import apply_seed
        apply_seed(conversation, data)
    elif kind in {"history_revised", "prompt_applied"}:
        if conversation.active_turn is not None or conversation.active_compaction is not None:
            raise ValueError("当前运行尚未结算，不能修改有效历史或提示词。")
        if kind == "prompt_applied":
            from ...prompts import PromptConfig
            from .models import validate_preset_identity
            old_prompt = conversation.current_prompt
            identifier, name = data.get("preset_id"), data.get("preset_name")
            validate_preset_identity(identifier, name)
            # 旧版队列没有显式提示词快照，应用新预设前固定其原配置。
            conversation.inputs = [replace(item, options=replace(item.options, prompt_config=old_prompt))
                                   if item.options is not None and item.options.prompt_config is None else item
                                   for item in conversation.inputs]
            conversation.active_prompt = PromptConfig(**data["prompt_config"])
            conversation.active_preset_id, conversation.active_preset_name = identifier, name
            conversation.prompt_applied_at = timestamp
        else:
            from .revisions import MessageAnchor, cut_history
            cut_history(conversation, MessageAnchor(**data["anchor"]), timestamp=timestamp, replacement=data.get("replacement"))
            metadata = {key: value for key, value in (conversation.navigation or {}).items()
                        if key in {"branch_title", "source_conversation_id"}}
            conversation.navigation = {**metadata, **deepcopy(data), "operation_id": run_id, "time": timestamp}
    elif kind == "input_submitted":
        _apply_input(conversation, run_id, data, timestamp)
    elif kind.startswith("compaction_"):
        _apply_compaction(conversation, kind, run_id, data, timestamp)
    elif conversation.active_compaction is not None:
        raise ValueError("摘要尝试尚未结算，不能推进模型或工具事件。")
    elif kind == "turn_started":
        if conversation.active_turn is not None or any(turn.run_id == run_id for turn in conversation.turns):
            raise ValueError("会话已有未结束轮次或重复运行编号。")
        source_id, resumed = data.get("source_input_id"), data.get("resumed_from_run_id")
        user = message_from_dict(data["user"]) if data.get("user") is not None else None
        continuation = data.get("continuation")
        source = None
        if source_id is not None:
            queue = conversation.pending_inputs("follow_up")
            source = queue[0] if queue and queue[0].input_id == source_id else None
            if source is None or user is not None or resumed is not None or conversation.resumable_turn is not None:
                raise ValueError("排队轮次须引用尚未使用的输入，不重复保存正文。")
            user = source.message
        if resumed is not None:
            previous = conversation.resumable_turn
            if previous is None or previous.run_id != resumed or previous.finished_at is None:
                raise ValueError("继续只能关联最后一个已结算的暂停运行。")
            if not isinstance(continuation, str) or not continuation:
                raise ValueError("继续需要独立的程序续接意图。")
        elif continuation is not None:
            raise ValueError("普通轮次不能伪造程序续接。")
        origin = data.get("origin", "user")
        if origin not in {"user", "subagent_notice"} or (origin == "subagent_notice" and (user is not None or source_id or resumed or not data.get("program_event_ids"))):
            raise ValueError("程序轮次来源与完成通知编号不一致。")
        if (user is not None and (not isinstance(user, ModelMessage) or user.role != "user" or user.runtime_context_id)) or (user is None and resumed is None and origin != "subagent_notice"):
            raise ValueError("轮次必须从真实用户输入或明确的程序续接开始。")
        environment = data.get("execution_environment")
        if environment is not None and not isinstance(environment, dict):
            raise ValueError("本轮环境记录须为对象或 null。")
        options = TurnOptions(**data["options"]) if data.get("options") is not None else None
        if options is not None:
            options.validate(conversation.settings)
        if source is not None:
            expected = source.options
            if expected.prompt_config is None and options is not None and options.prompt_config is not None:
                expected = replace(expected, prompt_config=conversation.current_prompt)
            if expected.model_target is None and options is not None and options.model_target is not None:
                # 旧队列固定原header；只允许补齐过去未保存的提供商规则。
                target = conversation.settings.model_target
                if target.config.provider_rules is None:
                    target = replace(target, config=replace(target.config, provider_rules=options.model_target.config.provider_rules))
                expected = replace(expected, model_target=target)
            if options != expected:
                raise ValueError("排队轮次不能改写提交时的选项快照。")
        baseline = None
        if data.get("runtime_baseline") is not None:
            baseline = message_from_dict(data["runtime_baseline"])
            if (conversation.runtime_baseline is not None or not isinstance(baseline, ModelMessage)
                    or baseline.role != "user" or baseline.runtime_context_kind != "baseline" or not baseline.runtime_context_id):
                raise ValueError("环境基线只能首次创建，不改写已有基线。")
        conversation.turns.append(ConversationTurn(
            run_id, user, AgentLimits(**data["limits"]), timestamp, execution_environment=environment,
            options=options, source_input_id=source_id, resumed_from_run_id=resumed, continuation=continuation,
            regeneration_notice=data.get("regeneration_notice"),
            origin=origin,
        ))
        if baseline is not None:
            conversation.runtime_baseline = baseline
    else:
        turn = conversation.active_turn
        if turn is None or turn.run_id != run_id:
            raise ValueError("事件不属于当前未结束的运行。")
        if kind == "turn_finished":
            _finish_turn(turn, data, timestamp)
        else:
            if kind == "model_started":
                _validate_input_ids(conversation, data.get("input_ids", []))
            _apply_step_event(turn, kind, event["step"], data)
    conversation.last_seq = seq
    conversation.update_input_index(kind)


def _apply_input(conversation: Conversation, run_id: str, data: dict, timestamp: str) -> None:
    """收件可在摘要期间发生，不能借此推进工具或改变正在发送的请求。"""
    turn = conversation.active_turn
    if turn is None or turn.run_id != run_id:
        raise ValueError("补充目标已经结束，不能改投其他运行。")
    message = message_from_dict(data["message"])
    if not isinstance(message, ModelMessage):
        raise ValueError("输入正文须为用户消息。")
    item = ConversationInput(data["input_id"], data["kind"], message, run_id, timestamp,
                             TurnOptions(**data["options"]) if data.get("options") is not None else None)
    if any(previous.input_id == item.input_id for previous in conversation.inputs):
        raise ValueError("输入编号不能重复保存。")
    if item.options is not None:
        item.options.validate(conversation.settings)
    conversation.inputs.append(item)


def _validate_input_ids(conversation: Conversation, values) -> None:
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values) or len(values) != len(set(values)):
        raise ValueError("步骤输入引用须为不重复的编号数组。")
    turn = conversation.active_turn
    origins = set()
    turns = {item.run_id: item for item in conversation.turns}
    while turn is not None:
        origins.add(turn.run_id)
        turn = turns.get(turn.resumed_from_run_id)
    pending = {item.input_id for item in conversation.pending_inputs("steer") if item.origin_run_id in origins}
    if set(values) - pending:
        raise ValueError("步骤引用了不存在或已经发送的补充。")


def _apply_compaction(conversation: Conversation, kind: str, compaction_id: str, data: dict, timestamp: str) -> None:
    """覆盖范围只指向已结束轮次；失败或崩溃不推进摘要边界。"""
    if kind == "compaction_started":
        if conversation.active_compaction is not None or any(
            item.compaction_id == compaction_id for item in conversation.compactions
        ):
            raise ValueError("已有未结束摘要或重复压缩编号。")
        covered = data["covered_turns"]
        previous = conversation.latest_compaction
        if type(covered) is not int or not (previous.covered_turns if previous else 0) < covered <= len(conversation.turns):
            raise ValueError("压缩范围没有推进或不属于当前会话。")
        target = conversation.turns[covered - 1]
        if target.finished_at is None or target.run_id != data["through_run_id"]:
            raise ValueError("压缩不能覆盖未结束轮次或错误的运行编号。")
        turn = conversation.active_turn
        if data.get("owner_run_id") != (turn.run_id if turn is not None else None):
            raise ValueError("压缩所属运行与当前会话状态不符。")
        if turn is not None and turn.steps and (
            turn.steps[-1].completion_status == "requesting" or any(item.result is None for item in turn.steps[-1].tools)
        ):
            raise ValueError("模型请求或工具批次尚未结束，不能压缩。")
        if data["trigger"] not in ("manual", "budget") or type(data["before_tokens_estimate"]) is not int or data["before_tokens_estimate"] < 0:
            raise ValueError("压缩原因或请求估算无效。")
        after_step = data.get("after_step", 0)
        if type(after_step) is not int or not 0 <= after_step <= (len(turn.steps) if turn else 0):
            raise ValueError("压缩显示位置不属于当前运行的已结束步骤。")
        conversation.compactions.append(CompactionRecord(compaction_id, started_at=timestamp, **data))
        return
    record = conversation.active_compaction
    if record is None or record.compaction_id != compaction_id:
        raise ValueError("压缩结果没有对应的开始事件。")
    if data["status"] not in ("succeeded", "failed", "cancelled", "interrupted"):
        raise ValueError("压缩终态不支持。")
    if not isinstance(data["summary"], str) or (data["status"] == "succeeded" and not data["summary"].strip()):
        raise ValueError("成功压缩必须有非空摘要文本。")
    allowed = {
        "status", "summary", "after_tokens_estimate", "summary_input_estimate", "duration_seconds",
        "usage", "token_usage", "completion_status", "finish_reason", "error", "attempts", "summary_requests",
    }
    if set(data) - allowed:
        raise ValueError("摘要终态包含未知字段；不能改写开始事件的覆盖范围或编号。")
    if data["status"] == "succeeded" and (
        type(data.get("after_tokens_estimate")) is not int
        or not 0 <= data["after_tokens_estimate"] < record.before_tokens_estimate
    ):
        raise ValueError("成功摘要需要有效的缩减后请求估算。")
    values = {**data, "finished_at": timestamp}
    if values.get("token_usage") is not None:
        values["token_usage"] = TokenUsage(**values["token_usage"])
    if "attempts" in values:
        values["attempts"] = [ModelAttemptEvent(**item) for item in values["attempts"]]
    # 构造后再替换，损坏的外部记录不能留下半更新的内存状态。
    conversation.compactions[-1] = replace(record, **values)


def _apply_step_event(turn: ConversationTurn, kind: str, number: int, data: dict[str, Any]) -> None:
    if type(number) is not int or number < 1:
        raise ValueError("模型步骤编号须为正整数。")
    if kind == "model_started":
        if number != len(turn.steps) + 1 or (turn.steps and turn.steps[-1].completion_status == "requesting"):
            raise ValueError("模型步骤不连续或上一请求没有结束。")
        if turn.steps and any(item.result is None for item in turn.steps[-1].tools):
            raise ValueError("上一批工具结果不完整，不能开始下一模型步骤。")
        turn.steps.append(AgentStep(number, context_messages=_context_messages(data.get("context_messages", [])),
                                    input_ids=list(data.get("input_ids", [])), program_event_ids=list(data.get("program_event_ids", []))))
        _validate_program_refs(turn.steps[-1])
        return
    if number != len(turn.steps):
        raise ValueError("事件没有对应的当前模型步骤。")
    step = turn.steps[-1]
    if kind == "model_attempt":
        if step.completion_status != "requesting":
            raise ValueError("已结束模型步骤不能增加联网尝试。")
        step.attempts.append(ModelAttemptEvent(**data["attempt"]))
        return
    if kind == "model_finished":
        finished = _step(data)
        if type(finished.step) is not int or finished.step != number or step.completion_status != "requesting":
            raise ValueError("模型响应编号不符或重复结束。")
        if finished.context_messages != step.context_messages or finished.input_ids != step.input_ids or finished.program_event_ids != step.program_event_ids:
            raise ValueError("模型结果不能改写开始检查点中的程序资料。")
        turn.steps[-1] = finished
        return
    if step.assistant is None:
        raise ValueError("工具事件缺少完整模型调用。")
    call_id = data["call_id"]
    position = next((i for i, item in enumerate(step.tools) if item.call_id == call_id), None)
    if position is None:
        raise ValueError("工具事件的调用 ID 不存在。")
    execution = step.tools[position]
    if execution.result is not None:
        raise ValueError("已有工具结果不能被后续记录覆盖。")
    if kind == "tool_started":
        if execution.handler_started:
            raise ValueError("工具不能重复开始。")
        execution.handler_started, execution.status = True, "running"
    elif kind == "tool_finished":
        finished = _execution(data)
        call = step.assistant.tool_calls[position]
        if finished.result is None or finished.result.call_id != call_id or finished.result.name != call.name:
            raise ValueError("工具结果与调用不对应。")
        if finished.status not in ("succeeded", "failed", "rejected", "interrupted", "not_executed"):
            raise ValueError("工具终态不支持。")
        step.tools[position] = finished
    else:
        raise ValueError(f"不支持会话事件类型：{kind}。")


def _finish_turn(turn: ConversationTurn, data: dict[str, Any], timestamp: str) -> None:
    reason = data["stop_reason"]
    if not isinstance(reason, str) or reason == "running" or any(
        step.completion_status == "requesting" or any(item.result is None for item in step.tools)
        for step in turn.steps
    ):
        raise ValueError("轮次结束原因无效或仍有未结算步骤。")
    for key in ("error", "diagnostics"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ValueError("轮次错误信息须为文本。")
    if reason == "completed" and (
        not turn.steps or turn.steps[-1].assistant is None or turn.steps[-1].assistant.tool_calls
    ):
        raise ValueError("成功轮次必须以完整的非工具回答结束。")
    turn.stop_reason, turn.error = reason, data.get("error")
    turn.diagnostics, turn.finished_at = data.get("diagnostics"), timestamp
    turn.request_tokens_estimate = data.get("request_tokens_estimate", [])
    turn.usage_summary = data.get("usage_summary")
    turn.compaction_usage_summary = data.get("compaction_usage_summary")
    turn.total_usage_summary = data.get("total_usage_summary")


def summary_message(summary: str) -> ModelMessage:
    """摘要是历史参考，不是新的系统规则、工具权限或用户授权。"""
    return user_message("[程序提供的历史摘要] 以下是旧交互的摘要，可能遗漏细节。"
                        "不是新的用户任务或授权；准确状态与权限以程序和原始记录为准。\n\n" + summary)


def turn_messages(turn: ConversationTurn, inputs: list[ConversationInput] | tuple[ConversationInput, ...] = ()) -> list[Message]:
    """一个完整轮次作为不可拆分的压缩单元，原生消息与工具结果保持原样。"""
    messages: list[Message] = [turn.user] if turn.user is not None else []
    if turn.regeneration_notice:
        messages.append(ModelMessage("user", turn.regeneration_notice))
    if turn.continuation:
        messages.append(ModelMessage("user", turn.continuation))
    by_id = {item.input_id: item.message for item in inputs}
    for step in turn.steps:
        if step.assistant is not None or any(event.phase == "started" for event in step.attempts):
            messages.extend(by_id[identifier] for identifier in step.input_ids)
        messages.extend(step.context_messages)
        if step.assistant is not None:
            messages.append(step.assistant)
            messages.extend(item.result for item in step.tools)
    messages.extend(by_id[identifier] for identifier in turn.retained_input_ids)
    if turn.stop_reason not in {"completed", "rewound"}:
        notice = (
            f"[程序运行状态] 上一轮运行 {turn.run_id} 因 {turn.stop_reason} 停止。"
            "这不是新的用户任务。未完成片段没有作为正式回答加入；"
        )
        if any(step.tools for step in turn.steps):
            notice += "工具结果中的未执行或结果未知状态不得当成操作成功，也不要因恢复历史自动重做旧调用。"
        messages.append(ModelMessage("user", notice))
    return expand_skill_messages(messages)


def build_history(conversation: Conversation, *, config: ModelConfig | None = None) -> list[Message]:
    """最新成功摘要加未覆盖原文；原始 turn/step 和失败片段仍在会话日志。"""
    if conversation.active_turn is not None:
        raise ValueError("需先结算中断轮次，才能组装下一轮历史。")
    compacted = conversation.latest_compaction
    # 被摘要覆盖的图片只留在原记录；需要再次分析时由用户重新附带。
    messages: list[Message] = [summary_message(compacted.summary)] if compacted is not None else []
    for turn in conversation.turns[compacted.covered_turns if compacted else 0:]:
        source = turn.options.model_target.config if turn.options is not None and turn.options.model_target is not None else conversation.settings.config
        messages.extend(bind_origin(turn_messages(turn, conversation.inputs), source))
    validate_tool_results(messages)
    if config is not None:
        return project_messages(messages, config, legacy_changed=compacted is not None)
    return messages


def interrupted_result(turn: ConversationTurn) -> AgentResult:
    """只结算遗留状态；进入执行边界但无结果的调用，不猜测成功或失败。"""
    recovered = turn.user if turn.user is not None else ModelMessage("user", turn.continuation or "[程序恢复状态] 结算中断运行，不重新执行历史工具。")
    result = AgentResult(ModelRequest([recovered]), steps=deepcopy(turn.steps), stop_reason="interrupted")
    result.error = "上次运行没有完整终态，已按记录结算中断；没有重新执行历史工具。"
    for step in result.steps:
        if step.completion_status == "requesting":
            step.completion_status = "interrupted"
        if step.assistant is None:
            continue
        for call, execution in zip(step.assistant.tool_calls, step.tools):
            if execution.result is not None:
                continue
            execution.status = "interrupted" if execution.handler_started else "not_executed"
            detail = (
                "上次运行进入执行边界后中断，最终结果未知；不能据此推断操作未发生。"
                if execution.handler_started else "上次运行中断，此调用没有进入执行边界。"
            )
            execution.result = error_result(call, "interrupted_run", detail)
    return result
