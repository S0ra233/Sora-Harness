"""模型试运行的独立界面入口；业务插件加载时不会导入本包。"""
from pathlib import Path

from ....ui.contracts import UiContribution
from .model_trial_page import ModelTrialPage


def create_ui(settings_path: Path) -> tuple[UiContribution, ...]:
    """页面拥有自己的状态和关闭检查，窗口只接收统一的贡献对象。"""
    page = ModelTrialPage(settings_path)
    return (UiContribution("model_trial", page, page.close_block),)
