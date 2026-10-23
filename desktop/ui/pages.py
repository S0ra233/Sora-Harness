"""按目录加载插件界面入口；不导入具体页面类或处理业务状态。"""
from collections.abc import Callable
from functools import partial
from pathlib import Path
from inspect import signature
from dataclasses import replace

from PySide6.QtWidgets import QWidget

from ..plugin_catalog import PLUGINS, load_plugin_catalog, load_entry
from ..application.plugin_settings import PluginSettings
from ..application.tasks import TaskRegistry
from .contracts import UiContribution, UiPageDefinition, UiRegistry, UiServices
from .chat.tools.presenters import default_presenters
from ..runtime.runs import exception_diagnostics


def _load_ui(entry: str, settings_path: Path, services: UiServices | None = None, *, staged=None) -> tuple:
    """界面入口只在 GUI 中调用；入口自己决定创建哪些控件。"""
    create_ui = load_entry(entry)
    contributions = [] if staged is None else staged
    contributions.extend(create_ui(settings_path, services=services) if "services" in signature(create_ui).parameters else create_ui(settings_path))
    return tuple(contributions)


def load_pages(settings_path: Path, *, tasks: TaskRegistry | None = None, services: UiServices | None = None, catalog=None) -> tuple[UiRegistry, list[str]]:
    """启动时组合工作区；某个插件失败时保留此前已登记的界面。"""
    registry = UiRegistry()
    catalog = catalog or load_plugin_catalog(settings_path.parent)
    registry.plugin_defaults = {item.plugin_id: item.default_enabled for item in catalog.descriptors}
    errors = list(default_presenters().load_errors)
    errors.extend(catalog.issues)
    try:
        values = PluginSettings(settings_path.parent / "plugins.json").values()
    except (OSError, ValueError):
        values = {item.plugin_id: False for item in catalog.descriptors}
        errors.append("插件开关读取失败，外部插件未加载；原设置保留。")
    for descriptor in catalog.descriptors:
        if descriptor.ui_entry is None:
            continue
        if descriptor.host is not None and not values.get(descriptor.plugin_id, descriptor.default_enabled):
            continue
        contributions = []
        try:
            _load_ui(descriptor.ui_entry, settings_path,
                     replace(services, plugin_host=descriptor.host) if services else None, staged=contributions)
            if any(isinstance(item, UiPageDefinition)
                   and item.plugin_id != descriptor.plugin_id for item in contributions):
                raise ValueError("页面的plugin_id与所属插件不一致。")
            if tasks is not None:
                for contribution in contributions:
                    if isinstance(contribution, UiContribution) and contribution.bind_tasks is not None:
                        contribution.bind_tasks(tasks)
            registry.register(contributions)
        except Exception as exc:
            registry.retain_failed(contributions)
            # 插件入口是窗口的错误边界；失败的贡献不挂载，其他插件仍可加载。
            errors.append(f"{descriptor.plugin_id}界面加载失败。\n{exception_diagnostics(exc)}")
    return registry, errors


def _legacy_page_factory(entry: str, settings_path: Path) -> QWidget:
    """保留旧工厂接口；新窗口使用完整登记和关闭检查。"""
    page = next((item.widget for item in _load_ui(entry, settings_path)
                 if isinstance(item, UiContribution) and item.slot == "workspace"), None)
    if page is None:
        raise ValueError("此插件没有旧式工作区页面，请使用页面登记入口。")
    return page


PAGE_FACTORIES: dict[str, Callable[[Path], QWidget]] = {
    descriptor.plugin_id: partial(_legacy_page_factory, descriptor.ui_entry)
    for descriptor in PLUGINS if descriptor.ui_entry is not None
}
