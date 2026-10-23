"""公开思考的展示投影；原生签名与回放内容不在这里改写。"""
from dataclasses import dataclass

from .messages import ReplayState


@dataclass(frozen=True)
class ReasoningBlock:
    block_id: str
    text: str
    kind: str = "reasoning"

    def __post_init__(self) -> None:
        if not isinstance(self.block_id, str) or not self.block_id or not isinstance(self.text, str) or self.kind not in {"reasoning", "summary"}:
            raise ValueError("公开思考块需要来源编号、文本和已支持的内容类型。")


@dataclass(frozen=True)
class ReasoningDelta:
    block_id: str
    text: str = ""
    status: str = "delta"
    kind: str = "reasoning"

    def __post_init__(self) -> None:
        if (not isinstance(self.block_id, str) or not self.block_id or not isinstance(self.text, str)
                or self.status not in {"started", "delta", "finished"} or self.kind not in {"reasoning", "summary"}):
            raise ValueError("公开思考通知的编号、文本、状态或类型无效。")


def responses_reasoning_id(item_id: object, output_index: int, kind: str, part_index: int = 0) -> str:
    """流式与完整响应共用来源编号，明文和摘要的同索引块不能覆盖彼此。"""
    identifier = item_id if isinstance(item_id, str) and item_id else f"output:{output_index}"
    return f"responses:{identifier}:{kind}:{part_index}"


def responses_reasoning_blocks(item: dict, output_index: int) -> tuple[ReasoningBlock, ...]:
    """只投影明确公开的Responses文本，不修改原生项或取出加密内容。"""
    blocks = []
    for field, kind, expected_type in (("content", "reasoning", "reasoning_text"),
                                       ("summary", "summary", "summary_text")):
        parts = item.get(field)
        if not isinstance(parts, list):
            continue
        for number, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            # 兼容旧网关省略摘要type；明文content必须明确声明reasoning_text。
            if part.get("type") != expected_type and not (field == "summary" and part.get("type") is None):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                blocks.append(ReasoningBlock(responses_reasoning_id(item.get("id"), output_index, kind, number), text, kind))
    return tuple(blocks)


def public_reasoning(state: ReplayState | None) -> tuple[tuple[ReasoningBlock, ...], bool | None]:
    """只提取接口已公开的文本；返回第二项表示有无实际思考证据。"""
    if state is None:
        return (), None
    data, blocks, active = state.payload, [], None
    def array(value):
        return value if isinstance(value, list) else []
    def add(identifier, text, kind="reasoning"):
        nonlocal active
        active = True
        if isinstance(text, str) and text.strip():
            blocks.append(ReasoningBlock(str(identifier), text, kind))
    if state.api_style == "chat_completions":
        message = data.get("message", {})
        if not isinstance(message, dict):
            return (), None
        details = array(message.get("reasoning_details"))
        for index, detail in enumerate(details):
            if not isinstance(detail, dict):
                continue
            kind = detail.get("type")
            if kind == "reasoning.encrypted":
                active = True
            elif kind in {"reasoning.text", "reasoning.summary"}:
                add(detail.get("index", detail.get("id") or index),
                    detail.get("summary") if kind == "reasoning.summary" else detail.get("text"),
                    "summary" if kind == "reasoning.summary" else "reasoning")
        if not blocks:
            text = message.get("reasoning_content") or message.get("reasoning")
            if isinstance(text, str) and text:
                add("chat", text)
    elif state.api_style == "responses":
        for index, item in enumerate(array(data.get("output"))):
            if isinstance(item, dict) and item.get("type") == "reasoning":
                active = True
                blocks.extend(responses_reasoning_blocks(item, index))
    elif state.api_style == "anthropic_messages":
        for index, item in enumerate(array(data.get("content"))):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "redacted_thinking":
                active = True
            elif item.get("type") == "thinking":
                add(index, item.get("thinking"))
    elif state.api_style == "gemini":
        group, fragments, previous_thought = 0, [], False
        content = data.get("content", {})
        if not isinstance(content, dict):
            return (), None
        for item in array(content.get("parts")):
            if not isinstance(item, dict):
                continue
            thought = item.get("thought") is True
            if previous_thought and not thought:
                if fragments:
                    add(f"gemini:{group}", "".join(fragments), "summary")
                    fragments.clear()
                group += 1
            previous_thought = thought
            if thought:
                active = True
                if isinstance(item.get("text"), str):
                    fragments.append(item["text"])
        if fragments:
            add(f"gemini:{group}", "".join(fragments), "summary")
    return tuple(blocks), active
