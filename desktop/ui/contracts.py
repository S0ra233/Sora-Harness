"""插件界面与窗口的约定；登记发生在启动时，不处理运行中热替换。"""
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import logging

from PySide6.QtWidgets import QWidget
from shiboken6 import isValid
from ..application.tasks import TaskRegistry


@dataclass(frozen=True)
class CloseBlock:
    """插件说明现在为何不能关闭，窗口只负责展示。"""

    title: str
    message: str


CloseCheck = Callable[[], CloseBlock | None]


@dataclass(frozen=True)
class UiPageContext:
    conversation_id: str | None = None
    project_id: str | None = None
    workspace: str | None = None
    mode: str = "dialogue"
    resource_id: str | None = None


@dataclass(frozen=True)
class UiServices:
    tasks: TaskRegistry
    subagents: object = None
    plugin_host: object = None


@dataclass(frozen=True)
class UiPageDefinition:
    """插件给创建函数，宿主决定如何挂载；页面本身不取得模型发送入口。"""
    page_id: str
    plugin_id: str
    title: str
    aliases: tuple[str, ...]
    create_page: Callable[[UiPageContext], "UiContribution"]
    presentation: str = "window"
    scope: str = "application"
    requires_conversation: bool = False
    available_when_disabled: Callable[[UiPageContext], bool] | None = None


@dataclass(frozen=True)
class UiContribution:
    """插件创建自己的控件，按稳定标识提供给窗口公开的位置。"""

    contribution_id: str
    widget: QWidget
    close_check: CloseCheck | None = None
    slot: str = "workspace"
    # 替换必须明确声明，并且目标必须已登记；避免拼错标识后意外添加。
    replace: bool = False
    # 宿主提供同一运行登记表；插件不能另建目录占用规则。
    bind_tasks: Callable[[TaskRegistry], None] | None = None
    bind_context: Callable[[UiPageContext], None] | None = None
    dispose: Callable[[], None] | None = None
    invalidate: Callable[[str], None] | None = None
    decorate_rows: Callable[[tuple, UiPageContext], tuple] | None = None


def release_idle_contribution(contribution: UiContribution) -> bool:
    """只释放可关闭的失败贡献；活动任务或关闭检查失败时由宿主继续保管。"""
    try:
        if contribution.close_check and contribution.close_check():
            return False
        if contribution.dispose:
            contribution.dispose()
        if isinstance(contribution.widget, QWidget) and isValid(contribution.widget):
            contribution.widget.deleteLater()
        return True
    except Exception as exc:
        logging.getLogger(__name__).error("插件贡献释放失败：贡献=%s 错误类型=%s", contribution.contribution_id, type(exc).__name__)
        return False


class UiRegistry:
    """组合已创建的界面；一次插件登记失败不改变此前登记的内容。"""

    def __init__(self, slots: Iterable[str] = ("workspace", "model_settings")) -> None:
        self._slots = frozenset(slots)
        self._entries: dict[tuple[str, str], UiContribution] = {}
        self._pages: dict[str, UiPageDefinition] = {}
        self._failed: list[UiContribution] = []
        self._disposed = False
        self.plugin_defaults: dict[str, bool] = {}

    def register(self, contributions: Iterable[UiContribution]) -> None:
        """先检查一批贡献，再整体接收；替换沿用原来的排列位置。"""
        pending = dict(self._entries)
        pages = dict(self._pages)
        for contribution in contributions:
            if isinstance(contribution, UiPageDefinition):
                if contribution.presentation not in {"window", "aside"} or contribution.scope not in {"application", "conversation"}:
                    raise ValueError("插件页面承载方式或作用域不正确。")
                if contribution.page_id in pages or not contribution.aliases:
                    raise ValueError("插件页面编号重复或没有命令入口。")
                aliases = {alias.casefold() for page in pages.values() for alias in page.aliases}
                if any(not alias or "/" in alias or any(char.isspace() for char in alias) or alias.casefold() in aliases for alias in contribution.aliases):
                    raise ValueError("插件命令为空、含空格或与既有别名冲突。")
                pages[contribution.page_id] = contribution
                continue
            if (not isinstance(contribution, UiContribution) or not isinstance(contribution.widget, QWidget)
                    or not isValid(contribution.widget)):
                raise ValueError("界面工厂须提供UiContribution和QWidget，或UiPageDefinition。")
            if contribution.slot not in self._slots:
                raise ValueError(f"窗口没有声明界面位置：{contribution.slot}")
            key = (contribution.slot, contribution.contribution_id)
            if contribution.replace:
                if key not in pending:
                    raise ValueError(f"替换目标尚未登记：{key}")
            elif key in pending:
                raise ValueError(f"界面标识重复，请明确声明替换：{key}")
            pending[key] = contribution
        self._entries = pending
        self._pages = pages

    def pages(self) -> tuple[UiPageDefinition, ...]:
        return tuple(self._pages.values())

    def all_contributions(self) -> tuple[UiContribution, ...]:
        return (*self._entries.values(), *self._failed)

    def retain_failed(self, contributions) -> None:
        """登记失败不挂载页面，但不能丢失已经启动任务的所有者。"""
        registered = {id(value) for value in self._entries.values()}
        widgets = {id(value.widget) for value in self._entries.values()}
        for contribution in contributions:
            if isinstance(contribution, UiContribution) and id(contribution) not in registered and id(contribution.widget) not in widgets:
                if not release_idle_contribution(contribution):
                    self._failed.append(contribution)

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        for contribution in self.all_contributions():
            if contribution.dispose:
                try:
                    contribution.dispose()
                except Exception as exc:
                    logging.getLogger(__name__).error("插件贡献退出失败：贡献=%s 错误类型=%s", contribution.contribution_id, type(exc).__name__)

    def contributions(self, slot: str) -> tuple[UiContribution, ...]:
        """返回该位置的有序贡献；窗口保留控件，切换时不重新创建。"""
        return tuple(value for (position, _), value in self._entries.items() if position == slot)
