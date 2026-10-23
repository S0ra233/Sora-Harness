"""运行记录的本地存取；不发送模型请求，也不替代插件的业务状态。"""
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from ..llm.models import ModelConfig, ModelRequest
from ..llm.validation import resolve_model_config
from ..llm.attempts import ModelAttemptEvent
from .runs import RunEvent, RunState
from ..storage.atomic import write_text_atomic


def utc_now() -> str:
    """统一用带时区的时间记录运行先后，不依赖窗口中的显示格式。"""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunRecord:
    """保存一次动作；运行管理对象按业务提供的转换函数保存结果。"""

    run_id: str
    plugin_id: str
    action: str
    config: ModelConfig
    payload: dict[str, Any]
    stream: bool
    started_at: str = field(default_factory=utc_now)
    state: RunState = RunState.RUNNING
    requests: list[ModelRequest] = field(default_factory=list)
    result: dict[str, Any] | None = None
    # 失败、取消时的片段单独保存；成功后以完整响应为准，不再重复存片段。
    partial_text: str = ""
    error: str | None = None
    finished_at: str | None = None
    diagnostics: str | None = None
    attempts: list[ModelAttemptEvent] = field(default_factory=list)


class SessionStore:
    """每次运行一个 JSON 文件；仅保存显式交入的记录，不接触 Provider 和密钥。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def save(self, record: RunRecord) -> None:
        """先写临时文件再替换本次记录；失败抛给调用方，保留原文件及临时产物。"""
        # 编号必须是后台生成的 UUID，不能作为任意路径写到记录目录外。
        filename = UUID(record.run_id).hex + ".json"
        path = self.directory / filename
        document = {"version": 1, "run": asdict(record)}
        content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        write_text_atomic(path, content)

    def list_records(self) -> tuple[list[RunRecord], list[str]]:
        """按开始时间倒序返回历史；个别损坏文件不挡住其他记录的展示。"""
        records = []
        errors = []
        for path in self.directory.glob("*.json"):
            try:
                records.append(self._load(path))
            except (OSError, ValueError) as exc:
                errors.append(f"{path.name}：{exc}")
        records.sort(key=lambda record: record.started_at, reverse=True)
        return records, errors

    def _load(self, path: Path) -> RunRecord:
        """只还原记录对象；读取历史不会创建 Runtime 或恢复执行。"""
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("version") != 1:
                raise ValueError("记录版本不支持。")
            data = document["run"]
            record = RunRecord(**{
                **data,
                "config": ModelConfig(**data["config"]),
                "requests": [ModelRequest(**request) for request in data["requests"]],
                "state": RunState(data["state"]),
                "attempts": [ModelAttemptEvent(**item) for item in data.get("attempts", [])],
            })
            if UUID(record.run_id).hex != path.stem:
                raise ValueError("记录编号与文件名不一致。")
            # dataclass 只还原字段，展示时需要的文本和对象类型仍在文件边界检查。
            text_fields = (record.plugin_id, record.action, record.started_at, record.partial_text)
            if not all(isinstance(value, str) for value in text_fields):
                raise ValueError("记录的文本字段格式不正确。")
            if not isinstance(record.payload, dict) or (
                record.result is not None and not isinstance(record.result, dict)
            ):
                raise ValueError("记录的输入与结果应为 JSON 对象。")
            if record.diagnostics is not None and not isinstance(record.diagnostics, str):
                raise ValueError("记录的错误定位信息应为文本。")
            return record
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"无法读取运行记录：{exc}") from exc


class RunTracker:
    """管理运行记录和待存结果；调用方串行交入事件，这里不依赖 Qt。"""

    def __init__(
        self, store: SessionStore, encode_result: Callable[[Any], dict[str, Any]],
    ) -> None:
        self._store = store
        self._encode_result = encode_result
        self._records: dict[str, RunRecord] = {}
        self._pending: dict[str, RunRecord] = {}

    def load_history(self) -> tuple[list[RunRecord], list[str]]:
        """读取已有记录供展示；没有恢复执行或重新调用模型。"""
        records, errors = self._store.list_records()
        self._records.update((record.run_id, record) for record in records)
        return records, errors

    def begin_run(
        self, *, run_id: str, plugin_id: str, action: str,
        config: ModelConfig, payload: dict[str, Any], stream: bool,
    ) -> RunRecord:
        """先保存基础记录；保存成功后才登记，调用方随后启动后台任务。"""
        record = RunRecord(
            run_id=run_id, plugin_id=plugin_id, action=action,
            config=resolve_model_config(config), payload=dict(payload), stream=stream,
        )
        self._store.save(record)
        self._records[run_id] = record
        return record

    def get_record(self, run_id: str) -> RunRecord:
        """按已登记的编号读取记录，供界面显示；调用方不修改它。"""
        return self._records[run_id]

    @property
    def pending_count(self) -> int:
        """待存状态由运行管理对象维护，不从界面标签推断。"""
        return len(self._pending)

    def accept_event(self, event: RunEvent) -> tuple[RunRecord, str | None]:
        """更新事件对应的记录；终态才保存，第二个返回值是保存错误。"""
        record = self._records[event.run_id]
        if event.state == RunState.RUNNING:
            if event.attempt is not None:
                record.attempts.append(event.attempt)
                if event.attempt.phase == "started":
                    record.partial_text = ""
                elif event.attempt.phase in ("failed", "cancelled"):
                    record.partial_text = event.attempt.partial_text
            if event.request is not None:
                record.requests.append(event.request)
            if event.text_delta is not None:
                record.partial_text += event.text_delta
            return record, None
        record.state = event.state
        record.error = event.error
        record.diagnostics = event.diagnostics
        record.finished_at = utc_now()
        # Agent 失败或取消也可能带着已执行步骤，不能丢弃其事实。
        if event.result is not None:
            record.result = self._encode_result(event.result)
        if event.state == RunState.SUCCEEDED:
            record.partial_text = ""
        return record, self._save_result(record)

    def _save_result(self, record: RunRecord) -> str | None:
        """模型结果与保存结果分开；失败保留内存记录，不重新调用模型。"""
        try:
            self._store.save(record)
        except (OSError, ValueError, TypeError) as exc:
            self._pending[record.run_id] = record
            return str(exc)
        self._pending.pop(record.run_id, None)
        return None

    def retry_pending(self) -> list[str]:
        """只重试文件保存；成功后移出待存列表，不删除文件。"""
        errors = []
        for record in list(self._pending.values()):
            error = self._save_result(record)
            if error is not None:
                errors.append(f"{record.run_id[:8]}：{error}")
        return errors
