"""技能来源、发现快照和真实输入装配；UI与运行层共用同一入口。"""
import re
from dataclasses import replace
from pathlib import Path
from threading import RLock

from ..llm.messages import ModelMessage, user_message
from ..skills.catalog import SkillScope, discover
from .skill_settings import SkillSettings


INVOCATION = re.compile(r"(?<!\S)/(skill:[^\s/]+|skill\s+[^\s/]+)(?=\s|$)", re.IGNORECASE)


class SkillService:
    def __init__(self, path: Path, *, enabled_plugins=None, roots=None, manual_invocation=True, plugin_catalog=None):
        self.settings = SkillSettings(path)
        self.enabled_plugins = enabled_plugins
        from ..plugin_catalog import load_plugin_catalog
        self.plugin_catalog = plugin_catalog or load_plugin_catalog(path.parent)
        self._fixed_roots = tuple(roots) if roots is not None else None
        self._manual_invocation = manual_invocation
        self._lock = RLock()
        self._cache = {}
        self._revision = 0

    @property
    def revision(self):
        """仅返回来源配置失效编号；GUI检查不访问磁盘。"""
        with self._lock:
            return self._revision

    def invalidate(self):
        with self._lock:
            self._cache.clear()
            self._revision += 1

    def _roots(self, workspace, enabled_plugins=None):
        if self._fixed_roots is not None:
            return self._fixed_roots
        from .plugin_settings import PluginSettings
        selected_plugins = self.enabled_plugins if enabled_plugins is None else enabled_plugins
        options = self.settings.load()
        roots = []
        if options.project_enabled and workspace is not None:
            roots.append(("项目", Path(workspace) / ".agents" / "skills"))
        roots.extend(("自定义", Path(value)) for value in options.custom_roots)
        if options.user_enabled:
            roots.append(("用户", Path.home() / ".agents" / "skills"))
        contributors = tuple(plugin for plugin in self.plugin_catalog.descriptors if plugin.skill_roots)
        values = (PluginSettings(self.settings.path.parent / "plugins.json").values()
                  if contributors and selected_plugins is None else {})
        for plugin in contributors:
            enabled = (plugin.plugin_id in selected_plugins if selected_plugins is not None
                       else values.get(plugin.plugin_id, plugin.default_enabled))
            if enabled:
                roots.extend(("插件:" + plugin.plugin_id, Path(__file__).resolve().parents[1] / value)
                             for value in plugin.skill_roots)
        return tuple(roots)

    def scope(self, workspace=None, *, refresh=False, check_cancelled=None, enabled_plugins=None):
        revision = self.revision
        if check_cancelled is not None:
            check_cancelled()
        key = str(Path(workspace).expanduser().resolve()) if workspace is not None else None
        roots = self._roots(key, enabled_plugins)
        signature = (key, tuple((name, str(path)) for name, path in roots))
        with self._lock:
            previous = self._cache.get(signature)
        if previous is None or refresh:
            entries, issues = discover(roots, check_cancelled=check_cancelled)
            previous = (entries, issues)
            if check_cancelled is not None:
                check_cancelled()
            with self._lock:
                # 只保留各工作区的当前目录，不缓存正文或累积每次来源修改的旧键。
                if revision == self._revision:
                    self._cache = {item: value for item, value in self._cache.items() if item[0] != key}
                    self._cache[signature] = previous
        return SkillScope(*previous, roots=roots)

    def prepare_message(self, text, images=None, *, workspace=None):
        return self.freeze(user_message(text, images), workspace=workspace)

    def freeze(self, message: ModelMessage, *, workspace=None, scope=None):
        """仅处理真实新输入；历史／队列已有快照不会因重开而二次加载。"""
        if not self._manual_invocation or message.skills or message.runtime_context_id or message.role != "user":
            return message
        # 只认输入开头的连续技能标记；引用资料／代码块中的斜杠不会触发读取。
        matches, position = [], 0
        while match := INVOCATION.match(message.text, position):
            matches.append(match)
            position = match.end()
            while position < len(message.text) and message.text[position].isspace():
                position += 1
        if not matches:
            return message
        selected = scope if scope is not None else self.scope(workspace, refresh=True)
        snapshots, seen = [], set()
        for match in matches:
            invocation = match.group(1)
            identifier = "skill:" + invocation[6:] if invocation.casefold().startswith("skill:") else invocation.split(None, 1)[1]
            if identifier.casefold().startswith("skill:"):
                # 唯一名称标记和菜单插入的带来源编号标记使用同一解析入口。
                identifier = identifier if "@" in identifier else identifier[6:]
            snapshot = selected.load(identifier, manual=True)
            if snapshot.skill_id not in seen:
                snapshots.append(snapshot)
                seen.add(snapshot.skill_id)
        return replace(message, skills=snapshots)
