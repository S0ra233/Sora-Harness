"""登记本地工具，检查本轮可用范围并把执行结果关联回调用。"""
import json
import asyncio
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

from ..llm.messages import ToolCall, ToolResult, ToolSchema
from .contracts import ToolContext, ToolDefinition, ToolError, ToolExecutionUnknown
from .artifacts import ToolArtifacts


class ToolRegistrationError(ValueError):
    """登记冲突必须报告，不能用后来的实现覆盖已有能力。"""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self.registration_errors: list[str] = []
        self._sources: dict[str, str | None] = {}

    def register(self, definition: ToolDefinition) -> None:
        """启动时登记；同名报错，不让后登记的实现覆盖原工具。"""
        self.register_many((definition,))

    def register_many(self, definitions: Iterable[ToolDefinition], *, source: str | None = None) -> None:
        """插件的一批工具先完整校验，再发布；失败不会留下半份贡献。"""
        pending = dict(self._tools)
        sources = dict(self._sources)
        for definition in definitions:
            if not isinstance(definition, ToolDefinition):
                raise ToolRegistrationError("工具工厂须返回ToolDefinition。")
            name = definition.schema.name
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", name) is None:
                raise ToolRegistrationError("模型工具名称须为字母或下划线开头的ASCII名称，最多64字符。")
            if name in pending:
                raise ToolRegistrationError(f"模型工具已登记：{name}")
            pending[name] = definition
            sources[name] = source
        self._tools = pending
        self._sources = sources

    def source(self, name: str) -> str | None:
        return self._sources.get(name)

    def schemas(self, names: Iterable[str]) -> list[ToolSchema]:
        """应用选择本轮工具；未知名字属于配置错误，在请求前报告。"""
        selected = list(names)
        if len(selected) != len(set(selected)):
            raise ValueError("本轮可用工具名称不能重复。")
        try:
            return [self._tools[name].schema for name in selected]
        except KeyError as exc:
            issues = "；插件加载诊断：" + "；".join(self.registration_errors) if self.registration_errors else ""
            raise ValueError(f"尚未登记模型工具：{exc.args[0]}{issues}") from exc

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """启动目录与本地展示读取定义，不执行handler或扩大本轮允许范围。"""
        return tuple(self._tools.values())

    def execution_class(self, name: str) -> str:
        """未知工具保守作为串行边界，权限仍由execute检查。"""
        definition = self._tools.get(name)
        return definition.execution_class if definition is not None else "exclusive"

    def _extension_error(self, call, context, definition, exc):
        source = self.source(call.name)
        if source is None:
            raise exc
        logging.getLogger(__name__).error("扩展工具失败：来源=%s 工具=%s 调用=%s 错误类型=%s",
                                          source, call.name, call.call_id, type(exc).__name__)
        unknown = definition.execution_class != "read"
        result = _record_result(call, context, error={"code": "extension_execution_error",
            "message": "扩展工具实现或返回结构异常；请检查插件，不要重复尝试同一参数。",
            "details": {**{key: value for key, value in context.execution_details.items()
                           if key in {"record_id", "receipt_path", "artifacts_path", "mutation_status"} and isinstance(value, str)},
                        "source": source, "exception_type": type(exc).__name__,
                        "execution_outcome": "unknown" if unknown else "failed"}})
        if unknown:
            raise ToolExecutionUnknown(result) from exc
        return result

    def _result(self, call, context, definition, *, data=None, error=None):
        """成功与业务错误共用返回结构边界，不让插件的非法详情绕过隔离。"""
        try:
            return _record_result(call, context, data=data, error=error)
        except (TypeError, ValueError, RecursionError) as exc:
            return self._extension_error(call, context, definition, exc)

    async def execute(
        self, call: ToolCall, context: ToolContext, *, on_execute: Callable[[], None] | None = None,
    ) -> ToolResult:
        """平台不变量错误停止运行；扩展错误隔离到调用，未知修改结果阻断后续执行。"""
        details: dict[str, Any] = {}
        entered = False
        definition = None
        context = replace(context, call_id=call.call_id, execution_details=details, audit_details={})
        if context.subagents is not None:
            context = replace(context, subagents=context.subagents.for_call(call.call_id))
        try:
            if call.name not in context.allowed_tools:
                raise ToolError("tool_not_allowed", "本轮未开放这个工具，请使用请求中提供的工具。")
            definition = self._tools.get(call.name)
            if definition is None:
                raise ToolError("unknown_tool", "该工具没有本地实现。")
            try:
                arguments = json.loads(call.arguments)
            except (ValueError, RecursionError) as exc:
                raise ToolError("invalid_arguments", "工具参数必须是完整的 JSON 对象。") from exc
            if not isinstance(arguments, dict):
                raise ToolError("invalid_arguments", "工具参数顶层必须是 JSON 对象。")
            async with asyncio.timeout(definition.timeout_seconds):
                from ..application.mutation_gate import mutation_gate
                async with mutation_gate(context.mutation_lock if definition.execution_class == "exclusive" else None):
                    if on_execute is not None:
                        on_execute()
                    entered = True
                    try:
                        value = await definition.execute(arguments, context)
                    except (ToolError, OSError, TimeoutError, asyncio.CancelledError):
                        raise
                    except Exception as exc:
                        return self._extension_error(call, context, definition, exc)
        except ToolError as exc:
            if exc.details is not None and not isinstance(exc.details, dict):
                return self._extension_error(call, context, definition, TypeError("工具错误详情须为JSON对象。"))
            evidence = {**details, **(exc.details or {})}
            result = self._result(call, context, definition, error={"code": exc.code, "message": str(exc), "details": evidence})
            if entered and definition.execution_class != "read" and evidence.get("mutation_status") == "interrupted":
                raise ToolExecutionUnknown(result) from exc
            return result
        except (TimeoutError, OSError) as exc:
            timed_out = isinstance(exc, TimeoutError)
            unknown = entered and definition.execution_class != "read"
            result = self._result(call, context, definition, error={
                "code": "tool_timeout" if timed_out else "tool_io_error",
                "message": ("工具等待或执行超时。" if timed_out else "工具遇到文件或记录读写错误。") +
                           ("请核对收据和实际文件，未继续后续修改。" if unknown else "处理函数未执行。" if not entered else "本次读取未完成。"),
                "details": {**details, "stop_reason": "timeout" if timed_out else "io_error", "handler_started": entered,
                            "execution_outcome": "unknown" if unknown else "failed" if entered else "not_executed",
                            "errno": getattr(exc, "errno", None), "winerror": getattr(exc, "winerror", None)}})
            if unknown:
                raise ToolExecutionUnknown(result) from exc
            return result
        except asyncio.CancelledError as exc:
            exc.tool_details = details
            try:
                exc.tool_result = _record_result(call, context, error={"code": "cancelled",
                    "message": "运行取消；文件操作的实际状态请看收据。" if entered else "运行取消；处理函数未执行。",
                    "details": {**details, "handler_started": entered}})
            except (TypeError, ValueError, RecursionError):
                exc.tool_result = error_result(call, "cancelled", "运行已取消，工具附带详情无法编码；请核对实际文件。",
                                              details={"handler_started": entered})
            raise
        # 非 JSON 返回是工具实现错误，不伪装成参数问题让模型反复重试。
        try:
            if not isinstance(value, dict):
                raise TypeError("工具结果须为JSON对象。")
            return _record_result(call, context, data=value)
        except (TypeError, ValueError, RecursionError) as exc:
            return self._extension_error(call, context, definition, exc)


def error_result(call: ToolCall, code: str, message: str, *, details: dict[str, Any] | None = None) -> ToolResult:
    """预期失败、取消或未执行仍提供对应 ID，避免留下孤立调用。"""
    content: dict[str, Any] = {"status": "error", "call_id": call.call_id, "name": call.name,
                              "error": {"code": code, "message": message, "details": details or {}}}
    return ToolResult(call.call_id, call.name, json.dumps(content, ensure_ascii=False), is_error=True)


def _record_result(call: ToolCall, context: ToolContext, *, data=None, error=None) -> ToolResult:
    """结果正文只编码一次；只读操作也提供可定位的本地收据，已有修改收据不重复建立。"""
    body = {"status": "error" if error is not None else "ok", "call_id": call.call_id, "name": call.name}
    body["error" if error is not None else "data"] = error if error is not None else data
    payload = error.get("details", {}) if error is not None else data
    references: dict[str, Any] = ({key: payload[key] for key in ("record_id", "receipt_path", "artifacts_path") if key in payload}
                  if "receipt_path" in payload and "artifacts_path" in payload else {})
    if not references and context.artifacts_dir is not None:
        try:
            # 只读结果留审计，不把创建收据误记成文件修改；实际修改沿已有handler收据。
            artifacts = ToolArtifacts(replace(context, execution_details=None), call.name)
            references = artifacts.references()
            references["record_access"] = _record_access(references, context)
            result_path = artifacts.directory / "result.json"
            result_path.write_text(json.dumps({**body, **references}, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            artifacts.update(body["status"], result_path=str(result_path))
        except (OSError, ValueError):
            body["record_warning"] = "工具结果可用，但本地收据保存失败。"
    if references and "record_access" not in references:
        references["record_access"] = _record_access(references, context)
    body.update(references)
    return ToolResult(call.call_id, call.name, json.dumps(body, ensure_ascii=False, allow_nan=False), is_error=error is not None)


def _record_access(references: dict, context: ToolContext) -> dict:
    """按本轮真实授权说明访问方法；路径引用本身不能开放根外读取。"""
    identifier = references.get("record_id")
    reader_open = "read_tool_record" in context.allowed_tools
    available = reader_open and isinstance(identifier, str) and identifier in context.artifact_ids
    if available:
        message = "本次调用记录用read_tool_record传record_id和file查看；offset/length用于分页。"
    elif not reader_open:
        message = "此引用仅供人工审计；本会话未开放记录读取入口，不能用文件工具读取工作区外路径。"
    else:
        message = "此引用没有本会话可验证的签发记录ID，仅供人工审计；已开放的记录读取工具不能据路径扩大权限。"
    return {"availability": "available" if available else "human_audit_only",
            "tool": "read_tool_record" if available else None, "record_id": identifier,
            "message": message}
