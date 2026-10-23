"""基于统一请求的粗略预算；不是厂商 tokenizer，也不是累计账单。"""
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from ...llm.messages import ImageContent, Message, ModelMessage
from ...llm.models import ModelRequest
from ...llm.reasoning import parse_effort


def effective_window(model_window: int | None, configured_window: int | None = None) -> int | None:
    """会话预算不能扩大已知模型窗口；未知容量不由模型名字猜测。"""
    known = [value for value in (model_window, configured_window) if value is not None]
    return min(known) if known else None


@dataclass(frozen=True)
class ContextPolicy:
    """策略可显式提供窗口；应用可从模型快照补齐，仍未知时仅支持手动压缩。"""

    context_window: int | None = None
    output_reserve: int = 4096
    safety_margin: int = 1024
    trigger_ratio: float = 0.8
    # 仅为旧配置兼容保留；新的压缩尾部不再按模型窗口比例扩张。
    retain_ratio: float = 0.2
    summary_max_tokens: int = 4096
    summary_instructions: str = ""
    # 摘要是独立的辅助请求；默认不继承主任务档位，也不强制关闭模型推理。
    summary_reasoning_effort: str | None = None
    summary_request_mode: str = "reuse_prefix"
    retain_turns: int = 2
    compacted_input_tokens: int = 10000
    summary_target_tokens: int = 2000

    def __post_init__(self) -> None:
        for name in ("context_window", "output_reserve", "safety_margin", "summary_max_tokens",
                     "retain_turns", "compacted_input_tokens", "summary_target_tokens"):
            value = getattr(self, name)
            if name == "context_window" and value is None:
                continue
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} 必须是正整数。")
        for name in ("trigger_ratio", "retain_ratio"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
                raise ValueError(f"{name} 必须在 0 与 1 之间。")
        if self.summary_soft_limit >= self.compacted_input_tokens:
            raise ValueError("摘要长度目标及余量必须小于压缩后的总输入目标。")
        if not isinstance(self.summary_instructions, str):
            raise ValueError("摘要指令必须是文本。")
        parse_effort(self.summary_reasoning_effort)
        if self.summary_request_mode not in {"reuse_prefix", "isolated"}:
            raise ValueError("summary_request_mode 须为 reuse_prefix 或 isolated。")
        if self.context_window is not None and self.threshold(None) <= 0:
            raise ValueError("窗口不足以容纳输出预留与安全余量，请调整预算。")

    def input_budget(self, max_output_tokens: int | None) -> int | None:
        if self.context_window is None:
            return None
        reserve = max(self.output_reserve, max_output_tokens or 0)
        return self.context_window - reserve - self.safety_margin

    def threshold(self, max_output_tokens: int | None) -> int | None:
        budget = self.input_budget(max_output_tokens)
        if budget is None:
            return None
        return min(math.floor(self.context_window * self.trigger_ratio), budget)

    @property
    def summary_soft_limit(self) -> int:
        """正文目标允许约20%浮动；不是接口中包含思考的输出上限。"""
        return math.ceil(self.summary_target_tokens * 1.2)

    def compacted_input_budget(self, max_output_tokens: int | None) -> int:
        """压缩后的总输入目标独立于触发比例，同时受模型可用窗口约束。"""
        available = self.input_budget(max_output_tokens)
        return min(self.compacted_input_tokens, available) if available is not None else self.compacted_input_tokens


def _estimate(value: object) -> int:
    """每两个 UTF-8 字节粗估一个 token；原生续接数据也计入，不保证精确。"""
    content = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return math.ceil(len(content.encode("utf-8")) / 2)


def estimate_message_tokens(message: Message) -> int:
    # Provider采用原生回放时只发送一份；不能再把普通正文和回放副本都计入。
    data = message.replay_state.payload if isinstance(message, ModelMessage) and message.replay_state is not None else asdict(message)
    data = dict(data)
    image_count = 0
    if isinstance(message, ModelMessage):
        data.pop("runtime_context_id", None)
        data.pop("runtime_context_kind", None)
        data.pop("skills", None)
        data.pop("question_reply", None)
        image_count = sum(isinstance(part, ImageContent) for part in message.content)
        if message.replay_state is None:
            data.pop("replay_state", None)
            data["content"] = [part for part in data["content"] if part["type"] != "image"]
    # 每图预留 4096 token 只是本地预算策略，并非 tokenizer 或可靠上界。
    # 不将附件 UUID 或 Base64 字符数误当作图片的真实 token 用量。
    return _estimate(data) + 8 + image_count * 4096


def estimate_request_tokens(request: ModelRequest) -> int:
    """测量准备发送的规则、资料、消息和工具声明，不累计过去的请求用量。"""
    return sum(estimate_message_tokens(message) for message in request.messages) + _estimate([
        asdict(tool) for tool in request.tools
    ]) + 16


class RequestTokenEstimator:
    """一次运行内复用稳定消息的估算；换运行或压缩移出窗口即释放引用。"""

    def __init__(self):
        self._messages = {}
        self._tools = None

    def __call__(self, request: ModelRequest) -> int:
        current, total = {}, 16
        for message in request.messages:
            entry = self._messages.get(id(message))
            if entry is None or entry[0] is not message:
                entry = (message, estimate_message_tokens(message))
            current[id(message)] = entry
            total += entry[1]
        self._messages = current
        tools = tuple(request.tools)
        if self._tools is None or any(a is not b for a, b in zip(self._tools[0], tools)) or len(self._tools[0]) != len(tools):
            self._tools = (tools, _estimate([asdict(tool) for tool in tools]))
        return total + self._tools[1]


def load_context_policy(path: Path | None = None, *, context_window: int | None = None) -> ContextPolicy:
    """读取预算与摘要指令；这是本地配置，不是厂商 API 请求 JSON。"""
    data = {}
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("无法读取上下文配置，请提供 UTF-8 JSON 对象。") from exc
        if not isinstance(data, dict):
            raise ValueError("上下文配置必须是 JSON 对象。")
    if context_window is not None:
        data["context_window"] = context_window
    try:
        return ContextPolicy(**data)
    except TypeError as exc:
        raise ValueError("上下文配置包含未知字段，请使用 ContextPolicy 中的字段。") from exc
