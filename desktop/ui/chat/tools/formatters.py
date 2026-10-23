"""详情格式器只处理已返回的数据，不执行工具或创建Qt控件。"""
import math
from dataclasses import dataclass
from typing import Any

from ....tools.contracts import ToolDisplay
from .models import ToolView, _diff_annotation, _pretty, _text


@dataclass(frozen=True)
class ToolDetailInput:
    view: ToolView
    display: ToolDisplay
    arguments: dict[str, Any]
    data: dict[str, Any]
    subject: str
    error_text: str


@dataclass(frozen=True)
class ToolDetail:
    body: str
    footer: str = ""
    annotation: str = ""
    diff_highlight: bool = False

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) for value in (self.body, self.footer, self.annotation)) or type(self.diff_highlight) is not bool:
            raise ValueError("工具详情须提供字符串正文／附注和布尔差异高亮。")


def format_json(context: ToolDetailInput) -> ToolDetail:
    view = context.view
    body = "参数\n" + _pretty(view.arguments)
    body += "\n\n结果\n" + (_pretty(view.result) if view.result is not None else "尚无返回结果。")
    return ToolDetail(body)


def format_command(context: ToolDetailInput) -> ToolDetail:
    view, data, args = context.view, context.data, context.arguments
    prompt = "PS>" if data.get("shell") == "powershell" else "$"
    body = f"{prompt} {_text(args.get('command')) or view.arguments}"
    footer = ""
    output = _text(data.get("output"))
    if output:
        body += "\n\n" + output
    elif view.result is None:
        body += "\n\n" + ("输出将在命令结束后显示。" if view.status == "running" else "尚无返回结果。")
    code, duration = data.get("exit_code"), data.get("duration_seconds")
    if type(code) is int:
        footer += f" · 退出码 {code}"
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration):
        footer += f" · {duration:.1f}秒"
    if data.get("truncated") is True:
        mode = data.get("output_summary", {}).get("mode") if isinstance(data.get("output_summary"), dict) else None
        footer += " · 输出已截断，当前显示首尾部分" if mode == "compact" else " · 输出已截断，当前显示末尾部分"
    if data.get("compacted") is True:
        footer += " · 连续重复行已折叠，完整输出保留在记录中"
    if view.result is not None and not isinstance(data.get("output"), str) and not context.error_text:
        body += "\n\n" + _pretty(view.result)
    return ToolDetail(body, footer)


def format_diff(context: ToolDetailInput) -> ToolDetail:
    view, data = context.view, context.data
    diff = _text(data.get("diff"))
    body = context.subject + "\n\n" + (diff or (
        "文件内容没有变化。" if data.get("status") == "unchanged" else "尚无文件差异。"))
    footer, annotation = "", ""
    stats = data.get("change_stats")
    if isinstance(stats, dict):
        if stats.get("complete") is True and all(type(stats.get(key)) is int and stats[key] >= 0
                                                for key in ("added_lines", "removed_lines")):
            annotation = f"+{stats['added_lines']} −{stats['removed_lines']}"
        elif stats.get("complete") is False:
            footer += " · 改动行数未知"
    else:
        annotation = _diff_annotation(diff, data.get("diff_truncated") is True)
    if data.get("diff_truncated") is True:
        footer += " · 差异已截断"
    if view.result is not None and not isinstance(data.get("diff"), str) and data.get("status") != "unchanged" and not context.error_text:
        body += "\n\n" + _pretty(view.result)
    return ToolDetail(body, footer, annotation, data.get("diff_format") != "replacement_preview")


def format_read(context: ToolDetailInput) -> ToolDetail:
    data = context.data
    if isinstance(data.get("entries"), list) and "requested_files" in data:
        parts = []
        for entry in data["entries"]:
            if entry.get("status") == "ok":
                value = entry["data"]
                location = f" · 第{value['start_line']}～{value['end_line']}行" if value.get("start_line") is not None else ""
                parts.append(f"{value['path']}{location}\n{value['content']}")
            else:
                parts.append(f"{entry['path']}\n读取失败：{entry['error']['message']}")
        footer = f" · 已处理{data['processed_files']}/{data['requested_files']}个区间"
        if data.get("next_files"):
            footer += f" · {len(data['next_files'])}个区间可续读"
        if data.get("failed_files"):
            footer += f" · {data['failed_files']}个失败"
        return ToolDetail("\n\n".join(parts), footer)
    if not isinstance(data.get("content"), str):
        return format_json(context)
    footer = ""
    if data.get("next_line") is not None:
        footer += f" · 后续内容从第 {data['next_line']} 行开始"
    elif data.get("next_cursor") is not None:
        footer += " · 可用字节游标继续读取"
    elif data.get("next_offset") is not None:
        footer += f" · 后续内容从字节 {data['next_offset']} 继续"
    return ToolDetail(context.subject + "\n\n" + data["content"], footer)


DETAIL_FORMATTERS = {"json": format_json, "command": format_command, "diff": format_diff, "read": format_read}
