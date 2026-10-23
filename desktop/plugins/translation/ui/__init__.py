"""翻译插件的独立界面入口；业务动作不导入界面包。"""
from pathlib import Path

from PySide6.QtWidgets import QVBoxLayout, QWidget

from ....ui.contracts import UiContribution, UiPageDefinition
from .projection import TranslationProjection
from .view_state import TranslationViewStore


class _LegacyTranslationPage(QWidget):
    """旧调用方显式打开时才创建旧页面；启动登记不再构造隐藏的大表格。"""

    def __init__(self, settings_path):
        super().__init__()
        self.setWindowTitle("游戏翻译")
        self._settings_path = settings_path
        self._page = None
        self._tasks = None

    def bind_tasks(self, tasks):
        self._tasks = tasks
        if self._page is not None:
            self._page.bind_tasks(tasks)

    def close_block(self):
        return self._page.close_block() if self._page is not None else None

    def showEvent(self, event):
        if self._page is None:
            from .translation_page import TranslationPage
            self._page = TranslationPage(self._settings_path)
            if self._tasks is not None:
                self._page.bind_tasks(self._tasks)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self._page)
        super().showEvent(event)


def create_ui(settings_path: Path) -> tuple[UiContribution, ...]:
    """斜杠入口使用双栏全文页，旧工作区工厂仍保留按需兼容。"""
    page = _LegacyTranslationPage(settings_path)
    state = TranslationViewStore(settings_path.parent / "plugin-data" / "translation" / "views.json")
    projection = TranslationProjection(state)
    return (UiContribution("translation", page, page.close_block, bind_tasks=page.bind_tasks,
                           decorate_rows=projection.decorate),
            UiPageDefinition("translation", "translation", "翻译内容", ("翻译", "translation"), projection.create))
