"""显式登记业务工厂和可选界面入口；本模块不加载 Qt 界面。"""
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from importlib import import_module
import hashlib
import json
from pathlib import Path
import re
import sys
from types import ModuleType

from .plugin_api.core import PluginHost
from .plugin_api import API_VERSION

from .runtime.contracts import Plugin
from .tools.contracts import ToolDefinition


def _create(entry: str):
    """在所属登记／执行错误边界内导入，目录本身不加载业务模块。"""
    module, name = entry.rsplit(":", 1)
    return getattr(import_module(module), name)()


@dataclass(frozen=True)
class PluginDescriptor:
    """目录保存元数据和延迟工厂；界面由 GUI 按自己的入口加载。"""

    plugin_id: str
    create_plugin: Callable[[], Plugin] | None = None
    ui_entry: str | None = None
    create_tools: Callable[[], Iterable[ToolDefinition]] | None = None
    # 只由GUI加载；返回(name, formatter)条目，不把Qt展示导入执行层。
    tool_ui_entry: str | None = None
    label: str = ""
    description: str = ""
    default_enabled: bool = True
    # 可选贡献真实技能目录；平台只发现SKILL.md，不执行其中的文件。
    skill_roots: tuple[str, ...] = ()
    host: PluginHost | None = None
    manifest_json: str | None = None
    entry_digests: tuple[tuple[str, str], ...] = ()
    version: str = "未标注"
    api_version: int = 1


PLUGINS = (
    PluginDescriptor(
        "model_trial", partial(_create, "desktop.plugins.model_trial:create_plugin"),
        "desktop.plugins.model_trial.ui:create_ui",
        label="模型试运行",
    ),
    PluginDescriptor(
        "translation", partial(_create, "desktop.plugins.translation:create_plugin"),
        "desktop.plugins.translation.ui:create_ui",
        create_tools=partial(_create, "desktop.plugins.translation.tools:create_tools"),
        label="游戏翻译",
        description="原文／译文对照、工作项目选择与按需翻译技能；请求仍在主聊天进行。",
        skill_roots=("../skills/game-translation",),
    ),
    PluginDescriptor("subagent", partial(_create, "desktop.plugins.subagent:create_plugin"), "desktop.plugins.subagent.ui:create_ui",
                     create_tools=partial(_create, "desktop.plugins.subagent.tools:create_tools"),
                     label="子智能体", description="后台协作任务与独立模型配置。", default_enabled=False),
    PluginDescriptor("file_compare", ui_entry="desktop.plugins.file_compare.ui:create_ui", label="文件比对", description="只读文本和差异文件查看。"),
    PluginDescriptor("diagnostics", ui_entry="desktop.plugins.diagnostics.ui:create_ui", label="运行诊断", description="查看请求、工具、摘要与用量，不发送模型请求。"),
)


@dataclass(frozen=True)
class PluginCatalog:
    """一次启动的目录快照，运行、技能、GUI和子进程复用同一组元数据。"""

    descriptors: tuple[PluginDescriptor, ...]
    issues: tuple[str, ...] = ()

    def snapshot(self) -> tuple[dict, ...]:
        return tuple({"directory": str(item.host.code_directory), "manifest": item.manifest_json,
                      "entry_digests": item.entry_digests}
                     for item in self.descriptors if item.host is not None)


def load_entry(entry: str):
    """只在贡献实际加载时导入，不在扫描清单时执行插件Python。"""
    module, function = entry.rsplit(":", 1)
    value = getattr(import_module(module), function)
    if not callable(value):
        raise ValueError("插件入口须为函数。")
    return value


def _external_factory(entry: str, host: PluginHost):
    return load_entry(entry)(host)


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _descriptor(directory: Path, raw: str, data_root: Path) -> PluginDescriptor:
    """校验真实路径和版本，再建立独立模块命名空间，避免不同插件互相覆盖。"""
    data = json.loads(raw)
    if not isinstance(data, dict) or type(data.get("api_version")) is not int or not 1 <= data["api_version"] <= API_VERSION:
        raise ValueError("插件api_version须为1或2；当前宿主支持到v2。")
    identifier = data.get("id")
    if not isinstance(identifier, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identifier) is None:
        raise ValueError("插件id须为小写字母开头的稳定标识。")
    for name in ("name", "description"):
        if not isinstance(data.get(name, ""), str):
            raise ValueError(f"插件{name}须为文字。")
    if type(data.get("default_enabled", False)) is not bool:
        raise ValueError("插件默认开关须为布尔值。")
    version = data.get("version", "未标注")
    if not isinstance(version, str) or not version or len(version) > 80:
        raise ValueError("插件version须为不超过80字的文字。")
    required = data.get("requires_host_api", data["api_version"])
    if type(required) is not int or not 1 <= required <= API_VERSION or required < data["api_version"]:
        raise ValueError("此插件要求不兼容的宿主接口版本。")
    namespace = "desktop_extension_" + hashlib.sha256(str(directory).encode()).hexdigest()[:20]
    entries, digests = {}, {}
    for key in ("business_entry", "tools_entry", "ui_entry", "tool_ui_entry"):
        value = data.get(key)
        if value is None:
            entries[key] = None
            continue
        if not isinstance(value, str) or value.count(":") != 1:
            raise ValueError(f"{key}须为文件.py:函数名。")
        relative, function = value.split(":")
        path = (directory / relative).resolve(strict=True)
        if not path.is_relative_to(directory) or path.suffix != ".py" or not path.is_file():
            raise ValueError("插件入口必须是插件目录内的Python文件。")
        parts = path.relative_to(directory).with_suffix("").parts
        if not function.isidentifier() or any(not part.isidentifier() for part in parts):
            raise ValueError("插件模块和函数名称不正确。")
        entries[key] = namespace + "." + ".".join(parts) + ":" + function
        digests[str(path)] = _file_digest(path)
    values = data.get("skill_roots", [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("skill_roots须为目录列表。")
    roots = tuple((directory / value).resolve() for value in values)
    if any(not root.is_relative_to(directory) for root in roots):
        raise ValueError("插件技能目录不得越出插件目录。")
    if not any(entries.values()) and not roots:
        raise ValueError("插件没有贡献入口或技能目录。")
    # 不执行__init__.py；插件使用相对导入自己的模块，依赖自行安装。
    if namespace not in sys.modules:
        module = ModuleType(namespace)
        module.__path__ = [str(directory)]
        module.__package__ = namespace
        sys.modules[namespace] = module
    host = PluginHost(identifier, directory, data_root / "plugin-data" / identifier)
    return PluginDescriptor(identifier,
        partial(_external_factory, entries["business_entry"], host) if entries["business_entry"] else None,
        entries["ui_entry"],
        partial(_external_factory, entries["tools_entry"], host) if entries["tools_entry"] else None,
        entries["tool_ui_entry"], data.get("name") or identifier, data.get("description", ""),
        data.get("default_enabled", False), tuple(map(str, roots)), host, raw, tuple(sorted(digests.items())), version, data["api_version"])


def load_plugin_catalog(data_root: Path | None = None, *, snapshot=None) -> PluginCatalog:
    """本地清单损坏只隔离该插件；子任务可传父运行的清单快照。"""
    from .storage.paths import LocalPaths
    root = (data_root or LocalPaths.default().root).expanduser().resolve()
    descriptors, issues = list(PLUGINS), []
    identifiers = {item.plugin_id for item in descriptors}
    if snapshot is None:
        extension_root = root / "extensions"
        try:
            from .application.extension_packages import managed_versions
            managed = managed_versions(root, issues=issues)
            sources = [(path.parent, None, None) for path in sorted(extension_root.glob("*/plugin.json"))]
            sources.extend((directory, None, None) for directory in managed.values() if directory is not None)
        except (OSError, ValueError) as exc:
            return PluginCatalog(tuple(descriptors), (f"扩展目录不可读（{type(exc).__name__}）：{exc}",))
    else:
        sources = [(Path(item["directory"]), item["manifest"], item.get("entry_digests")) for item in snapshot]
    for directory, saved, saved_digests in sources:
        try:
            directory = directory.resolve(strict=True)
            manifest = directory / "plugin.json"
            if manifest.stat().st_size > 128 * 1024:
                raise ValueError("插件清单超过128KiB。")
            raw = manifest.read_text(encoding="utf-8-sig")
            if saved is not None and raw != saved:
                raise ValueError("插件清单已改变，请由父任务重新启动子任务。")
            item = _descriptor(directory, raw, root)
            if snapshot is None:
                owner = next((key for key, value in managed.items() if value == directory), None)
                if owner is not None and owner != item.plugin_id:
                    raise ValueError("托管插件清单ID与发布目录记录不一致。")
            if snapshot is None and item.plugin_id in managed and directory != managed[item.plugin_id]:
                continue
            if saved_digests is not None and item.entry_digests != tuple(tuple(value) for value in saved_digests):
                raise ValueError("插件入口代码已改变，请由父任务重新启动子任务。")
            if item.plugin_id in identifiers:
                raise ValueError("插件id与已有条目重复。")
            identifiers.add(item.plugin_id)
            descriptors.append(item)
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            issues.append(f"插件 {directory.name} 未加载（{type(exc).__name__}）：{exc}")
    return PluginCatalog(tuple(descriptors), tuple(issues))
