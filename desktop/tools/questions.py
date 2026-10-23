"""非阻塞提问只发布问题；回答通过真实用户消息进入下一次请求。"""
from dataclasses import asdict, dataclass
import json
import re

from ..llm.messages import ToolResult, ToolSchema
from .contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError

QUESTION_TOOL = "request_user_input"
QUESTION_POLICIES = {
    "low": "只询问会实质改变结果、范围或必要决定的缺失信息；先查已有上下文，常规细节自行判断。",
    "medium": "除必要信息外，询问重要的方案、风格和取舍；合并相关问题，给出明确建议。",
    "high": "适合共同设计：逐步追问关键假设、边界和依赖，先用资料排除已知答案；每次围绕少量重要决定，不反复追问已确认内容。",
}


@dataclass(frozen=True)
class QuestionOption:
    label: str
    description: str = ""


@dataclass(frozen=True)
class UserQuestion:
    id: str
    question: str
    options: tuple[QuestionOption, ...] = ()


def parse_questions(value) -> tuple[UserQuestion, ...]:
    """工具调用和本地恢复共用边界，不把问题正文当成程序命令。"""
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise ValueError("每次请提供1至3个问题。")
    questions = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"id", "question", "options"}:
            raise ValueError("问题只接受id、question和options。")
        identifier, text, options = item.get("id"), item.get("question"), item.get("options", [])
        if not isinstance(identifier, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier) is None:
            raise ValueError("问题id须为1至64位字母、数字、下划线或连字符。")
        if not isinstance(text, str) or not text.strip() or len(text) > 1600:
            raise ValueError("问题正文须为非空文本，最多1600字符。")
        if not isinstance(options, (list, tuple)) or options and not 2 <= len(options) <= 3:
            raise ValueError("选项可留空；提供时须为2至3项。")
        parsed = []
        for option in options:
            if not isinstance(option, dict) or set(option) - {"label", "description"}:
                raise ValueError("选项只接受label和description。")
            label, description = option.get("label"), option.get("description", "")
            if not isinstance(label, str) or not label.strip() or len(label) > 240:
                raise ValueError("选项名称须为非空文本，最多240字符。")
            if not isinstance(description, str) or len(description) > 800:
                raise ValueError("选项说明最多800字符。")
            parsed.append(QuestionOption(label, description))
        if len({option.label for option in parsed}) != len(parsed):
            raise ValueError("同一问题的选项不得重名。")
        questions.append(UserQuestion(identifier, text, tuple(parsed)))
    if len({question.id for question in questions}) != len(questions):
        raise ValueError("同一次提问的问题id不得重复。")
    return tuple(questions)


def questions_from_result(result: ToolResult | None) -> tuple[UserQuestion, ...]:
    """只从成功执行的真实结果恢复；未执行或失败的调用不产生问题卡片。"""
    if result is None or result.name != QUESTION_TOOL or result.is_error:
        return ()
    try:
        envelope = json.loads(result.content)
        if envelope.get("status") != "ok":
            return ()
        return parse_questions(envelope["data"]["questions"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return ()


async def _ask(arguments: dict, context: ToolContext) -> dict:
    if context.question_frequency not in QUESTION_POLICIES:
        raise ToolError("interaction_unavailable", "当前运行没有用户提问界面；请在答复中说明需要的决定。")
    try:
        if set(arguments) != {"questions"}:
            raise ValueError("提问参数只接受questions。")
        questions = parse_questions(arguments["questions"])
    except ValueError as exc:
        raise ToolError("invalid_arguments", str(exc)) from exc
    return {"call_id": context.call_id, "questions": [asdict(question) for question in questions],
            "answer_status": "pending", "blocking": False,
            "next_step": "问题已展示，尚无用户回答。继续不依赖答案的工作；相关决定暂缓。回答会以用户消息加入后续请求，不要重复提问或把推荐项当作已获同意。"}


def create_question_tool() -> ToolDefinition:
    option = {"type": "object", "properties": {"label": {"type": "string"},
        "description": {"type": "string"}}, "required": ["label"], "additionalProperties": False}
    question = {"type": "object", "properties": {"id": {"type": "string"},
        "question": {"type": "string"}, "options": {"type": "array", "maxItems": 3, "items": option}},
        "required": ["id", "question"], "additionalProperties": False}
    return ToolDefinition(ToolSchema(QUESTION_TOOL,
        ("向用户发起非阻塞的结构化提问（每次 1～3 个问题，每题可提供 2～3 个明确候选项，用户也可自由输入文字或跳过）。注意本工具的运行机制：\n- 调用后工具会立即将问题卡片停靠在用户输入框上方，并立刻返回 `an"
            "swer_status=pending`（代表提问已送达，而非用户已回答）；\n- 先查已有信息，在需要用户决定或缺失信息会实质影响结果时使用，并遵守运行上下文的 question_policy 提问频率偏好；\n"
            "- 发出提问后，你可以继续完成当前不依赖该答案的独立准备工作，随后结束本轮回复，保留仍需决定的事项；回答会作为用户消息加入后续请求。推荐选项不代表用户已同意，不连续重复尚未回答的问题。"),
        {"type": "object", "properties": {"questions": {"type": "array", "minItems": 1,
            "maxItems": 3, "items": question}}, "required": ["questions"], "additionalProperties": False}),
        _ask, ToolDisplay(category="question", icon="question", verb="提问", summary="提出了{n}组问题",
            heading="用户提问", subject_fields=()), execution_class="control")
