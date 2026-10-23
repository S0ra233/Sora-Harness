"""子Agent配置与只读展开页；宿主提供数据接口和公共容器。"""
from ....ui.contracts import UiContribution, UiPageDefinition
from .settings import SubagentSettings
from .page import SubagentPage
from .projection import decorate


def create_ui(settings_path, *, services=None):
    settings = SubagentSettings(settings_path.parent)
    contributions = [UiContribution("subagent", settings, settings.close_block, slot="model_settings",
        decorate_rows=(lambda rows, context: decorate(rows, context, services.subagents))
            if services is not None and services.subagents is not None else None)]
    if services is not None and services.subagents is not None:
        def create(context):
            page = SubagentPage(services.subagents, settings_path.parent, context)
            return UiContribution("subagent", page, page.close_block, bind_context=page.bind_context)
        contributions.append(UiPageDefinition("subagent", "subagent", "子智能体", ("子智能体", "subagent"), create,
            presentation="aside", scope="conversation", requires_conversation=True,
            available_when_disabled=lambda context: bool(services.subagents.list(context.conversation_id))))
    return tuple(contributions)
