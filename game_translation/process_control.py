"""同步引擎的可取消进程边界；取消只回收进程，所有已有文件和日志保留。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
from threading import Event
from time import monotonic

from process_support import EngineCancelled
from process_support.tree import ProcessTree
from process_support.launcher import worker_command


@dataclass(frozen=True)
class ProcessControl:
    cancel_event: Event | None = None


_control = ContextVar("game_translation_process_control", default=ProcessControl())


@contextmanager
def process_scope(cancel_event: Event | None = None):
    token = _control.set(ProcessControl(cancel_event))
    try:
        check_cancelled()
        yield
    finally:
        _control.reset(token)


def check_cancelled() -> None:
    control = _control.get()
    if control.cancel_event is not None and control.cancel_event.is_set():
        raise EngineCancelled("操作已停止，已生成文件保留；请核对工作目录后决定后续操作。")


def _timeout(engine: str) -> float:
    path = Path(__file__).with_name("engine_processes.json")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or not isinstance(data.get("engines"), dict):
        raise ValueError(f"引擎进程配置格式不正确：{path}")
    value = data["engines"].get(engine, data.get("default_timeout_seconds", 900))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"引擎 {engine} 的 timeout_seconds 须为正数：{path}")
    return float(value)


def run_engine_process(command, directory, output, *, engine: str) -> subprocess.CompletedProcess:
    """包装进程在 stdin 等待准入，归入进程树后才会创建真正的引擎子进程。"""
    timeout = _timeout(engine)
    check_cancelled()
    tree, process = ProcessTree(), None
    started = monotonic()
    try:
        launcher = Path(__file__).with_name("process_launcher.py")
        process = subprocess.Popen(worker_command("engine", ["-B", str(launcher)]), cwd=directory,
                                   stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                   start_new_session=os.name != "nt")
        tree.attach(process.pid)
        check_cancelled()
        payload = json.dumps({"command": list(map(str, command)), "cwd": str(directory)}, ensure_ascii=True) + "\n"
        process.stdin.write(payload.encode("utf-8"))
        process.stdin.close()
        while True:
            check_cancelled()
            remaining = timeout - (monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(f"引擎 {engine} 超过 {timeout:g} 秒；进程树已停止，日志和产物保留：{output.name}")
            try:
                code = process.wait(timeout=min(0.1, remaining))
                return subprocess.CompletedProcess(command, code)
            except subprocess.TimeoutExpired:
                continue
    finally:
        try:
            tree.close()
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
