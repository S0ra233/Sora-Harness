"""Atomic files, project task storage and portable path boundaries."""
import hashlib
import json
import ntpath
import os
from pathlib import Path
import tempfile

TASK_STATUSES = {"NONE", "PROCESSED", "NEEDS_HUMAN", "ERROR"}


def read_file(path, binary=False):
    path = Path(path)
    if not path.is_file():
        return None
    return path.read_bytes() if binary else path.read_text(encoding="utf-8-sig")


def write_file(path, content, binary=False):
    """Replace a complete file, so an interruption cannot truncate old work."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    options = {} if binary else {"encoding": "utf-8", "newline": ""}
    # A failed write/replace retains its temporary file for inspection. Normal
    # replacement still publishes only a completely flushed file.
    with tempfile.NamedTemporaryFile(
        mode="wb" if binary else "w", dir=path.parent,
        prefix="." + path.name + ".", delete=False, **options
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json(path, value):
    write_file(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_tasks(path):
    tasks = [json.loads(line) for line in (read_file(path) or "").splitlines() if line.strip()]
    for task in tasks:
        if not isinstance(task, dict) or task.get("status") not in TASK_STATUSES:
            raise ValueError("任务缺少有效状态，必须是 NONE/PROCESSED/NEEDS_HUMAN/ERROR")
        if not isinstance(task.get("text"), str) or not isinstance(task.get("id"), (str, int)):
            raise ValueError("任务缺少有效的原文或 ID")
    ids = [t["id"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("tasks.jsonl 中存在重复任务 ID")
    return tasks


def save_tasks(path, tasks):
    write_file(path, "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks))


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:24]


def separate_directories(first, second):
    first, second = Path(first).resolve(), Path(second).resolve()
    if first == second or first in second.parents or second in first.parents:
        raise ValueError(f"目录必须互相独立，不能重合或嵌套: {first} / {second}")


def safe_join(root, relative):
    """Accept portable relative resource paths, including on Windows."""
    relative = str(relative).replace("\\", "/")
    if ntpath.splitdrive(relative)[0] or relative.startswith("/") or ":" in relative:
        raise ValueError(f"资源路径必须是相对路径: {relative}")
    if ".." in relative.split("/"):
        raise ValueError(f"资源路径不能包含上级目录: {relative}")
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if target == root or root not in target.parents:
        raise ValueError(f"资源路径超出目录: {relative}")
    return target
