"""集中内置工厂与插件贡献；登记不等于向已有会话授权。"""
from collections.abc import Callable
from dataclasses import replace

from ..plugin_catalog import PluginCatalog, load_plugin_catalog
from .contracts import ToolDefinition
from .registry import ToolRegistry
from ..runtime.runs import exception_diagnostics
from .command.tool import create_command_tool
from .records import create_record_tool
from .questions import create_question_tool
from .filesystem.directories import create_list_directory_tool
from .filesystem.edit import create_edit_tool, create_file_tool
from .filesystem.find import create_find_files_tool
from .filesystem.metadata import create_read_bytes_tool, create_stat_tool
from .filesystem.operations import create_directory_tool, create_move_tool
from .filesystem.read import create_read_text_tool
from .filesystem.search import create_search_text_tool
from ..skills.tools import create_load_skill_tool, create_read_skill_resource_tool


BUILTIN_FACTORIES: tuple[Callable[[], ToolDefinition], ...] = (
    create_read_text_tool, create_list_directory_tool, create_search_text_tool,
    create_find_files_tool, create_stat_tool, create_read_bytes_tool,
    create_file_tool, create_edit_tool, create_directory_tool, create_move_tool,
    create_record_tool, create_command_tool,
    create_load_skill_tool, create_read_skill_resource_tool,
    create_question_tool,
)


def create_tool_registry(catalog: PluginCatalog | None = None, *, enabled_plugins=None) -> ToolRegistry:
    """每个Runtime独立登记；插件缺失不影响其他能力，重名仍明确报错。"""
    registry = ToolRegistry()
    registry.register_many(builtin_tool_definitions())
    catalog = catalog or load_plugin_catalog()
    registry.registration_errors.extend(catalog.issues)
    for descriptor in catalog.descriptors:
        if descriptor.host is not None and (enabled_plugins is None or descriptor.plugin_id not in enabled_plugins):
            continue
        if descriptor.create_tools is None:
            continue
        try:
            definitions = tuple(descriptor.create_tools())
            # 工厂与整批登记共用错误边界，冲突不会中断其他插件或内置工具。
            registry.register_many(definitions, source=descriptor.plugin_id)
        except Exception as exc:
            registry.registration_errors.append(f"{descriptor.plugin_id}工具登记失败。\n{exception_diagnostics(exc)}")
            continue
    return registry


def builtin_tool_definitions() -> tuple[ToolDefinition, ...]:
    """工厂只创建定义；每次返回独立Schema，不共享可修改的参数字典。"""
    readers = {"read_text_file", "list_directory", "search_text", "find_files", "stat_file", "read_bytes", "read_tool_record"}
    return tuple(replace(value, execution_class="read") if value.schema.name in readers else value
                 for value in (create() for create in BUILTIN_FACTORIES))


def default_tool_names(profile: str, catalog=None, *, enabled_plugins=None) -> tuple[str, ...]:
    """新建／显式授权时选目录默认项；已有会话还须受原配置限制。"""
    catalog = catalog or load_plugin_catalog()
    if enabled_plugins is None:
        from ..application.plugin_settings import PluginSettings
        from ..storage.paths import LocalPaths
        values = PluginSettings(LocalPaths.default().root / "plugins.json").values()
        enabled_plugins = frozenset(item.plugin_id for item in catalog.descriptors if values.get(item.plugin_id, item.default_enabled))
    # 可选外部工具由启用开关在运行时加入，不写进新会话永久默认权限。
    builtin_catalog = PluginCatalog(tuple(item for item in catalog.descriptors if item.host is None), catalog.issues)
    return tuple(definition.schema.name for definition in create_tool_registry(builtin_catalog, enabled_plugins=enabled_plugins).definitions()
                 if profile in definition.default_profiles)


# 旧控制器入口保留内置默认项，不在模块导入时运行可选插件工厂。
DEFAULT_WORK_TOOLS = tuple(definition.schema.name for definition in builtin_tool_definitions()
                           if "desktop_work" in definition.default_profiles)
