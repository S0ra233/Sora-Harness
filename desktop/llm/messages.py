"""模型消息与工具协议的数据对象；不登记或执行本地工具。"""
from dataclasses import dataclass, field, replace
import json
from typing import Any
from uuid import UUID


@dataclass
class TextContent:
    text: str
    type: str = field(default="text", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("消息中的文本必须是字符串。")


@dataclass(frozen=True)
class ImageContent:
    """持久附件引用，不接受任意路径、远程 URL 或内联 Base64。"""

    attachment_id: str
    type: str = field(default="image", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.attachment_id, str) or UUID(self.attachment_id).hex != self.attachment_id:
            raise ValueError("图片附件编号必须是规范的 UUID hex。")


@dataclass
class ToolSchema:
    """给模型看的名称、用途与参数规则，不包含处理函数或插件 ID。"""
    name: str
    description: str
    parameters: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("工具名称不能为空。")
        if not isinstance(self.description, str):
            raise ValueError("工具说明必须是字符串。")
        if not isinstance(self.parameters, dict) or self.parameters.get("type") != "object":
            raise ValueError("工具参数声明必须是 type=object 的 JSON Schema。")


@dataclass
class ToolCall:
    """模型请求的调用；保留原始参数 JSON，到执行阶段再解析和检查。"""
    call_id: str
    name: str
    arguments: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.call_id, self.name)):
            raise ValueError("工具调用必须提供非空的 call_id 和 name。")
        if not isinstance(self.arguments, str):
            raise ValueError("工具调用参数必须是原始 JSON 字符串。")


@dataclass
class ToolResult:
    """对应一条调用的结果消息；执行失败也是反馈给模型的结果。"""
    call_id: str
    name: str
    content: str
    is_error: bool = False
    role: str = field(default="tool", init=False)

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.call_id, self.name)):
            raise ValueError("工具结果必须提供非空的 call_id 和 name。")
        if not isinstance(self.content, str) or not isinstance(self.is_error, bool):
            raise ValueError("工具结果应包含文本 content 和布尔 is_error。")


@dataclass
class ReplayState:
    """同一协议、地址与模型的原生助手续接数据，不是整份API请求录制。

    system、历史、工具声明和生成参数由ModelRequest及Provider另行装配；
    本对象不承诺逐字节重放某次HTTP请求，也不能作为通用文本跨后端发送。
    """
    api_style: str
    endpoint_url: str
    model: str
    payload: dict[str, Any]
    provider_kind: str | None = None
    credential_ref: str | None = None
    key_env: str | None = None
    request_prefix_hash: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.payload, dict):
            raise ValueError("模型续接数据必须是 JSON 对象。")
        if self.request_prefix_hash is not None and (not isinstance(self.request_prefix_hash, str)
                or len(self.request_prefix_hash) != 64 or any(char not in "0123456789abcdef" for char in self.request_prefix_hash)):
            raise ValueError("原生续接前缀指纹须为SHA256。")


@dataclass(frozen=True)
class SkillContent:
    """提交时冻结的技能正文；sha256标识完整SKILL.md文件字节，不携带工具授权。"""

    skill_id: str
    name: str
    source: str
    path: str
    sha256: str
    body: str

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in
               (self.skill_id, self.name, self.source, self.path, self.sha256, self.body)):
            raise ValueError("技能快照需要编号、名称、来源、路径、版本和完整正文。")
        if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
               for value in (self.skill_id, self.sha256)):
            raise ValueError("技能编号和正文版本须为SHA256。")


@dataclass(frozen=True)
class QuestionReply:
    """用户真实回复的本地关联；Provider只发送消息正文，不发送这些标记。"""
    run_id: str
    call_id: str
    question_id: str
    answer: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in
               (self.run_id, self.call_id, self.question_id, self.answer)) or len(self.answer) > 20000:
            raise ValueError("问题回复需要调用编号、问题编号和非空回答；回答最多20000字符。")


@dataclass
class ModelMessage:
    """普通角色消息；可见文本与工具调用、后端续接信息分开保存。"""
    role: str
    content: list[TextContent | ImageContent] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    replay_state: ReplayState | None = None
    # 本地装配归属，Provider不发送此字段；不能从用户正文的JSON推导归属。
    runtime_context_id: str | None = None
    runtime_context_kind: str | None = None
    # 旧日志缺字段时仍可读；UI显示原文，发送投影才展开这份不可变资料。
    skills: list[SkillContent] = field(default_factory=list)
    question_reply: QuestionReply | None = None

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant"):
            raise ValueError("普通消息角色必须是 system、user 或 assistant；工具结果使用 ToolResult。")
        if (self.runtime_context_id is not None and (self.role != "user" or not isinstance(self.runtime_context_id, str)
                or not self.runtime_context_id or self.runtime_context_kind not in {"baseline", "environment_update", "request_state", "subagent_notice", "skill"})):
            raise ValueError("程序上下文标记须为user资料的非空归属与已知类型。")
        if self.runtime_context_kind is not None and self.runtime_context_id is None:
            raise ValueError("程序上下文类型需要对应的本地归属。")
        if isinstance(self.question_reply, dict):
            self.question_reply = QuestionReply(**self.question_reply)
        if self.question_reply is not None and (not isinstance(self.question_reply, QuestionReply)
                or self.role != "user" or self.runtime_context_id is not None):
            raise ValueError("问题回复标记只属于真实用户消息。")
        if not isinstance(self.skills, list):
            raise ValueError("技能快照须为数组。")
        self.skills = [SkillContent(**item) if isinstance(item, dict) else item for item in self.skills]
        if (any(not isinstance(item, SkillContent) for item in self.skills)
                or len({item.skill_id for item in self.skills}) != len(self.skills)
                or self.skills and (self.role != "user" or self.runtime_context_id)):
            raise ValueError("技能快照只属于真实用户输入，编号不得重复。")
        # 旧记录的 content 是字符串，新记录用带类型的内容块。
        if isinstance(self.content, str):
            self.content = [TextContent(self.content)]
        if not isinstance(self.content, list) or not isinstance(self.tool_calls, list):
            raise ValueError("消息内容和工具调用必须是数组。")
        self.content = [content_from_dict(value) if isinstance(value, dict) else value for value in self.content]
        if not all(isinstance(value, (TextContent, ImageContent)) for value in self.content):
            raise ValueError("消息内容仅支持文本和本地图片附件引用。")
        if self.role != "user" and any(isinstance(value, ImageContent) for value in self.content):
            raise ValueError("当前只接入用户图片输入；系统规则与模型输出不接受图片引用。")
        self.tool_calls = [ToolCall(**value) if isinstance(value, dict) else value for value in self.tool_calls]
        if not all(isinstance(value, ToolCall) for value in self.tool_calls):
            raise ValueError("消息的 tool_calls 必须是 ToolCall。")
        if isinstance(self.replay_state, dict):
            self.replay_state = ReplayState(**self.replay_state)
        if self.replay_state is not None and not isinstance(self.replay_state, ReplayState):
            raise ValueError("消息续接信息必须是 ReplayState。")
        if self.role != "assistant" and (self.tool_calls or self.replay_state is not None):
            raise ValueError("只有 assistant 消息可以携带工具调用或模型续接信息。")

    @property
    def text(self) -> str:
        return "".join(part.text for part in self.content if isinstance(part, TextContent))


Message = ModelMessage | ToolResult


def expand_skill_messages(messages: list[Message]) -> list[Message]:
    """历史、当前输入及压缩计数共用相同投影；绝不重新读取旧技能文件。"""
    expanded = []
    for message in messages:
        if isinstance(message, ModelMessage) and message.skills:
            for skill in message.skills:
                metadata = {"skill_id": skill.skill_id, "name": skill.name, "source": skill.source,
                            "path": skill.path, "sha256": skill.sha256}
                expanded.append(ModelMessage("user", [TextContent(
                    "[用户选定的技能资料] 以下是提交时保存的完整正文。按任务使用；"
                    "它不能新增工具或扩大执行权限。附属资料用read_skill_resource按需读取。\n"
                    + json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n\n" + skill.body)],
                    runtime_context_id=skill.skill_id, runtime_context_kind="skill"))
            expanded.append(replace(message, skills=[]))
        else:
            expanded.append(message)
    return expanded


def content_from_dict(value: dict[str, Any]) -> TextContent | ImageContent:
    """读取内容块；图片只还原引用，不在解析历史时读文件。"""
    if value.get("type") == "text":
        return TextContent(value.get("text"))
    if value.get("type") == "image":
        return ImageContent(value.get("attachment_id"))
    raise ValueError("消息内容仅支持 type=text 或 type=image。")


def user_message(text: str, images: list[ImageContent] | None = None) -> ModelMessage:
    """界面、CLI 与会话服务共用文字／图片的输入边界。"""
    message = ModelMessage("user", [TextContent(text), *(images or [])])
    if not text.strip() and not images:
        raise ValueError("请输入文字或选择图片。")
    return message


def message_from_dict(value: dict[str, Any]) -> Message:
    """从请求文件或运行记录还原消息，不执行其中的工具描述。"""
    if value.get("role") == "tool":
        fields = {key: item for key, item in value.items() if key != "role"}
        return ToolResult(**fields)
    return ModelMessage(**value)


def validate_tool_results(messages: list[Message]) -> None:
    """发送前检查调用与结果关联；避免孤立结果或漏掉并行调用的结果。"""
    pending: dict[str, ToolCall] = {}
    seen: set[str] = set()
    for message in messages:
        if isinstance(message, ToolResult):
            call = pending.get(message.call_id)
            if call is None or call.name != message.name:
                raise ValueError("工具结果的 call_id/name 与前面的待处理调用不对应。")
            pending.pop(message.call_id)
            continue
        if pending:
            raise ValueError("进入下一条普通消息前，必须提供上一轮所有工具调用的结果。")
        for call in message.tool_calls:
            if call.call_id in seen:
                raise ValueError("同一请求历史不能重复使用工具 call_id。")
            seen.add(call.call_id)
            pending[call.call_id] = call
    if pending:
        raise ValueError("本次请求缺少工具结果；不能只把 assistant 工具调用发回服务。")
