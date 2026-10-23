"""翻译插件贡献项目发现和展示引用；不登记翻译、提交或构建工具。"""
from ...llm.messages import ToolSchema
from ...tools.contracts import ToolDefinition, ToolDisplay, ToolError
from .project_access import discover_projects, in_project_thread, select_project


async def _projects(arguments, context):
    if set(arguments) - {"path", "max_depth"}:
        raise ToolError("invalid_arguments", "只接受path和max_depth。")
    depth = arguments.get("max_depth", 2)
    if type(depth) is not int or not 0 <= depth <= 3:
        raise ToolError("invalid_arguments", "max_depth须为0至3的整数。")
    return await in_project_thread(discover_projects, context.workspace, arguments.get("path", "."), depth)


async def _show(arguments, context):
    if set(arguments) != {"work_dir"}:
        raise ToolError("invalid_arguments", "须提供且只提供work_dir。")
    return await in_project_thread(select_project, context.workspace, arguments["work_dir"])


def create_tools():
    """声明保持静态，具体目录与项目状态只放在真实结果中。"""
    return (
        ToolDefinition(ToolSchema("translation_projects",
            ("在当前授权工作区内有界发现并校验有效的游戏翻译项目目录，返回经 Core 验证的项目路径、引擎/项目元数据及可用于打开阅读视图的引用信息。当用户询问当前工作区有哪些翻译项目，或需要在操作前确认有效项目路径时调用"
            "。path为工作区内相对目录，max_depth默认2、最多3；最多128目录／4,096目录项／16候选，scan_complete=false时不能声称全树无项目。此工具不检测游戏、不创建项目。"),
            {"type": "object", "properties": {"path": {"type": "string", "description": "工作区内相对目录，默认.。"},
             "max_depth": {"type": "integer", "minimum": 0, "maximum": 3}}, "additionalProperties": False}), _projects,
            ToolDisplay(category="translation", icon="folder", verb="查找", summary="查找了{n}个翻译项目范围",
                        heading="翻译项目", subject_fields=("path",)),
            default_profiles=("desktop_work",), execution_class="read"),
        ToolDefinition(ToolSchema("show_translation",
            ("通过必填的 work_dir 选定已有游戏翻译项目并同步到平台的 `/翻译` 双栏对照阅读页（左原文、右已保存译文，按说话人格式展示），并在主聊天生成“查看翻译”快捷入口。如果用户已打开阅读页，会平滑切换到指定"
            "项目；Scene由阅读页选择，本工具不接受scene_id；如果未打开，不会强制弹窗打断用户。注意：阅读页读取经Core校验的项目状态，译文为空的有效项目也能展示，模型在聊天中直接输出的文本或未提交的临时 JS"
            "ON 不会自动变成阅读页中的正式译文。"),
            {"type": "object", "properties": {"work_dir": {"type": "string", "description": "相对于当前工作区的翻译工作目录。"}},
             "required": ["work_dir"], "additionalProperties": False}), _show,
            ToolDisplay(category="translation", icon="folder", verb="展示", summary="选择了{n}个翻译项目",
                        heading="查看翻译", subject_fields=("work_dir",)),
            default_profiles=("desktop_work",), execution_class="read"),
    )
