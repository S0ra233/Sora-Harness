"""已确认 Messages 缓存接口的断点；已有标记与签名内容保持原样。"""


def mark_anthropic_prefix(payload: dict, *, auxiliary: bool = False) -> None:
    tools = payload.get("tools", [])
    system = payload.get("system", [])
    messages = payload.get("messages", [])
    blocks = [*tools, *(system if isinstance(system, list) else []),
              *(block for message in messages for block in message.get("content", [])
                if isinstance(block, dict))]
    remaining = 4 - sum("cache_control" in block for block in blocks)
    def mark(block):
        nonlocal remaining
        if remaining > 0 and "cache_control" not in block and block.get("type") not in {"thinking", "redacted_thinking"}:
            block["cache_control"] = {"type": "ephemeral"}
            remaining -= 1
    if tools:
        mark(tools[-1])
    if remaining > 0 and isinstance(system, str):
        system = payload["system"] = [{"type": "text", "text": system}]
    if isinstance(system, list) and system:
        mark(system[-1])
    candidates = messages[:-1] if auxiliary else messages
    for message in reversed(candidates):
        content = message.get("content")
        if isinstance(content, str):
            content = message["content"] = [{"type": "text", "text": content}]
        eligible = [block for block in (content or []) if isinstance(block, dict)
                    and block.get("type") not in {"thinking", "redacted_thinking"}]
        if eligible:
            mark(eligible[-1])
            break
    # 历史较早的用户断点便于跨轮命中；不改写已有 TTL 或超过数量限制。
    for message in reversed(candidates[:-1]):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list) and content:
                eligible = [block for block in content if block.get("type") not in {"thinking", "redacted_thinking"}]
                if eligible:
                    mark(eligible[-1])
                    break
