"""按注册信息查找和执行插件动作，不处理插件业务。"""
from typing import Any
from collections.abc import Callable

from .contracts import ExecutionContext, Plugin
from .runs import exception_diagnostics


class RegistryError(RuntimeError):
    """重复注册或找不到插件、动作时，向入口报告错误。"""


class PluginRegistry:
    def __init__(self):
        self._plugins: dict[str, Plugin] = {}
        self._factories: dict[str, Callable[[], Plugin]] = {}
        self.load_errors: dict[str, str] = {}

    def _check_identifier(self, plugin_id: str) -> None:
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise RegistryError("插件标识须为非空字符串。")
        if plugin_id in self._plugins or plugin_id in self._factories:
            raise RegistryError(f"插件已注册：{plugin_id}")

    @staticmethod
    def _check_plugin(plugin: Plugin) -> None:
        if not isinstance(plugin, Plugin) or not isinstance(plugin.actions, dict) or any(
                not isinstance(name, str) or not name or not callable(handler)
                for name, handler in plugin.actions.items()):
            raise RegistryError("插件工厂须返回Plugin及有效的动作映射。")

    def register_factory(self, plugin_id: str, factory: Callable[[], Plugin]) -> None:
        """只发布入口；第一次动作执行才加载实现，失败归属该插件。"""
        self._check_identifier(plugin_id)
        if not callable(factory):
            raise RegistryError(f"插件 {plugin_id} 的工厂不可调用。")
        self._factories[plugin_id] = factory

    def register(self, plugin: Plugin) -> None:
        """明确登记插件，重复标识报错，避免覆盖已有动作。"""
        self._check_plugin(plugin)
        self._check_identifier(plugin.plugin_id)
        self._plugins[plugin.plugin_id] = plugin

    def _load(self, plugin_id: str) -> Plugin:
        if plugin_id in self._plugins:
            return self._plugins[plugin_id]
        if plugin_id in self.load_errors:
            raise RegistryError(self.load_errors[plugin_id])
        factory = self._factories.get(plugin_id)
        if factory is None:
            raise RegistryError(f"未注册的插件：{plugin_id}")
        try:
            plugin = factory()
            self._check_plugin(plugin)
            if plugin.plugin_id != plugin_id:
                raise RegistryError("插件工厂返回的标识与登记入口不一致。")
        except Exception as exc:
            # 不回显工厂的任意异常正文；保留类型与代码位置供定位。
            detail = f"插件 {plugin_id} 加载失败（{type(exc).__name__}）。\n{exception_diagnostics(exc)}"
            self.load_errors[plugin_id] = detail
            raise RegistryError(detail) from exc
        self._plugins[plugin_id] = plugin
        return plugin

    async def run_action(
        self,
        plugin_id: str,
        action: str,
        payload: dict[str, Any],
        context: ExecutionContext,
    ) -> Any:
        """找到处理函数，传入平台能力和业务输入，再返回执行结果。"""
        plugin = self._load(plugin_id)

        try:
            handler = plugin.actions[action]
        except KeyError as exc:
            raise RegistryError(f"插件 {plugin_id} 没有动作：{action}") from exc

        # 不在这里判断是聊天、翻译还是其他业务，也不限制动作的结果类型。
        return await handler(context, payload)
