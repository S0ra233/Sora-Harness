"""项目与会话目录；保存列表元数据，消息与执行状态仍归 Core 和 Hub。"""
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from ..storage.atomic import write_text_atomic
from uuid import UUID, uuid4

from ..runtime.conversations import ConversationSummary
from ..runtime.session import utc_now


def _workspace_key(path: str | Path) -> str:
    """归属比较使用实际绝对路径；Windows 大小写差异不创建两个项目。"""
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _title(text: str) -> str:
    value = " ".join(text.split())
    return value[:40] + ("…" if len(value) > 40 else "") if value else "新聊天"


@dataclass(frozen=True)
class ProjectEntry:
    project_id: str
    name: str
    workspace: str
    created_at: str


@dataclass(frozen=True)
class ConversationEntry:
    conversation_id: str
    title: str
    project_id: str | None
    created_at: str
    updated_at: str
    turn_count: int = 0
    last_seq: int = 0
    manual_title: bool = False
    pinned: bool = False
    journal_size: int | None = None
    journal_mtime_ns: int | None = None
    archived: bool = False
    deleted: bool = False


@dataclass(frozen=True)
class CatalogSnapshot:
    """排序后的不可变列表摘要；置顶只是同一条目的另一处显示。"""

    projects: tuple[ProjectEntry, ...] = ()
    conversations: tuple[ConversationEntry, ...] = ()


def _read_catalog(path: Path) -> CatalogSnapshot:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return CatalogSnapshot()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in (1, 2):
            raise ValueError("会话目录须使用version=1或2。")
        projects = tuple(ProjectEntry(**item) for item in data["projects"])
        conversations = tuple(ConversationEntry(**item) for item in data["conversations"])
        project_ids = set()
        roots = set()
        for project in projects:
            if UUID(project.project_id).hex != project.project_id or project.project_id in project_ids:
                raise ValueError("项目编号无效或重复。")
            if not isinstance(project.name, str) or not project.name.strip() or not Path(project.workspace).is_absolute():
                raise ValueError("项目需要名称与绝对工作目录。")
            root = _workspace_key(project.workspace)
            if root in roots or not isinstance(project.created_at, str):
                raise ValueError("项目工作目录重复或时间无效。")
            project_ids.add(project.project_id)
            roots.add(root)
        seen = set()
        for entry in conversations:
            if UUID(entry.conversation_id).hex != entry.conversation_id or entry.conversation_id in seen:
                raise ValueError("会话编号无效或重复。")
            if entry.project_id is not None and entry.project_id not in project_ids:
                raise ValueError("会话归属的项目不存在。")
            if not all(isinstance(value, str) and value for value in (entry.title, entry.created_at, entry.updated_at)):
                raise ValueError("会话标题或时间无效。")
            if (type(entry.turn_count) is not int or entry.turn_count < 0 or type(entry.last_seq) is not int
                    or entry.last_seq < 0 or type(entry.manual_title) is not bool or type(entry.pinned) is not bool):
                raise ValueError("会话计数与置顶／人工命名字段无效。")
            if any(value is not None and (type(value) is not int or value < 0)
                   for value in (entry.journal_size, entry.journal_mtime_ns)):
                raise ValueError("日志变化标记须为非负整数或空值。")
            if type(entry.archived) is not bool or type(entry.deleted) is not bool or (entry.deleted and not entry.archived):
                raise ValueError("归档／删除标记无效；只有归档会话才能标记删除。")
            seen.add(entry.conversation_id)
        return CatalogSnapshot(projects, conversations)
    except (TypeError, KeyError, AttributeError, ValueError, RecursionError) as exc:
        raise ValueError(f"无法读取会话目录 {path}：{exc}；保留原文件，请修正后重试。") from exc


class CatalogService:
    """界面线程持有元数据；后台只返回日志摘要，不并发修改本对象。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        snapshot = _read_catalog(path)
        self._projects = {entry.project_id: entry for entry in snapshot.projects}
        self._conversations = {entry.conversation_id: entry for entry in snapshot.conversations}
        self.pending_save = False

    def snapshot(self) -> CatalogSnapshot:
        conversations = tuple(sorted(self._conversations.values(), key=lambda item: (item.updated_at, item.conversation_id), reverse=True))
        activity = {project.project_id: project.created_at for project in self._projects.values()}
        for entry in conversations:
            if entry.project_id is not None:
                activity[entry.project_id] = max(activity[entry.project_id], entry.updated_at)
        projects = tuple(sorted(self._projects.values(), key=lambda item: (activity[item.project_id], item.project_id), reverse=True))
        return CatalogSnapshot(projects, conversations)

    def project(self, project_id: str) -> ProjectEntry:
        return self._projects[project_id]

    def conversation(self, conversation_id: str) -> ConversationEntry:
        return self._conversations[conversation_id]

    def journal_signatures(self) -> dict[str, tuple[int, int]]:
        """只供启动／显式刷新跳过未变化日志，不替代 Core 校验或访问锁。"""
        return {item.conversation_id: (item.journal_size, item.journal_mtime_ns)
                for item in self._conversations.values()
                if item.journal_size is not None and item.journal_mtime_ns is not None}

    def add_project(self, name: str, workspace: Path) -> ProjectEntry:
        name = name.strip()
        if not name:
            raise ValueError("请输入项目名称。")
        root = workspace.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("项目工作目录必须是文件夹。")
        previous = self._project_at(str(root))
        if previous is not None:
            return previous
        entry = ProjectEntry(uuid4().hex, name, str(root), utc_now())
        self._projects[entry.project_id] = entry
        self._changed()
        return entry

    def _project_at(self, workspace: str) -> ProjectEntry | None:
        key = _workspace_key(workspace)
        return next((item for item in self._projects.values() if _workspace_key(item.workspace) == key), None)

    def observe(self, summaries: tuple[ConversationSummary, ...]) -> None:
        """合并日志事实，保留人工名称、置顶及归档／删除标记。"""
        changed = False
        for summary in summaries:
            old = self._conversations.get(summary.conversation_id)
            if old is not None and summary.last_seq < old.last_seq:
                continue
            project = None
            if summary.workspace is not None:
                project = self._project_at(summary.workspace)
                if project is None:
                    root = Path(summary.workspace).expanduser().resolve()
                    project = ProjectEntry(uuid4().hex, root.name or str(root), str(root), summary.created_at)
                    self._projects[project.project_id] = project
                    changed = True
            title = old.title if old is not None and old.manual_title else summary.branch_title or _title(summary.first_text)
            # 内存读回没有文件标记；同序号可沿用，序号变化后等待下次后台扫描。
            size, mtime = summary.journal_size, summary.journal_mtime_ns
            if size is None and old is not None and old.last_seq == summary.last_seq:
                size, mtime = old.journal_size, old.journal_mtime_ns
            entry = ConversationEntry(
                summary.conversation_id, title, project.project_id if project else None,
                summary.created_at, max(summary.updated_at, old.updated_at if old else summary.created_at),
                summary.turn_count, summary.last_seq, bool(old and old.manual_title or summary.branch_title), old.pinned if old else False,
                size, mtime, old.archived if old else False, old.deleted if old else False,
            )
            if old != entry:
                self._conversations[entry.conversation_id] = entry
                changed = True
        if changed:
            self._changed()

    def rename(self, conversation_id: str, title: str) -> None:
        title = title.strip()
        if not title:
            raise ValueError("会话名称不能为空。")
        self._conversations[conversation_id] = replace(self.conversation(conversation_id), title=title, manual_title=True)
        self._changed()

    def toggle_pin(self, conversation_id: str) -> None:
        entry = self.conversation(conversation_id)
        self._conversations[conversation_id] = replace(entry, pinned=not entry.pinned)
        self._changed()

    def record_activity(self, conversation_id: str, text: str, timestamp: str) -> None:
        """开始检查点已存在时更新排序；计数和序号仍等完整 Core 读回。"""
        entry = self.conversation(conversation_id)
        self._conversations[conversation_id] = replace(
            entry, updated_at=max(entry.updated_at, timestamp),
            title=_title(text) if not entry.manual_title and entry.turn_count == 0 and entry.title == "新聊天" else entry.title,
        )
        self._changed()

    def archive(self, conversation_id: str) -> None:
        """界面桥先等待任务退出；这里仅保存元数据，不改消息或活动时间。"""
        entry = self.conversation(conversation_id)
        if entry.deleted:
            raise ValueError("该聊天已从界面移除，不能归档。")
        if not entry.archived:
            self._conversations[conversation_id] = replace(entry, archived=True)
            self._changed()

    def restore(self, conversation_id: str) -> None:
        entry = self.conversation(conversation_id)
        if entry.deleted:
            raise ValueError("该聊天已从界面移除，首版不提供恢复已删除聊天的入口。")
        if entry.archived:
            self._conversations[conversation_id] = replace(entry, archived=False)
            self._changed()

    def delete_archived(self, conversation_id: str) -> None:
        """只移除列表条目；持久标记防止扫描再次加入，不删除日志／草稿／附件。"""
        entry = self.conversation(conversation_id)
        if not entry.archived:
            raise ValueError("只有已归档聊天才能删除，请先归档。")
        if not entry.deleted:
            self._conversations[conversation_id] = replace(entry, deleted=True)
            self._changed()

    def _changed(self) -> None:
        # 先保留内存结果；磁盘失败时可以只补存目录，不重建会话或重发请求。
        self.pending_save = True
        self.flush()

    def flush(self) -> None:
        if not self.pending_save:
            return
        content = json.dumps({
            "version": 2, "projects": [asdict(item) for item in self._projects.values()],
            "conversations": [asdict(item) for item in self._conversations.values()],
        }, ensure_ascii=False, indent=2) + "\n"
        write_text_atomic(self.path, content)
        self.pending_save = False
