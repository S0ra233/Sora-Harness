"""分类提示词配置；与模型地址、密钥和执行权限分别保存。"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_PROMPT_PATH = Path(__file__).with_name("default.json")
DEFAULT_SUMMARY_PATH = Path(__file__).with_name("summary.txt")
MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True)
class PromptConfig:
    """规则和资料分别保存；空字符串关闭字段，source 仅用于记录来源。"""

    system: dict[str, str] = field(default_factory=dict)
    context: dict[str, str] = field(default_factory=dict)
    version: int = 2
    source: str | None = None
    application: dict[str, str] = field(default_factory=dict)
    prelude: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version not in (1, 2):
            raise ValueError("提示词配置仅支持整数 version=1 或 2。")
        for category, values in (("system", self.system), ("context", self.context), ("application", self.application)):
            if not isinstance(values, dict):
                raise ValueError(f"{category} 必须是“字段名: 文本”的 JSON 对象。")
            for name, text in values.items():
                if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
                    raise ValueError(f"{category} 字段名使用小写英文字母、数字和下划线，并以字母开头。")
                if not isinstance(text, str):
                    raise ValueError(f"{category}.{name} 必须是字符串；留空请使用空字符串。")
            # 隔离调用方修改原字典的影响，不在每步模型请求时重复复制配置。
            object.__setattr__(self, category, dict(values))
        if not isinstance(self.prelude, list) or len(self.prelude) % 2:
            raise ValueError("prelude 必须是完整 user／assistant 对话组成的数组；不使用时填 []。")
        for index, message in enumerate(self.prelude):
            expected_role = "user" if index % 2 == 0 else "assistant"
            if (
                not isinstance(message, dict) or message.keys() != {"role", "content"}
                or message.get("role") != expected_role
                or not isinstance(message.get("content"), str) or not message["content"].strip()
            ):
                raise ValueError(
                    f"prelude[{index}] 仅接受 role={expected_role} 与非空字符串 content；"
                    "前置对话不能包含工具调用、图片或模型续接数据。"
                )
        # 前置对话是配置快照，不与调用方数组或真实会话轮次共享修改。
        object.__setattr__(self, "prelude", [dict(message) for message in self.prelude])
        if self.version == 1:
            # 旧规则仅迁移职责，保留文字；旧会话恢复后仍可使用原来的工作规则。
            rules = dict(self.system)
            application = dict(self.application)
            for name in ("task_strategy", "tool_guidance"):
                if name in rules:
                    application.setdefault(name, rules.pop(name))
            object.__setattr__(self, "system", rules)
            object.__setattr__(self, "application", application)
            object.__setattr__(self, "version", 2)


def _read_config(path: Path) -> PromptConfig:
    """文件格式错误在发送请求前报告，不静默回退到默认配置。"""
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("提示词配置文件超过 64 KiB。")
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or data.keys() - {"version", "system", "context", "application", "prelude"}:
            raise ValueError("配置顶层只接受 version、system、context、application、prelude，不是模型 API 的请求体。")
        data.setdefault("version", 2 if "application" in data else 1)
        return PromptConfig(**data, source=str(path))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"无法读取提示词配置 {path}：{exc}") from exc


def load_prompt_config(path: Path | str | None = None) -> PromptConfig:
    """规则与资料分别合并，前置对话整组采用用户数组；不改写旧会话快照。"""
    defaults = _read_config(DEFAULT_PROMPT_PATH)
    if path is None:
        return defaults
    custom = _read_config(Path(path).expanduser().resolve())
    return PromptConfig(
        system={**defaults.system, **custom.system}, context={**defaults.context, **custom.context},
        application={**defaults.application, **custom.application}, version=2, source=custom.source,
        prelude=custom.prelude,
    )


def load_summary_instructions() -> str:
    """新会话读取内置摘要正文并保存快照，旧空值继续使用原兼容指令。"""
    try:
        with DEFAULT_SUMMARY_PATH.open("rb") as source:
            raw = source.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("摘要提示词超过64 KiB。")
        text = raw.decode("utf-8-sig").strip()
        if not text:
            raise ValueError("内置摘要提示词不能为空。")
        return text
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"无法读取摘要提示词 {DEFAULT_SUMMARY_PATH}：{exc}") from exc
