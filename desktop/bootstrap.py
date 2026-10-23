"""集中组装平台，避免不同入口重复创建后端和登记插件。"""
from functools import partial
from pathlib import Path
from .attachments.store import ImageStore

from .plugin_catalog import load_plugin_catalog
from .llm.contracts import TextSink
from .llm.models import ModelConfig, ModelRequest, ModelResponse
from .llm.validation import resolve_model_config
from .llm.providers.factory import create_provider, open_model_session
from .runtime.application import Runtime
from .runtime.contracts import ExecutionContext
from .runtime.registry import PluginRegistry
from .tools.catalog import create_tool_registry


def create_runtime(
    config: ModelConfig | None = None, api_key: str = "", *, attachments: ImageStore | None = None,
    artifacts_dir: Path | None = None,
    enabled_plugins: frozenset[str] | None = None,
    skills=None,
    excluded_env: frozenset[str] = frozenset(),
    plugin_catalog=None,
    mcp_servers=None,
    registration_issues=(),
) -> Runtime:
    """配置模型时绑定后端；检测、准备和浏览项目可以省略模型配置。"""
    if config is None:
        context = ExecutionContext(
            complete=_model_not_configured, stream_complete=_stream_not_configured,
        )
    else:
        config = resolve_model_config(config)
        provider = create_provider(config, api_key, attachments=attachments)
        # 绑定方法保留后端配置和密钥；组装本身不发送请求。
        context = ExecutionContext(
            complete=provider.complete, stream_complete=provider.stream_complete,
        )
    registry = PluginRegistry()
    catalog = plugin_catalog or load_plugin_catalog()
    from .storage.paths import LocalPaths
    if mcp_servers is None:
        from .mcp.settings import McpSettings
        try:
            mcp_servers = McpSettings(LocalPaths.default().mcp).load()
        except (OSError, ValueError):
            mcp_servers = ()
            registration_issues = (*registration_issues, "MCP设置不可读，本轮未开放MCP。")
    if enabled_plugins is None:
        from .application.plugin_settings import PluginSettings
        switches = PluginSettings(LocalPaths.default().root / "plugins.json")
        try:
            values = switches.values()
        except (OSError, ValueError):
            values = {item.plugin_id: False for item in catalog.descriptors}
            registration_issues = (*registration_issues, "插件开关不可读，此运行未开放插件。")
        enabled_plugins = frozenset(item.plugin_id for item in catalog.descriptors if values.get(item.plugin_id, item.default_enabled))
    for descriptor in catalog.descriptors:
        if descriptor.create_plugin is not None:
            registry.register_factory(descriptor.plugin_id, descriptor.create_plugin)
    tools = create_tool_registry(catalog, enabled_plugins=enabled_plugins)
    from .mcp.tools import register_mcp_tools
    register_mcp_tools(tools, mcp_servers)
    tools.registration_errors.extend(registration_issues)
    if skills is None:
        from .application.skills import SkillService
        skills = SkillService(LocalPaths.default().skills, enabled_plugins=enabled_plugins, plugin_catalog=catalog)
    return Runtime(
        registry, context, tools=tools,
        artifacts_dir=artifacts_dir,
        model_session=partial(open_model_session, config, api_key, attachments=attachments) if config is not None else None,
        excluded_env=excluded_env | (frozenset({"MODEL_API_KEY", config.key_env}) if config is not None else frozenset({"MODEL_API_KEY"})),
        context_window=config.context_window if config is not None else None,
        enabled_plugins=enabled_plugins,
        skills=skills,
        mcp_servers=mcp_servers,
        # 插件以default_profiles显式贡献工作默认项；子Agent管理工具没有该标记。
        extension_plugin_ids=frozenset(item.plugin_id for item in catalog.descriptors
                                      if item.create_tools is not None and item.plugin_id in enabled_plugins),
    )


async def _model_not_configured(request: ModelRequest) -> ModelResponse:
    """不调用模型的动作可先使用平台；真正请求模型时再报告缺少配置。"""
    raise ValueError("尚未配置模型调用，请先保存模型配置并设置对应的密钥环境变量。")


async def _stream_not_configured(request: ModelRequest, on_text: TextSink) -> ModelResponse:
    """未配置模型时，两种读取方式使用同一条说明。"""
    return await _model_not_configured(request)
