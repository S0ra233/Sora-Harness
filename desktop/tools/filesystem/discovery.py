"""文件查找与文本搜索共用的路径匹配、遍历和扫描范围。"""
import asyncio
import fnmatch
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts import ToolError
from .limits import MAX_DIRECTORIES, MAX_EXAMINED_ENTRIES
from .coverage import Coverage


SKIP_DIRECTORIES = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})


class FilePattern:
    """一次调用只编译一次；无分隔符匹配文件名，有分隔符按路径分段匹配。"""

    def __init__(self, value: Any) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ToolError("invalid_arguments", "文件匹配模式必须是 1 到 200 字符的非空字符串。")
        self.pattern = value.replace("\\", "/")
        while self.pattern.startswith("./"):
            self.pattern = self.pattern[2:]
        parts = self.pattern.casefold().split("/")
        if ":" in self.pattern or any(part in {"", ".", ".."} for part in parts):
            raise ToolError("invalid_arguments", "文件匹配模式须为相对路径，不含盘符、空路径段或 '..'。")
        if "\x00" in self.pattern or "{" in self.pattern or "}" in self.pattern or self.pattern.startswith("!"):
            raise ToolError("invalid_arguments", "文件匹配模式不支持空字节、花括号或否定模式。")
        # 只查文件，末尾 ** 仍须包含文件名；src/** 不应匹配名为 src 的文件。
        if parts[-1] == "**":
            parts.append("*")
        # fnmatch 只处理单个路径段；** 的跨目录语义由 matches 处理。
        self._segments = tuple(None if part == "**" else re.compile(fnmatch.translate(part)) for part in parts)

    def matches(self, relative: str) -> bool:
        """*、?、[] 不跨层；完整段 ** 可消耗零层或多层，不枚举匹配组合。"""
        names = relative.casefold().split("/")
        if len(self._segments) == 1 and self._segments[0] is not None:
            return self._segments[0].fullmatch(names[-1]) is not None
        positions = {0}
        for name in names:
            following = set()
            for position in positions:
                # ** 可留在原段消耗这一层，也可跳过它、让下一段匹配同一名称。
                while position < len(self._segments):
                    segment = self._segments[position]
                    if segment is None:
                        following.add(position)
                        position += 1
                    else:
                        if segment.fullmatch(name) is not None:
                            following.add(position + 1)
                        break
            if not following:
                return False
            positions = following
        return len(self._segments) in positions


@dataclass
class ScanProgress:
    """只保留本次遍历计数，不保存整个文件树；read_errors 也包含目录枚举失败。"""

    directories: int = 0
    examined_entries: int = 0
    skipped_paths: int = 0
    read_errors: int = 0
    limit_reason: str | None = None
    coverage: Coverage = field(default_factory=lambda: Coverage("python"))


async def candidate_files(root: Path, pattern: FilePattern, progress: ScanProgress) -> AsyncIterator[Path]:
    """逐目录枚举实际范围；显式指定单文件时直接返回，不再用模式过滤它。"""
    if root.is_file():
        progress.examined_entries = 1
        await asyncio.sleep(0)
        yield root
        return
    pending, seen = [root], {root}
    while pending:
        directory = pending.pop()
        progress.directories += 1
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if progress.examined_entries == MAX_EXAMINED_ENTRIES:
                        progress.limit_reason = "entry_limit"
                        return
                    progress.examined_entries += 1
                    if progress.examined_entries % 25 == 0:
                        await asyncio.sleep(0)
                    try:
                        path = Path(entry.path).resolve()
                        path.relative_to(root)
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.casefold() in SKIP_DIRECTORIES or path in seen:
                                progress.skipped_paths += 1
                                progress.coverage.skip("ignored_directory" if entry.name.casefold() in SKIP_DIRECTORIES else "duplicate_directory")
                                continue
                            if len(seen) == MAX_DIRECTORIES:
                                progress.limit_reason = "directory_limit"
                                return
                            seen.add(path)
                            pending.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            if pattern.matches(path.relative_to(root).as_posix()):
                                yield path
                        else:
                            progress.skipped_paths += 1
                            progress.coverage.skip("symlink_or_special")
                    except (ValueError, RuntimeError, OSError):
                        progress.skipped_paths += 1
                        progress.coverage.skip("unresolvable_or_outside_scope")
        except OSError:
            progress.read_errors += 1
            progress.coverage.skip("directory_read_error")
