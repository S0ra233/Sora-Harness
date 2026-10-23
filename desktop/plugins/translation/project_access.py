"""Agent的只读项目访问；目录发现与Core校验不依赖界面。"""
import asyncio
from collections import deque
from concurrent.futures import CancelledError
import os
from pathlib import Path
import stat
from threading import Event

from . import _load_snapshot
from ...tools.contracts import ToolError
from ...tools.filesystem.paths import resolve_workspace_path
from ...tools.filesystem.discovery import SKIP_DIRECTORIES


MAX_DIRECTORIES = 128
MAX_ENTRIES = 4096
MAX_CANDIDATES = 16
PROJECT_FILES = ("project.json", "entries.json", "tasks.jsonl")
IGNORED_DIRECTORIES = SKIP_DIRECTORIES | {"archive"}


async def in_project_thread(function, *arguments):
    """取消后停止继续遍历，并等已开始的Core读取退出，不遗留后台读任务。"""
    cancelled = Event()
    pending = asyncio.create_task(asyncio.to_thread(function, *arguments, cancelled))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        cancelled.set()
        await asyncio.gather(pending, return_exceptions=True)
        raise


def _check_cancelled(cancelled):
    if cancelled.is_set():
        raise CancelledError()


def load_workspace_project(path, workspace):
    """固定项目文件同样检查真实路径，不能经文件链接绕过工作区边界。"""
    root = resolve_workspace_path(path, workspace, kind="directory")
    try:
        for filename in PROJECT_FILES:
            resolve_workspace_path((root / filename).relative_to(workspace).as_posix(), workspace, kind="file")
        return _load_snapshot(root)
    except ToolError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ToolError("invalid_translation_project", f"目录未通过翻译Core校验：{str(exc)[:300]}", details={
            "work_dir": str(root), "reason": str(exc)[:600], "error_type": type(exc).__name__,
            "required_files": list(PROJECT_FILES),
        }) from exc


def project_description(snapshot, workspace):
    """只返回选项目需要的事实，不把整份原文和译文塞回模型上下文。"""
    return {"work_dir": snapshot.work_dir.relative_to(workspace).as_posix(),
            "project_id": snapshot.manifest["project_id"],
            "engine": snapshot.manifest["engine"].get("family"),
            "scenes": len(snapshot.manifest["scenes"]), "tasks": len(snapshot.tasks),
            "translated_tasks": sum(bool(task["translation"].strip()) for task in snapshot.tasks)}


def select_project(workspace, path, cancelled):
    _check_cancelled(cancelled)
    snapshot = load_workspace_project(path, workspace)
    _check_cancelled(cancelled)
    return {**project_description(snapshot, workspace), "workspace": str(workspace),
            "view": {"page_id": "translation", "resource_id": str(snapshot.work_dir)}}


def discover_projects(workspace, path, max_depth, cancelled):
    """广度优先有界查找；深度、数量和拒绝访问都显式报告，不冒充全树清单。"""
    start = resolve_workspace_path(path, workspace, kind="directory")
    queue = deque([(start, 0)])
    seen = {start}
    projects, errors, limits = [], [], set()
    directories = entries = candidates = error_count = ignored = links = 0

    def error(relative, exc):
        nonlocal error_count
        error_count += 1
        if len(errors) < 8:
            errors.append({"path": relative, "reason": str(exc)[:400],
                           "code": exc.code if isinstance(exc, ToolError) else type(exc).__name__})

    while queue:
        _check_cancelled(cancelled)
        root, depth = queue.popleft()
        relative = root.relative_to(workspace).as_posix()
        directories += 1
        try:
            # 排队后目录也可能被替换成链接，因此每次实际读取前重新校验。
            metadata = root.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                links += 1
                continue
            root = resolve_workspace_path(relative, workspace, kind="directory")
            if (root / "project.json").is_file():
                if candidates >= MAX_CANDIDATES:
                    limits.add("candidate_limit")
                    break
                candidates += 1
                try:
                    projects.append(project_description(load_workspace_project(relative, workspace), workspace))
                except ToolError as exc:
                    limits.add("candidate_errors")
                    error(relative, exc)
            with os.scandir(root) as iterator:
                for entry in iterator:
                    _check_cancelled(cancelled)
                    if entries >= MAX_ENTRIES:
                        limits.add("entry_limit")
                        break
                    entries += 1
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                            links += 1
                            continue
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        if entry.name.casefold() in IGNORED_DIRECTORIES:
                            ignored += 1
                            continue
                        child = Path(entry.path).resolve()
                        if not child.is_relative_to(workspace):
                            links += 1
                            continue
                        if depth >= max_depth:
                            limits.add("depth_limit")
                        elif child not in seen:
                            if len(seen) >= MAX_DIRECTORIES:
                                limits.add("directory_limit")
                            else:
                                seen.add(child)
                                queue.append((child, depth + 1))
                    except OSError as exc:
                        limits.add("io_errors")
                        error(str(Path(relative) / entry.name), exc)
        except (OSError, ToolError) as exc:
            limits.add("io_errors")
            error(relative, exc)
        if "entry_limit" in limits:
            break
    return {"projects": projects, "workspace": str(workspace), "path": path,
            "scan_complete": not limits, "coverage_limits": sorted(limits),
            "examined_directories": directories, "examined_entries": entries,
            "checked_candidates": candidates, "errors": errors, "error_count": error_count,
            "skipped_technical_directories": ignored, "skipped_links": links,
            "budgets": {"max_depth": max_depth, "directories": MAX_DIRECTORIES,
                        "entries": MAX_ENTRIES, "candidates": MAX_CANDIDATES},
            "scope": "当前工作区内，排除技术目录、archive及链接；未覆盖范围不能推断为没有项目。"}
