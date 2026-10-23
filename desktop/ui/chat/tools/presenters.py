"""工具展示登记；执行状态统一处理，插件只定制已返回数据的详情。"""
from collections.abc import Callable, Iterable
from functools import lru_cache
from importlib import import_module
import logging

from ....plugin_catalog import PLUGINS, load_entry
from ....tools.catalog import builtin_tool_definitions
from ....tools.contracts import ToolDefinition, ToolDisplay
from .models import STATUS_LABELS, ToolPresentation, ToolView, _error_summary, _object, _text, _title
from .formatters import DETAIL_FORMATTERS, ToolDetail, ToolDetailInput, format_json


ToolFormatter = Callable[[ToolDetailInput], ToolDetail]
_GENERIC_DISPLAY = ToolDisplay()
_LOG = logging.getLogger(__name__)


class ToolPresenterRegistry:
    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._displays = {definition.schema.name: definition.display for definition in definitions}
        self._formatters: dict[str, ToolFormatter] = {}
        self.load_errors: list[str] = []

    def register_definitions(self, definitions: Iterable[ToolDefinition]) -> None:
        """只接收本地展示语义；执行函数不会进入Qt控件。"""
        pending = dict(self._displays)
        for definition in definitions:
            if not isinstance(definition, ToolDefinition):
                raise ValueError("工具展示目录须来自ToolDefinition。")
            name = definition.schema.name
            if name in pending:
                raise ValueError(f"工具展示定义已登记：{name}")
            pending[name] = definition.display
        self._displays = pending

    def register(self, entries: Iterable[tuple[str, ToolFormatter]]) -> None:
        """先检查整批，防止插件失败时只安装了一半展示函数。"""
        pending = dict(self._formatters)
        for name, formatter in entries:
            if name not in self._displays:
                raise ValueError(f"展示对应的工具未登记：{name}")
            if name in pending:
                raise ValueError(f"工具展示已登记：{name}")
            if not callable(formatter):
                raise ValueError(f"工具展示函数不可调用：{name}")
            pending[name] = formatter
        self._formatters = pending

    def present(self, view: ToolView) -> ToolPresentation:
        """仅在调用或结果变化时格式化，状态不由插件或退出码推测。"""
        fallback = ToolDisplay(category="mcp", icon="plugin", heading="MCP 工具", summary="调用了{n}个MCP工具",
                               result_subject_fields=("remote_tool",)) if view.name.startswith("mcp_") else _GENERIC_DISPLAY
        display = self._displays.get(view.name, fallback)
        args, result = _object(view.arguments), _object(view.result)
        error = result.get("error")
        error_text = _text(error.get("message")) if isinstance(error, dict) else ""
        data = result.get("details", {}) if isinstance(error, dict) else result
        if not isinstance(data, dict):
            data = {}
        resolved_values = tuple(_text(data.get(field)) for field in display.result_subject_fields) if view.status == "succeeded" else ()
        values = resolved_values if any(resolved_values) else tuple(_text(args.get(field)) for field in display.subject_fields)
        subject = (display.subject_separator.join(value for value in values if value) if display.subject_separator is not None
                   else next((value for value in values if value), "")) or view.name
        short_error = _error_summary(error) if isinstance(error, dict) else ""
        title_subject = subject.replace("\n", " ").replace("\r", " ")
        if short_error:
            title_subject = f"{short_error} · {title_subject}"
        title = _title(view, display.verb, title_subject)
        footer = STATUS_LABELS.get(view.status, view.status)
        if short_error:
            footer += " · " + short_error
        partial, notice = _coverage_notice(data)
        partial = view.status == "succeeded" and partial
        if partial:
            title = f"部分{display.verb}结果 {title_subject}"
        if view.status == "succeeded" and notice:
            footer += " · " + notice
        if isinstance(error, dict) and error.get("code") in {"tool_limit", "step_limit"}:
            footer += " · 超出本轮工具额度" if error["code"] == "tool_limit" else " · 达到模型步骤上限"
        context = ToolDetailInput(view, display, args, data, subject, error_text)
        formatter = self._formatters.get(view.name) or DETAIL_FORMATTERS.get(display.detail_kind, format_json)
        try:
            detail = formatter(context)
            if not isinstance(detail, ToolDetail):
                raise TypeError("详情格式器须返回ToolDetail")
        except Exception as exc:
            # 展示错误只降级当前行，不改变真实工具结果或重做操作。
            _LOG.error("工具详情展示失败：工具=%s 调用=%s 格式器=%s 错误类型=%s", view.name, view.call_id,
                       getattr(formatter, "__qualname__", type(formatter).__name__), type(exc).__name__)
            detail = format_json(context)
            footer += f" · 详情展示失败（{type(exc).__name__}），已显示原始结果"
        body = detail.body + ("\n\n" + error_text if error_text else "")
        if data.get("warnings"):
            footer += " · 详情含真实诊断"
        category, glyph, annotation = display.category, display.icon, detail.annotation
        if display.detail_kind == "diff" and data.get("status") == "unchanged" and view.status == "succeeded":
            title, category, glyph, annotation = f"已核对 {title_subject}，未修改", "metadata", "file", ""
        summary = "检查了{n}个路径" if category == "metadata" and category != display.category else display.summary
        return ToolPresentation(title, glyph, category, display.heading, body, footer + detail.footer,
                                annotation, detail.diff_highlight, partial, summary)


def _coverage_notice(data: dict) -> tuple[bool, str]:
    """分别表达扫描上限、IO缺口和显示分页，分页不等于执行失败。"""
    coverage = data.get("coverage")
    coverage = coverage if isinstance(coverage, dict) else {}
    reason = _text(data.get("stop_reason") or coverage.get("stop_reason"))
    limited = reason in {"cap_reached", "budget_reached", "io_errors", "timeout", "killed"}
    partial = data.get("status") == "partial" or limited
    notices = {
        "cap_reached": "已达结果上限，未完成全范围扫描",
        "budget_reached": "已达扫描预算，结果仅覆盖已处理范围",
        "io_errors": "部分路径不可读，已保留有效结果",
        "timeout": "达到时限，仅保留已返回结果",
        "killed": "扫描中断，仅保留已返回结果",
    }
    notice = notices.get(reason, "")
    if not notice and data.get("status") == "partial":
        notice = "部分文件区间读取失败" if "requested_files" in data else (
            "部分条目元数据不可读" if data.get("entries") is not None else "部分结果，详见诊断")
    if data.get("result_truncated") is True:
        partial = True
        notice += ("；" if notice else "") + "计数表未全部显示，合计的扫描范围见详情"
    if data.get("output_mode") in {"files_with_matches", "count", "summary"} and coverage.get("scan_complete") is False:
        partial = True
        if not notice:
            notice = "扫描范围未完整覆盖，仅含已观察结果"
    if data.get("has_more") is True or data.get("next_offset") is not None:
        notice += ("；" if notice else "") + "有后续分页"
    if data.get("directory_changed") is True:
        notice += ("；" if notice else "") + "目录已变化，请重新列举"
    if data.get("enumeration_complete") is False:
        partial = True
        notice += ("；" if notice else "") + "目录枚举未完成，总数未知"
    return partial, notice


@lru_cache(maxsize=1)
def default_presenters() -> ToolPresenterRegistry:
    """GUI启动目录只构造一次，不在流式更新时重复调用工具工厂。"""
    registry = ToolPresenterRegistry(builtin_tool_definitions())
    for descriptor in PLUGINS:
        if descriptor.create_tools is not None:
            try:
                registry.register_definitions(tuple(descriptor.create_tools()))
            except Exception as exc:
                registry.load_errors.append(f"{descriptor.plugin_id}工具展示定义：{type(exc).__name__}：{exc}")
        if descriptor.tool_ui_entry is not None:
            try:
                module_name, function_name = descriptor.tool_ui_entry.rsplit(":", 1)
                create_presenters = getattr(import_module(module_name), function_name)
                registry.register(create_presenters())
            except Exception as exc:
                registry.load_errors.append(f"{descriptor.plugin_id}工具详情：{type(exc).__name__}：{exc}")
    return registry


def install_plugin_presenters(catalog, enabled_plugins):
    """GUI启动时补登记外部展示；流式更新复用已创建的展示表。"""
    registry = default_presenters()
    for descriptor in catalog.descriptors:
        if descriptor.host is None or descriptor.plugin_id not in enabled_plugins:
            continue
        try:
            if descriptor.create_tools is not None:
                registry.register_definitions(tuple(descriptor.create_tools()))
            if descriptor.tool_ui_entry is not None:
                registry.register(load_entry(descriptor.tool_ui_entry)(descriptor.host))
        except Exception as exc:
            registry.load_errors.append(f"{descriptor.plugin_id}工具展示加载失败（{type(exc).__name__}）。")
