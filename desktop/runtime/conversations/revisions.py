"""修订只改变有效视图；原事件仍留在追加日志，工具事实不允许手工修改。"""
from dataclasses import asdict, dataclass, replace
from copy import deepcopy
from uuid import UUID

from ...llm.messages import ModelMessage, message_from_dict, validate_tool_results
from ...llm.usage import TokenUsage
from ...llm.attempts import ModelAttemptEvent
from .models import CompactionRecord, Conversation, ConversationInput, ConversationTurn, TurnOptions


@dataclass(frozen=True)
class MessageAnchor:
    run_id: str
    input_id: str | None = None
    step: int | None = None

    def __post_init__(self) -> None:
        if UUID(self.run_id).hex != self.run_id:
            raise ValueError("消息锚点需要稳定运行编号。")
        if self.input_id is not None and UUID(self.input_id).hex != self.input_id:
            raise ValueError("补充锚点需要稳定输入编号。")
        if self.step is not None and (type(self.step) is not int or self.step < 1 or self.input_id):
            raise ValueError("回答锚点需要正整数步骤，不能同时指定补充。")


COMPACTION_REVISION_ERROR = "会话已经成功压缩，只能查看、复制或建立分支，不能回退、修改或重新生成。"


def revision_unavailable(conversation: Conversation) -> str | None:
    """只检查新操作；旧修订事件仍由原投影读取，不追溯拒绝历史。"""
    return COMPACTION_REVISION_ERROR if conversation.latest_compaction is not None else None


def regeneration_anchor(conversation: Conversation, anchor: MessageAnchor) -> MessageAnchor:
    """重新执行须检查真正的用户目标，不能从压缩后的回答绕回旧请求。"""
    by_id = {turn.run_id: turn for turn in conversation.turns}
    turn = by_id.get(anchor.run_id)
    while turn is not None and turn.user is None and turn.resumed_from_run_id:
        turn = by_id.get(turn.resumed_from_run_id)
    if turn is None or turn.user is None:
        raise ValueError("该运行没有可重新执行的原用户消息。")
    return MessageAnchor(turn.run_id)


def selected_message(conversation: Conversation, anchor: MessageAnchor) -> ModelMessage:
    if anchor.input_id:
        item = next((item for item in conversation.inputs if item.input_id == anchor.input_id), None)
        if item is None or not any(turn.run_id == anchor.run_id for turn in conversation.turns):
            raise ValueError("补充消息不再属于当前有效对话。")
        return item.message
    turn = next((turn for turn in conversation.turns if turn.run_id == anchor.run_id), None)
    if turn is None:
        raise ValueError("所选消息已经不在当前有效对话中。")
    if anchor.step is not None:
        step = next((step for step in turn.steps if step.step == anchor.step), None)
        if (step is None or step.assistant is None or step.assistant.tool_calls
                or step.completion_status != "completed" or step is not turn.steps[-1]):
            raise ValueError("只允许编辑本轮最后一个完整的普通回答。")
        return step.assistant
    if turn.user is None:
        raise ValueError("所选运行没有独立用户消息。")
    return turn.user


def cut_history(conversation: Conversation, anchor: MessageAnchor, *, timestamp: str,
                replacement: str | None = None) -> None:
    """先构造合法前缀，再一次替换；不会拆开已完成的工具批次。"""
    selected_message(conversation, anchor)
    position = next(i for i, turn in enumerate(conversation.turns) if turn.run_id == anchor.run_id)
    kept = list(conversation.turns[:position])
    if anchor.input_id:
        # 补充可能由暂停后的新运行首次使用，以实际请求位置为准。
        found = next(((i, j) for i, turn in enumerate(conversation.turns)
                      for j, step in enumerate(turn.steps) if anchor.input_id in step.input_ids), None)
        if found is not None:
            position, boundary = found
        else:
            boundary = len(conversation.turns[position].steps)
        turn = conversation.turns[position]
        steps = list(turn.steps[:boundary])
        removed_inputs = []
        while steps and (steps[-1].assistant is None or any(tool.result is None for tool in steps[-1].tools)):
            removed_inputs[:0] = steps.pop().input_ids
        earlier = []
        if found is not None:
            ids = turn.steps[boundary].input_ids
            earlier = ids[:ids.index(anchor.input_id)]
        kept = list(conversation.turns[:position])
        if turn.user is not None or steps or earlier:
            retained = [*turn.retained_input_ids, *removed_inputs, *earlier]
            retained = retained[:retained.index(anchor.input_id)] if anchor.input_id in retained else retained
            kept.append(replace(turn, steps=steps, retained_input_ids=list(dict.fromkeys(retained)),
                                stop_reason="rewound", finished_at=timestamp, error=None, diagnostics=None,
                                usage_summary=None, total_usage_summary=None, compaction_usage_summary=None))
    elif anchor.step is not None:
        if not isinstance(replacement, str) or not replacement.strip():
            raise ValueError("修改后的回答须为非空文本。")
        turn = conversation.turns[position]
        steps = list(turn.steps)
        index = next(i for i, step in enumerate(steps) if step.step == anchor.step)
        # 新正文不能继续携带包含旧文字的厂商原生回放。
        steps[index] = replace(steps[index], assistant=ModelMessage("assistant", replacement))
        kept.append(replace(turn, steps=steps[:index + 1], error=None, diagnostics=None,
                            stop_reason="completed"))
    used = {turn.source_input_id for turn in kept if turn.source_input_id}
    used.update(identifier for turn in kept for identifier in turn.retained_input_ids)
    used.update(identifier for turn in kept for step in turn.steps for identifier in step.input_ids)
    inputs = [item for item in conversation.inputs if item.input_id in used]
    from .history import turn_messages
    validate_tool_results([message for turn in kept for message in turn_messages(turn, inputs)])
    conversation.turns, conversation.inputs = kept, inputs
    conversation.rebuild_input_index()
    # 包含截点所在轮次的摘要失效；原摘要事件仍在日志。
    conversation.compactions = [record for record in conversation.compactions
                               if record.finished_at is not None and record.covered_turns <= position
                               and (not record.covered_turns or kept[record.covered_turns - 1].run_id == record.through_run_id)
                               and (record.owner_run_id is None or any(turn.run_id == record.owner_run_id for turn in kept))]


def cut_branch_history(conversation: Conversation, anchor: MessageAnchor, *, point_time: str, timestamp: str) -> TurnOptions:
    """分支采用当时的选项及已生效摘要，裁切不能把后来摘要带回旧请求。"""
    turn = next(turn for turn in conversation.turns if turn.run_id == anchor.run_id)
    request_step = turn.steps[0].step if turn.steps else 1
    if anchor.input_id:
        consumed = next(((item, step) for item in conversation.turns for step in item.steps
                         if anchor.input_id in step.input_ids), None)
        if consumed:
            turn, step = consumed
            request_step = step.step
        else:
            request_step = len(turn.steps) + 1
    options = turn.options or TurnOptions(conversation.settings.reasoning_effort, conversation.settings.model_target)
    prompt = options.prompt_config or conversation.current_prompt
    if prompt != conversation.current_prompt:
        conversation.active_prompt = deepcopy(prompt)
        conversation.active_preset_id = conversation.settings.prompt_preset_id if prompt == conversation.settings.prompt_config else None
        conversation.active_preset_name = conversation.settings.prompt_preset_name if prompt == conversation.settings.prompt_config else None
        conversation.prompt_applied_at = None
    options = replace(options, model_target=options.model_target or conversation.settings.model_target,
                      prompt_config=deepcopy(prompt))
    records = [record for record in conversation.compactions if record.finished_at is not None
               and (record.after_step < request_step if record.owner_run_id == turn.run_id
                    else record.finished_at <= point_time)]
    cut_history(conversation, anchor, timestamp=timestamp)
    kept = {item.run_id for item in conversation.turns}
    conversation.compactions = [record if record.owner_run_id is None or record.owner_run_id in kept
                               else replace(record, owner_run_id=None, after_step=0)
                               for record in records if record.covered_turns <= len(conversation.turns)
                               and (not record.covered_turns or conversation.turns[record.covered_turns - 1].run_id == record.through_run_id)]
    return options


def seed_payload(conversation: Conversation) -> dict:
    """分支仅复制有效事实及基线，不复制锁、活动连接和旧待发送输入。"""
    return {"turns": [asdict(turn) for turn in conversation.turns],
            "inputs": [asdict(item) for item in conversation.inputs],
            "compactions": [asdict(record) for record in conversation.compactions],
            "runtime_baseline": asdict(conversation.runtime_baseline) if conversation.runtime_baseline else None,
            "active_prompt": asdict(conversation.active_prompt) if conversation.active_prompt else None,
            "active_preset_id": conversation.active_preset_id,
            "active_preset_name": conversation.active_preset_name,
            "prompt_applied_at": conversation.prompt_applied_at,
            "navigation": deepcopy(conversation.navigation)}


def apply_seed(conversation: Conversation, data: dict) -> None:
    """复用现有协议消息／步骤解析，种子只用于一个新建会话。"""
    from .history import _step
    from ..agent.models import AgentLimits
    from ...prompts import PromptConfig
    if conversation.turns or conversation.inputs or conversation.last_seq:
        raise ValueError("历史种子只能写入新会话。")
    turns = []
    for raw in data["turns"]:
        values = dict(raw)
        values["user"] = message_from_dict(values["user"]) if values["user"] else None
        values["limits"] = AgentLimits(**values["limits"])
        values["steps"] = [_step(step) for step in values["steps"]]
        values["options"] = TurnOptions(**values["options"]) if values["options"] else None
        turn = ConversationTurn(**values)
        if turn.finished_at is None or any(step.completion_status == "requesting" or any(tool.result is None for tool in step.tools) for step in turn.steps):
            raise ValueError("分支不能复制未结算的模型／工具批次。")
        turns.append(turn)
    inputs = [ConversationInput(**{**raw, "message": message_from_dict(raw["message"]),
              "options": TurnOptions(**raw["options"]) if raw["options"] else None}) for raw in data["inputs"]]
    records = []
    for raw in data["compactions"]:
        records.append(CompactionRecord(**{**raw,
            "token_usage": TokenUsage(**raw["token_usage"]) if raw["token_usage"] else None,
            "attempts": [ModelAttemptEvent(**event) for event in raw["attempts"]]}))
    baseline = message_from_dict(data["runtime_baseline"]) if data.get("runtime_baseline") else None
    if baseline is not None and (not isinstance(baseline, ModelMessage) or baseline.runtime_context_kind != "baseline"):
        raise ValueError("分支基线无效。")
    from .history import turn_messages
    validate_tool_results([message for turn in turns for message in turn_messages(turn, inputs)])
    conversation.turns, conversation.inputs, conversation.compactions = turns, inputs, records
    conversation.runtime_baseline = baseline
    conversation.active_prompt = PromptConfig(**data["active_prompt"]) if data.get("active_prompt") else None
    from .models import validate_preset_identity
    identifier, name = data.get("active_preset_id"), data.get("active_preset_name")
    validate_preset_identity(identifier, name)
    conversation.active_preset_id, conversation.active_preset_name = identifier, name
    conversation.prompt_applied_at = data.get("prompt_applied_at")
    conversation.navigation = deepcopy(data.get("navigation"))
    conversation.rebuild_input_index()
