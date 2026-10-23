"""子角色目录引用现有连接服务；保存配置不启动模型。"""
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

from .models import ModelSettingsService
from ..llm.model_catalog import ModelTarget
from ..llm.reasoning import parse_effort
from ..llm.validation import parse_temperature, parse_max_output_tokens
from ..prompts import PromptConfig
from ..runtime.agent.models import AgentLimits
from ..storage.atomic import write_text_atomic


@dataclass(frozen=True)
class SubagentProfile:
    profile_id: str
    name: str
    connection_id: str
    model_id: str
    description: str = ""
    enabled: bool = False
    instructions: str = ""
    effort: str | None = None
    timeout_seconds: float = 900
    max_steps: int = 8
    max_tool_calls: int = 16
    temperature: float | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) for value in (self.name, self.description, self.instructions)):
            raise ValueError("角色名称、说明与规则须为文本。")
        for identifier in (self.profile_id, self.connection_id, self.model_id):
            UUID(identifier)
        if not self.name.strip() or len(self.name) > 100 or not isinstance(self.enabled, bool):
            raise ValueError("子角色需要名称和明确开关。")
        if not isinstance(self.instructions, str) or len(self.instructions.encode("utf-8")) > 65536:
            raise ValueError("子角色规则超出允许长度。")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 86400:
            raise ValueError("运行总时限须在1～86400秒之间。")
        parse_effort(self.effort)
        AgentLimits(self.max_steps, self.max_tool_calls)
        object.__setattr__(self, "temperature", parse_temperature(self.temperature))
        object.__setattr__(self, "max_output_tokens", parse_max_output_tokens(self.max_output_tokens))


class SubagentProfiles:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "subagent-profiles.json"
        self.models = ModelSettingsService(directory / "subagent-connections.json")
        self._lock = RLock()

    def list(self) -> tuple[SubagentProfile, ...]:
        with self._lock:
            if not self.path.exists():
                return ()
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("profiles"), list):
                raise ValueError("子角色目录格式不正确，原文件保留。")
            values = tuple(SubagentProfile(**item) for item in data["profiles"])
            if len({item.profile_id for item in values}) != len(values):
                raise ValueError("子角色编号重复。")
            return values

    def save(self, profile: SubagentProfile) -> None:
        # 引用在保存时即解析；开启并不绕过提供商协议与effort校验。
        target, prompt = self.resolve(profile)
        from ..llm.reasoning import resolve_effort
        resolve_effort(target.config.reasoning, profile.effort, has_tools=True,
                       temperature=profile.temperature, max_output_tokens=profile.max_output_tokens)
        with self._lock:
            values = {item.profile_id: item for item in self.list()}
            values[profile.profile_id] = profile
            write_text_atomic(self.path, json.dumps({"version": 1, "profiles": [asdict(item) for item in values.values()]},
                                                    ensure_ascii=False, indent=2))

    def resolve(self, profile: SubagentProfile, catalog=None) -> tuple[ModelTarget, PromptConfig]:
        catalog = catalog if catalog is not None else self.models.model_catalog()
        entry = catalog.models.get(profile.model_id)
        if entry is None or entry.connection_id != profile.connection_id:
            raise ValueError("子角色引用的接口／模型已不存在。")
        prompt = PromptConfig(system={"identity": profile.instructions})
        return ModelTarget(entry.config, profile.model_id, profile.connection_id, entry.label, entry.connection_label), prompt

    def snapshot(self) -> dict[str, dict]:
        """一次主运行只捕获一次；后续保存不改动进行中的角色目录。"""
        values = {}
        selected = [profile for profile in self.list() if profile.enabled]
        if not selected:
            return values
        catalog = self.models.model_catalog()
        for profile in selected:
            target, prompt = self.resolve(profile, catalog)
            values[profile.profile_id] = {"profile": asdict(profile), "target": asdict(target), "prompt": asdict(prompt)}
        return values
