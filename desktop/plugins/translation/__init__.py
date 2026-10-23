"""翻译插件的业务入口；调用现有 Core，不依赖桌面控件。"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...runtime.contracts import ExecutionContext, Plugin


@dataclass
class ProjectSnapshot:
    """保存一次只读加载的结果；界面据此展示，不以它直接修改项目文件。"""

    work_dir: Path
    manifest: dict[str, Any]
    entries: list[dict[str, Any]]
    tasks: list[dict[str, Any]]


@dataclass
class PrepareResult:
    """准备报告与可展示的项目分开；未更新时不加载旧项目冒充成功。"""

    report: dict[str, Any]
    project: ProjectSnapshot | None


async def open_project(context: ExecutionContext, payload: dict[str, Any]) -> ProjectSnapshot:
    """在后台动作中加载项目，不使用上下文中的模型调用能力。"""
    work_dir = Path(payload["work_dir"]).expanduser().resolve()
    from game_translation.process_control import process_scope, check_cancelled
    with process_scope(context.cancel_event):
        result = _load_snapshot(work_dir)
        check_cancelled()
        return result


async def detect_game(context: ExecutionContext, payload: dict[str, Any]) -> dict[str, Any]:
    """只返回引擎与判断依据；识别出引擎不代表所有文本均可提取。"""
    from game_translation import detect
    from game_translation.process_control import process_scope, check_cancelled

    game_dir = Path(payload["game_dir"]).expanduser().resolve()
    with process_scope(context.cancel_event):
        result = detect(game_dir)
        check_cancelled()
        return result


async def prepare_project(context: ExecutionContext, payload: dict[str, Any]) -> PrepareResult:
    """沿用 Core 的目录、提取和状态规则，准备完成后再读取展示快照。"""
    from game_translation import prepare
    from game_translation.process_control import process_scope, check_cancelled

    game_dir = Path(payload["game_dir"]).expanduser().resolve()
    work_dir = Path(payload["work_dir"]).expanduser().resolve()
    with process_scope(context.cancel_event):
        check_cancelled()
        report = prepare(game_dir, work_dir)
    if report.get("project_updated") is False:
        return PrepareResult(report, None)
    try:
        project = _load_snapshot(work_dir)
    except (OSError, ValueError) as exc:
        # 写入已经完成，展示失败不能描述为回滚，也不应自动重复准备。
        raise ValueError(
            f"项目已准备到 {work_dir}，但加载展示失败：{exc}。请使用“打开项目”重新读取。"
        ) from exc
    return PrepareResult(report, project)


def _load_snapshot(work_dir: Path) -> ProjectSnapshot:
    """打开与准备共用一次 Core 加载；不额外调用 status 重读项目。"""
    # 平台组装和模型试运行不需要提前加载引擎模块。
    from game_translation import load_project

    manifest, entries, tasks = load_project(work_dir)
    return ProjectSnapshot(work_dir, manifest, entries, tasks)


def create_plugin() -> Plugin:
    """登记业务动作；界面和执行器无需直接调用 Core。"""
    return Plugin(plugin_id="translation", actions={
        "open_project": open_project,
        "detect_game": detect_game,
        "prepare_project": prepare_project,
    })
