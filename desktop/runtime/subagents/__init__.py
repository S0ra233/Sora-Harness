"""独立子运行入口；不加载Qt。"""

# 应用与插件共享管理协议名称，不能为读取名称而加载可选工具实现。
SUBAGENT_TOOLS = frozenset({"subagent", "send_subagent", "list_subagents", "get_subagent_result", "wait_subagents", "cancel_subagent"})
