"""发现元数据和按需读取正文；同名保留，实际路径是身份和资料边界。"""
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..llm.messages import ModelMessage, SkillContent, ToolResult
from ..tools.contracts import ToolContext, ToolError
from ..tools.filesystem.read import read_text_file
from .frontmatter import SkillDependencyError, parse_metadata


MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_SKILL_DIRECTORIES = 2048
MAX_SKILL_ENTRIES = 20000


def read_document(path, *, header_only=False):
    """目录只读取有界头部；载入正文则完整读取或明确拒绝，不悄悄截断。"""
    limit = MAX_HEADER_BYTES if header_only else MAX_BODY_BYTES
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        if header_only:
            pieces, used = [], 0
            while used <= limit:
                line = source.readline(limit - used + 1)
                if not line:
                    break
                pieces.append(line)
                used += len(line)
                if len(pieces) > 1 and line.strip() == b"---":
                    break
            raw = b"".join(pieces)
        else:
            raw = source.read(limit + 1)
        after = os.fstat(source.fileno())
    current = path.stat()
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if signature(before) != signature(after) or signature(after) != signature(current):
        facts = lambda value: {"bytes": value.st_size, "mtime_ns": value.st_mtime_ns,
                               "device": value.st_dev, "file_id": value.st_ino}
        raise ToolError("file_changed", f"技能文件读取期间变化，请重新读取：{path}",
                        details={"resolved_path": str(path), "before": facts(before), "after": facts(after),
                                 "current_path": facts(current), "observed_sha256": hashlib.sha256(raw).hexdigest(),
                                 "observed_sha256_scope": "read_bytes", "observed_bytes": len(raw), "version_basis": "stat"})
    if not header_only and len(raw) > limit:
        raise ValueError(f"技能正文超过4 MiB，请把大资料放入附属文件按需读取：{path}")
    if header_only and len(raw) > limit:
        raise ValueError(f"技能元数据超过64 KiB：{path}")
    text = raw.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"SKILL.md缺少YAML元数据头：{path}")
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if end is None:
        raise ValueError(f"技能元数据未闭合或超过64 KiB：{path}")
    header = "".join(lines[1:end])
    if len(header.encode("utf-8")) > MAX_HEADER_BYTES:
        raise ValueError(f"技能元数据超过64 KiB：{path}")
    return header, "".join(lines[end + 1:]), hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SkillEntry:
    skill_id: str
    name: str
    description: str
    source: str
    path: Path
    root: Path
    user_invocable: bool = True
    model_invocable: bool = True

    @property
    def token(self):
        return "skill:" + self.name + "@" + self.skill_id[:12]


def discover(roots, *, check_cancelled=None):
    """确定顺序的有限目录扫描；遇到技能后不递归其scripts／references。"""
    entries, issues, seen, examined, enumerated = [], [], set(), 0, 0
    for source_name, source_root in roots:
        if check_cancelled is not None:
            check_cancelled()
        try:
            root = Path(source_root).expanduser().resolve()
            if not root.exists():
                continue
            if not root.is_dir():
                raise ValueError("技能来源不是目录。")
            pending = [root]
            while pending:
                if check_cancelled is not None:
                    check_cancelled()
                folder = pending.pop()
                examined += 1
                if examined > MAX_SKILL_DIRECTORIES:
                    raise ValueError("目录扫描达到2048项，本次目录不完整；请缩小技能来源。")
                resolved = folder.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    issues.append(f"跳过越出来源的目录链接：{folder}")
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                skill_path = resolved / "SKILL.md"
                if skill_path.exists():
                    try:
                        actual = skill_path.resolve(strict=True)
                        if not actual.is_relative_to(resolved) or not actual.is_file():
                            raise ValueError("SKILL.md链接越出技能目录或不是普通文件。")
                        header, _, _ = read_document(actual, header_only=True)
                        metadata = parse_metadata(header, resolved.name)
                        identifier = hashlib.sha256(os.path.normcase(str(actual)).encode("utf-8")).hexdigest()
                        entries.append(SkillEntry(identifier, metadata["name"], metadata["description"], source_name,
                                                  actual, root, metadata["user_invocable"], metadata["model_invocable"]))
                    except SkillDependencyError as exc:
                        return tuple(entries), (*issues, str(exc))
                    except (OSError, ValueError) as exc:
                        issues.append(f"{skill_path}：{exc}")
                    continue
                try:
                    children = []
                    with os.scandir(resolved) as items:
                        for value in items:
                            enumerated += 1
                            if check_cancelled is not None and enumerated % 128 == 0:
                                check_cancelled()
                            if enumerated > MAX_SKILL_ENTRIES:
                                raise ValueError("目录枚举达到20000项，本次目录不完整；请缩小技能来源。")
                            if value.name.startswith(".") or value.name in {"node_modules", "__pycache__"}:
                                continue
                            try:
                                if value.is_dir():
                                    children.append(Path(value.path))
                            except OSError as exc:
                                issues.append(f"{value.path}：{exc}")
                    pending.extend(sorted(children, key=lambda value: value.name.casefold(), reverse=True))
                except OSError as exc:
                    issues.append(f"{resolved}：{exc}")
        except (OSError, ValueError) as exc:
            issues.append(f"{source_name} {source_root}：{exc}")
    return tuple(entries), tuple(issues)


class SkillScope:
    """一个请求运行使用固定目录；加载检查当前文件，旧快照只用于恢复资料。"""
    def __init__(self, entries, issues, *, roots=()):
        self.entries, self.issues = entries, list(issues)
        self.roots = tuple((name, str(path)) for name, path in roots)
        self.loaded = set()

    def model_catalog(self):
        return [{"skill_id": entry.skill_id, "name": entry.name, "description": entry.description,
                 "source": entry.source} for entry in self.entries if entry.model_invocable]

    def resolve(self, identifier):
        if not isinstance(identifier, str) or not identifier.strip():
            raise ToolError("invalid_arguments", "skill_id须为目录中的编号或唯一名称。")
        matches = [entry for entry in self.entries if identifier.casefold() in
                   {entry.skill_id.casefold(), entry.name.casefold(), entry.token.casefold()}]
        if not matches:
            message = "技能不在本轮有效目录中。" if not self.issues else "技能目录读取有缺口：" + self.issues[0]
            raise ToolError("skill_not_found" if not self.issues else "skill_unavailable", message, details={"skill_id": identifier,
                "catalog_complete": not bool(self.issues), "catalog_issues": self.issues[:3], "issue_count": len(self.issues)})
        if len(matches) > 1:
            raise ToolError("skill_ambiguous", "存在同名技能，请使用skill_id区分来源。",
                            details={"candidates": [{"skill_id": entry.skill_id, "source": entry.source} for entry in matches]})
        return matches[0]

    def load(self, identifier, *, manual=False):
        entry = self.resolve(identifier)
        if not (entry.user_invocable if manual else entry.model_invocable):
            raise ToolError("skill_not_invocable", "该技能未开放此种调用方式。", details={"skill_id": entry.skill_id})
        try:
            actual = entry.path.resolve(strict=True)
            if actual != entry.path or not actual.is_relative_to(entry.root) or not actual.is_relative_to(entry.path.parent):
                raise ValueError("技能文件的实际路径已变化，请刷新目录。")
            header, body, digest = read_document(actual)
            metadata = parse_metadata(header, actual.parent.name)
            if (metadata["name"] != entry.name or metadata["user_invocable"] != entry.user_invocable
                    or metadata["model_invocable"] != entry.model_invocable):
                raise ValueError("技能名称或调用规则已变化，请刷新目录。")
            if not body.strip():
                raise ValueError("技能正文为空。")
        except ToolError:
            raise
        except (OSError, ValueError) as exc:
            raise ToolError("skill_read_failed", str(exc), details={"skill_id": entry.skill_id, "path": str(entry.path),
                "errno": getattr(exc, "errno", None), "winerror": getattr(exc, "winerror", None)}) from exc
        snapshot = SkillContent(entry.skill_id, entry.name, entry.source, str(actual), digest, body)
        if not manual:
            self.loaded.add(entry.skill_id)
        return snapshot

    def restore(self, messages):
        """只恢复程序快照或真实成功工具结果，普通用户JSON不能签发资料访问。"""
        known = {entry.skill_id for entry in self.entries}
        for message in messages:
            if isinstance(message, ModelMessage):
                self.loaded.update(skill.skill_id for skill in message.skills if skill.skill_id in known)
                if message.runtime_context_kind == "skill" and message.runtime_context_id in known:
                    self.loaded.add(message.runtime_context_id)
            elif isinstance(message, ToolResult) and message.name == "load_skill" and not message.is_error:
                try:
                    identifier = json.loads(message.content).get("data", {}).get("skill_id")
                except (ValueError, AttributeError):
                    continue
                if identifier in known:
                    self.loaded.add(identifier)

    async def read_resource(self, identifier, path, cursor=None):
        entry = self.resolve(identifier)
        if entry.skill_id not in self.loaded:
            raise ToolError("skill_not_loaded", "先加载技能正文，再按需读取附属资料。", details={"skill_id": entry.skill_id})
        base = entry.path.parent.resolve(strict=True)
        if base != entry.path.parent or not base.is_relative_to(entry.root):
            raise ToolError("skill_path_changed", "技能目录位置已经变化，请重新读取目录。")
        arguments = {"path": path}
        if cursor is not None:
            arguments["cursor"] = cursor
        # 复用流式UTF-8读取、游标、版本和剪裁说明，不授予通用文件工具根外权限。
        result = await read_text_file(arguments, ToolContext(base, frozenset({"read_text_file"})))
        return {**result, "skill_id": entry.skill_id, "resource_base": str(base), "read_only": True}
