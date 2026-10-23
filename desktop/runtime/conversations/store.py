"""追加式 JSONL 存取和同一会话的排他写入；不删除日志或锁文件。"""
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from uuid import UUID, uuid4

from ..session import utc_now
from .history import apply_event
from .models import Conversation, ConversationSettings, ConversationSummary
from .snapshots import ConversationSnapshots
from ...llm.messages import ModelMessage, message_from_dict


class ConversationJournal:
    """持有当前写入范围；一项检查点失败后不允许继续追加其他事件。"""

    def __init__(self, conversation: Conversation, output: BinaryIO, last_event: dict | None) -> None:
        self.conversation = conversation
        self._output = output
        self.last_event = last_event
        self.pending: dict[str, Any] | None = None
        self.failure: Exception | None = None

    def append(self, kind: str, run_id: str, data: dict[str, Any], *, step: int = 0) -> None:
        event = {
            "seq": self.conversation.last_seq + 1, "type": kind, "time": utc_now(),
            "run_id": run_id, "step": step, "data": data,
        }
        self.append_event(event)

    def append_event(self, event: dict[str, Any]) -> None:
        """先校验事件及尾部候选，再编码追加并 fsync；成功才发布会话事实。"""
        if self.pending is not None:
            raise OSError("前一检查点未确认保存，不能继续写入；请仅重试保存。")
        content = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        # 只复制可能改变的尾部状态，不复制整段历史；非法事件在落盘前失败。
        candidate = replace(self.conversation, turns=list(self.conversation.turns),
                            compactions=list(self.conversation.compactions), inputs=list(self.conversation.inputs))
        if hasattr(self.conversation, "_consumed_inputs"):
            candidate._consumed_inputs = self.conversation._consumed_inputs
            candidate._consumed_programs = self.conversation._consumed_programs
        turn = candidate.active_turn
        if turn is not None:
            copied = replace(turn, steps=list(turn.steps))
            if copied.steps:
                last = copied.steps[-1]
                copied.steps[-1] = replace(last, tools=[replace(tool) for tool in last.tools],
                                           attempts=list(last.attempts))
            candidate.turns[-1] = copied
        apply_event(candidate, event)
        self.pending = event
        try:
            if self._output.write(content) != len(content):
                raise OSError("会话检查点只写入了一部分；保留原文件，请检查末尾记录。")
            os.fsync(self._output.fileno())
            # 保持 Conversation 对象身份，已有服务引用也能看到提交后的事实。
            self.conversation.__dict__.update(candidate.__dict__)
        except (OSError, ValueError) as exc:
            self.failure = exc
            raise
        self.last_event, self.pending = event, None

    def flush(self) -> None:
        """重试已写入检查点的同步，不追加数据。"""
        os.fsync(self._output.fileno())

    def confirm_event(self, event: dict[str, Any]) -> None:
        """仅重试原保存：完整行已存在时同步确认，不追加重复事件。"""
        if self.conversation.last_seq == event["seq"] - 1:
            self.append_event(event)
        elif self.conversation.last_seq == event["seq"] and self.last_event == event:
            self.flush()
        else:
            raise ValueError("保存失败后会话已有其他变化，不能覆盖或重复追加原事件。")


class ConversationStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().resolve()
        self._snapshots = ConversationSnapshots(self.directory)

    def path(self, conversation_id: str) -> Path:
        return self.directory / (UUID(conversation_id).hex + ".jsonl")

    def create(self, settings: ConversationSettings, *, conversation_id: str | None = None, seed: dict | None = None) -> Conversation:
        """只创建新的 UUID 文件；配置快照与规则只在头部保存一次。"""
        conversation = Conversation(UUID(conversation_id).hex if conversation_id else uuid4().hex, settings, utc_now())
        document = {
            "type": "conversation", "version": 3, "conversation_id": conversation.conversation_id,
            "created_at": conversation.created_at, "settings": asdict(settings),
        }
        content = (json.dumps(document, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if seed is not None:
            event = {"seq": 1, "type": "history_seed", "time": conversation.created_at,
                     "run_id": uuid4().hex, "step": 0, "data": seed}
            apply_event(conversation, event)
            content += (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.path(conversation.conversation_id).open("xb", buffering=0) as output:
            if output.write(content) != len(content):
                raise OSError("会话头部没有完整写入；原文件保留。")
            os.fsync(output.fileno())
        return conversation

    def load(self, conversation_id: str) -> Conversation:
        """只读还原；不关闭未结束轮次，不创建 Runtime，也不发送请求。"""
        with self._exclusive(conversation_id):
            conversation, last_event = self._read(conversation_id)
            self._snapshots.save(self.path(conversation_id), conversation, last_event)
            return conversation

    def load_committed(self, conversation_id: str) -> Conversation:
        """分支读取完整追加行，不抢运行的写锁；正在写入的末尾行不作为事实。"""
        conversation, _ = self._read(conversation_id, allow_open_tail=True)
        return conversation

    def load_branch_point(self, conversation_id: str, anchor) -> tuple[Conversation, str]:
        """只读回放到所选输入首次使用的请求；后来的预设和压缩不进入分支。"""
        from .revisions import selected_message
        current = self.load_committed(conversation_id)
        selected_message(current, anchor)
        turn = next(turn for turn in current.turns if turn.run_id == anchor.run_id)
        if anchor.input_id:
            consumed = next(((item, step) for item in current.turns for step in item.steps
                             if anchor.input_id in step.input_ids), None)
            marker = ("model_started", consumed[0].run_id, consumed[1].step) if consumed else (
                "input_submitted", next(item.origin_run_id for item in current.inputs if item.input_id == anchor.input_id), anchor.input_id)
        else:
            marker = ("model_started", turn.run_id, turn.steps[0].step) if turn.steps else ("turn_started", turn.run_id, None)
        snapshot, event = self._read(conversation_id, allow_open_tail=True, through=marker)
        if event is None:
            raise ValueError("所选消息没有完整的持久化分支位置。")
        # 旧分支种子没有原事件序列；逐轮保存的选项与摘要位置仍可恢复其有效前缀。
        if event["type"] == "history_seed":
            target = next((item for item in snapshot.turns if item.run_id == marker[1]), turn)
            if target.options is None or target.options.prompt_config is None:
                raise ValueError("此旧分支没有保存所选请求的预设快照，不能准确恢复；请从原会话建立分支。")
            return snapshot, target.started_at
        return snapshot, event["time"]

    def load_settings(self, conversation_id: str) -> ConversationSettings:
        """启动后端前只读会话头，避免为取得规则再次解析整段历史。"""
        with self._exclusive(conversation_id):
            conversation, _ = self._read(conversation_id, include_events=False)
            return conversation.settings

    def summarize(self, conversation_id: str) -> ConversationSummary:
        """后台重建目录时逐行扫描，不实例化全部模型步骤和工具结果。

        摘要不是完整日志校验；打开／续接仍使用 load 的原验证与排他锁。
        """
        path = self.path(conversation_id)
        with self._exclusive(conversation_id), path.open("rb") as source:
            header = None
            latest, first_text, turns, seq = "", "", 0, 0
            revised = False
            for number, raw in enumerate(source, 1):
                try:
                    if not raw.endswith(b"\n"):
                        raise ValueError("记录末尾没有完整换行，原文件保留。")
                    data = json.loads(raw.decode("utf-8"))
                    if number == 1:
                        header = self._header(data, path)
                        latest = header.created_at
                        continue
                    if type(data["seq"]) is not int or data["seq"] != seq + 1 or not isinstance(data["time"], str):
                        raise ValueError("摘要记录序号或时间无效。")
                    seq = data["seq"]
                    latest = max(latest, data["time"])
                    revised = revised or data["type"] in {"history_revised", "history_seed"}
                    if data["type"] == "turn_started":
                        if turns == 0 and data["data"].get("user") is not None:
                            user = message_from_dict(data["data"]["user"])
                            if not isinstance(user, ModelMessage) or user.role != "user":
                                raise ValueError("首条消息不是用户输入。")
                            first_text = user.text
                        turns += 1
                except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                    raise ValueError(f"无法读取会话摘要 {path} 第 {number} 行：{exc}") from exc
            if header is None:
                raise ValueError(f"会话文件为空：{path}。")
            # 在持锁范围内取得同一份日志的标记，防止摘要与文件变化错配。
            stamp = os.fstat(source.fileno())
            if revised:
                conversation, _ = self._read(conversation_id)
                summary = ConversationSummary.from_conversation(conversation)
                return replace(summary, updated_at=latest, journal_size=stamp.st_size, journal_mtime_ns=stamp.st_mtime_ns)
            return ConversationSummary(path.stem, header.settings.workspace, header.created_at,
                                       latest, first_text, turns, seq, stamp.st_size, stamp.st_mtime_ns)

    @staticmethod
    def _header(data: dict, path: Path) -> Conversation:
        """完整读取与目录摘要共用头部边界，不另建一套配置解析。"""
        if data["type"] != "conversation" or type(data["version"]) is not int or data["version"] not in (1, 2, 3):
            raise ValueError("会话格式或版本不支持。")
        if data["conversation_id"] != path.stem or not isinstance(data["created_at"], str):
            raise ValueError("会话编号与文件名不符，或创建时间无效。")
        return Conversation(path.stem, ConversationSettings.from_dict(data["settings"]), data["created_at"])

    @contextmanager
    def writer(self, conversation_id: str) -> Iterator[ConversationJournal]:
        """从恢复到终态保持同一排他范围，第二个进程立即报告忙碌。"""
        with self._exclusive(conversation_id):
            conversation, last_event = self._read(conversation_id)
            with self.path(conversation_id).open("ab", buffering=0) as output:
                journal = ConversationJournal(conversation, output, last_event)
                yield journal
                if journal.pending is None:
                    self._snapshots.save(self.path(conversation_id), conversation, journal.last_event)

    def _read(self, conversation_id: str, *, include_events: bool = True, allow_open_tail: bool = False,
              through: tuple[str, str, int | str | None] | None = None) -> tuple[Conversation, dict | None]:
        path = self.path(conversation_id)
        conversation, last_event = None, None
        branch_found = through is None
        with path.open("rb") as source:
            restored = self._snapshots.restore(path, source) if include_events and through is None else None
            if restored is None:
                source.seek(0)
                first_line = 1
            else:
                conversation, last_event, line_count = restored
                first_line = line_count + 1
            for number, raw in enumerate(source, first_line):
                try:
                    if not raw.endswith(b"\n"):
                        if allow_open_tail and number > 1:
                            break
                        raise ValueError("记录末尾没有完整换行，可能在写入中被中断；不自动裁掉原内容。")
                    data = json.loads(raw.decode("utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("记录必须是 JSON 对象。")
                    if number == 1:
                        conversation = self._header(data, path)
                        if not include_events:
                            break
                    else:
                        apply_event(conversation, data)
                        last_event = data
                        if through is not None:
                            marker = (data["type"], data["run_id"], data["data"].get("input_id")
                                      if data["type"] == "input_submitted" else data["step"] if data["type"] == "model_started" else None)
                            inherited = data["type"] == "history_seed" and (
                                any(turn.run_id == through[1] for turn in conversation.turns)
                                or through[0] == "input_submitted" and any(item.input_id == through[2] for item in conversation.inputs))
                            if marker == through or inherited:
                                branch_found = True
                                break
                except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                    raise ValueError(f"无法读取会话 {path} 第 {number} 行：{exc}") from exc
        if conversation is None:
            raise ValueError(f"会话文件为空：{path}。")
        if not branch_found:
            raise ValueError("所选消息的请求边界不在完整记录中，未建立分支。")
        return conversation, last_event

    @contextmanager
    def _exclusive(self, conversation_id: str) -> Iterator[None]:
        """锁文件保留；操作系统在进程退出时释放锁，文件存在不等于忙碌。"""
        path = self.path(conversation_id).with_suffix(".lock")
        self.directory.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise OSError(f"会话 {conversation_id} 正在使用，或无法取得文件锁。") from exc
                try:
                    yield
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise OSError(f"会话 {conversation_id} 正在使用，或无法取得文件锁。") from exc
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
