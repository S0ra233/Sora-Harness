"""目标模型的发送历史；转换副本，不改变日志、执行ID或公开思考展示。"""
import hashlib
import json
import re
from dataclasses import asdict, replace

from .messages import Message, ModelMessage, ReplayState, ToolResult, validate_tool_results
from .models import ModelConfig, ModelRequest


def same_origin(state: ReplayState, config: ModelConfig) -> bool:
    if (state.api_style, state.endpoint_url, state.model) != (config.api_style, config.endpoint_url, config.model):
        return False
    # 旧独立请求没有来源字段；会话恢复先以所属轮次补齐来源。
    return state.provider_kind is None or (
        state.provider_kind, state.credential_ref, state.key_env
    ) == (config.provider_kind, config.credential_ref, config.key_env)


def bind_origin(messages: list[Message], config: ModelConfig) -> list[Message]:
    """缺字段的旧原生块从轮次快照取得来源，不借当前候选目录推测。"""
    return [replace(message, replay_state=replace(message.replay_state,
                provider_kind=config.provider_kind, credential_ref=config.credential_ref, key_env=config.key_env))
            if isinstance(message, ModelMessage) and message.replay_state is not None
            and message.replay_state.provider_kind is None else message for message in messages]


def _without_thinking(state: ReplayState) -> ReplayState:
    blocks = state.payload.get("content")
    if not isinstance(blocks, list) or any(not isinstance(block, dict) for block in blocks):
        raise ValueError("原生思考续接的content结构不正确，历史未修改。")
    payload = {**state.payload, "content": [block for block in blocks
                 if block.get("type") not in {"thinking", "redacted_thinking"}]}
    return replace(state, payload=payload, request_prefix_hash=None)


def project_messages(messages: list[Message], config: ModelConfig, *, legacy_changed: bool = False) -> list[Message]:
    """外部历史保留公开正文和完整工具对；ID仅在发送副本中确定性转换。"""
    validate_tool_results(messages)
    output, ids = [], {}
    occupied = {call.call_id for message in messages if isinstance(message, ModelMessage) for call in message.tool_calls}
    changed = legacy_changed
    for message in messages:
        if isinstance(message, ToolResult):
            output.append(replace(message, call_id=ids.get(message.call_id, message.call_id)))
            continue
        state = message.replay_state
        if state is None or same_origin(state, config):
            if (state is not None and config.api_style == "anthropic_messages" and changed
                    and state.request_prefix_hash is None):
                message = replace(message, replay_state=_without_thinking(state))
            output.append(message)
            continue
        changed = True
        calls = []
        for call in message.tool_calls:
            identifier = call.call_id
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier) is None:
                identifier = "call_" + hashlib.sha256(call.call_id.encode("utf-8")).hexdigest()[:48]
                if identifier in occupied:
                    raise ValueError("跨模型工具ID转换发生冲突，历史未修改。")
            ids[call.call_id] = identifier
            calls.append(replace(call, call_id=identifier))
        imported = replace(message, tool_calls=calls, replay_state=None)
        if config.api_style == "gemini" and calls:
            from .providers.protocol_messages import object_arguments
            parts = [{"text": imported.text}] if imported.text else []
            for index, call in enumerate(calls):
                part = {"functionCall": {"id": call.call_id, "name": call.name, "args": object_arguments(call)}}
                if index == 0:
                    # 官方外部历史导入标记；不替代本模型的真实签名。
                    part["thoughtSignature"] = "skip_thought_signature_validator"
                parts.append(part)
            imported = replace(imported, replay_state=ReplayState(config.api_style, config.endpoint_url,
                config.model, {"content": {"parts": parts}, "call_ids": {call.call_id: call.call_id for call in calls}},
                provider_kind=config.provider_kind, credential_ref=config.credential_ref, key_env=config.key_env))
        output.append(imported)
    validate_tool_results(output)
    return output


def _hash_value(hasher, value) -> None:
    # 按块编码，避免为每一个签名重新序列化前面全部历史。
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for chunk in encoder.iterencode(value):
        hasher.update(chunk.encode("utf-8"))
    hasher.update(b"\n")


def project_request(request: ModelRequest, config: ModelConfig, *, legacy_changed: bool = False) -> tuple[ModelRequest, str | None]:
    """预算与Provider共用视图；签名前缀指纹只在Messages协议计算。"""
    key = (config.api_style, config.endpoint_url, config.model, config.provider_kind, config.credential_ref, config.key_env)
    prepared = getattr(request, "_prepared_projection", None)
    identity = (tuple(id(message) for message in request.messages), tuple(id(tool) for tool in request.tools))
    if prepared is not None and prepared[:2] == (key, identity) and (not legacy_changed or prepared[2]):
        return request, prepared[3]
    messages = project_messages(request.messages, config, legacy_changed=legacy_changed)
    if config.api_style != "anthropic_messages":
        result, prefix = replace(request, messages=messages), None
        result._prepared_projection = (key, (tuple(map(id, result.messages)), tuple(map(id, result.tools))), legacy_changed, prefix)
        return result, prefix
    hasher = hashlib.sha256()
    _hash_value(hasher, {"system": [message.text for message in messages
        if isinstance(message, ModelMessage) and message.role == "system"], "tools": [asdict(tool) for tool in request.tools]})
    output = []
    for message in messages:
        if isinstance(message, ModelMessage):
            if message.role == "system":
                output.append(message)
                continue
            state = message.replay_state
            if (state is not None and state.request_prefix_hash is not None
                    and state.request_prefix_hash != hasher.hexdigest()):
                message = replace(message, replay_state=_without_thinking(state))
            value = {"role": message.role, "content": [asdict(part) for part in message.content],
                     "tool_calls": [asdict(call) for call in message.tool_calls],
                     "native": message.replay_state.payload if message.replay_state is not None else None}
        else:
            value = asdict(message)
        _hash_value(hasher, value)
        output.append(message)
    result, prefix = replace(request, messages=output), hasher.hexdigest()
    # 仅存于当前请求对象；replace产生的新请求不继承，既不落日志也不进入API JSON。
    result._prepared_projection = (key, (tuple(map(id, result.messages)), tuple(map(id, result.tools))), legacy_changed, prefix)
    return result, prefix


def consume_projected_request(request: ModelRequest, config: ModelConfig) -> tuple[ModelRequest, str | None]:
    """Provider接收准备结果后立即结束缓存生命周期，后续复用可变请求须重新投影。"""
    prepared, prefix = project_request(request, config)
    prepared.__dict__.pop("_prepared_projection", None)
    return prepared, prefix
