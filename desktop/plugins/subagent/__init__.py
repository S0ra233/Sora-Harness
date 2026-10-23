"""子Agent贡献静态管理工具；实际服务由应用按父运行绑定。"""
from ...runtime.contracts import Plugin


def create_plugin() -> Plugin:
    return Plugin(plugin_id="subagent", actions={})
