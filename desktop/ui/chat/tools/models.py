"""把实际调用和结果转换为显示内容；阅读状态只保存在界面内存中。"""
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


STATUS_LABELS = {
    "pending": "等待执行", "running": "正在执行", "succeeded": "成功",
    "failed": "失败", "rejected": "未获准执行", "interrupted": "中断，结果未知",
    "not_executed": "未执行",
}


@dataclass(frozen=True)
class ToolView:
    call_id: str
    name: str
    arguments: str
    status: str
    result: str | None = None


@dataclass(frozen=True)
class ToolPresentation:
    title: str
    icon: str
    category: str
    heading: str
    body: str
    footer: str
    annotation: str = ""
    diff_highlight: bool = False
    partial: bool = False
    summary: str | None = None


@dataclass
class ToolItemState:
    expanded: bool = False
    vertical: int = 0
    horizontal: int = 0


@dataclass
class ToolGroupState:
    expanded: bool = False
    scroll: int = 0
    items: dict[str, ToolItemState] = field(default_factory=dict)


def _object(text: str | None) -> dict[str, Any]:
    if text is None:
        return {}
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(value, dict):
        return {}
    if value.get("status") == "ok" and isinstance(value.get("data"), dict):
        return {**value["data"], **{key: value[key] for key in ("receipt_path", "artifacts_path") if key in value}}
    if value.get("status") == "error" and isinstance(value.get("error"), dict):
        return {**value, "details": value["error"].get("details", {})}
    return value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _pretty(text: str) -> str:
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except (ValueError, RecursionError):
        return text


def _title(view: ToolView, verb: str, subject: str) -> str:
    prefix = {
        "running": f"正在{verb}", "succeeded": f"已{verb}", "failed": f"{verb}失败",
        "pending": "等待执行", "rejected": "未获准执行", "not_executed": "未执行",
        "interrupted": "中断，结果未知",
    }.get(view.status, view.status)
    return f"{prefix} {subject}".rstrip()


def _error_summary(error: dict[str, Any]) -> str:
    """短原因放在路径前，省略长路径时仍能看见；完整错误留在详情。"""
    labels = {
        "path_not_allowed": "只接受工作区内相对路径", "outside_workspace": "工作区外路径",
        "path_access_denied": "拒绝访问", "not_found": "路径不存在",
        "not_a_directory": "该路径不是目录", "not_a_file": "该路径不是普通文件",
        "unsupported_encoding": "不是有效UTF-8", "file_changed": "文件版本变化",
        "tool_not_allowed": "本次未开放此工具", "unknown_tool": "工具没有本地实现",
        "invalid_arguments": "参数不符合要求", "environment_missing": "执行程序不可用",
        "search_launch_failed": "搜索启动或管道读取失败", "search_timeout": "搜索超时",
        "tool_timeout": "达到工具时限", "cancelled": "运行已取消",
    }
    details = error.get("details")
    if isinstance(details, dict) and (details.get("errno") == 13 or details.get("winerror") == 5):
        return "拒绝访问"
    code = _text(error.get("code"))
    if code in labels:
        return labels[code]
    text = " ".join(_text(error.get("message")).split()) or code or "工具返回错误"
    return text[:69] + "…" if len(text) > 70 else text


def _diff_annotation(diff: str, truncated: bool) -> str:
    """只数完整diff的变更段，不将文件头或截断片段当成可靠行数。"""
    if truncated:
        return ""
    added = removed = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            added += line.startswith("+")
            removed += line.startswith("-")
    return f"+{added} −{removed}" if in_hunk else ""


def present_tool(view: ToolView) -> ToolPresentation:
    """保留原调用入口；展示登记延迟加载，避免模型类型的循环导入。"""
    from .presenters import default_presenters
    return default_presenters().present(view)


def group_summary(views: tuple[ToolView, ...], presentations: tuple[ToolPresentation, ...]) -> tuple[str, str, bool]:
    for view, display in zip(views, presentations):
        if view.status == "running":
            return display.title, display.icon, True
    counts = Counter((display.category, display.summary) for view, display in zip(views, presentations) if view.status == "succeeded")
    labels = {
        "command": "运行了{n}条命令", "read": "读取了{n}个文件", "edit": "编辑了{n}个文件",
        "create": "创建了{n}个文件", "directory": "列出了{n}个目录", "search": "搜索了{n}次",
        "find": "查找了{n}次", "tool": "调用了{n}个工具",
        "metadata": "检查了{n}个路径", "create_directory": "创建了{n}个目录", "move": "移动了{n}个路径",
    }
    title = "，".join((summary or labels.get(kind, labels["tool"])).format(n=count)
                     for (kind, summary), count in counts.items())
    states = Counter(view.status for view in views)
    if not title:
        title = "工具过程已结束" if any(states[status] for status in ("failed", "rejected", "interrupted")) else "工具未执行"
        if states["pending"]:
            title = "等待执行"
    partials = sum(display.partial for view, display in zip(views, presentations) if view.status == "succeeded")
    if partials or any(states[status] for status in ("failed", "rejected", "interrupted", "not_executed", "pending")):
        categories = [("正常结果", states["succeeded"] - partials), ("部分结果", partials),
                      ("失败", states["failed"]), ("未获准", states["rejected"]),
                      ("中断，结果未知", states["interrupted"]), ("未执行", states["not_executed"]),
                      ("等待执行", states["pending"])]
        title += " · " + " · ".join(f"{label} {number}" for label, number in categories if number)
    return title, presentations[0].icon if presentations else "plugin", False
