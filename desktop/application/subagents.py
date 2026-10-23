"""应用拥有子进程、持久任务及完成收件箱；视图不拥有执行器。"""
import asyncio
import hashlib
import json
import os
import subprocess
import threading
import time
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID, uuid4
from process_support.launcher import worker_command

from .subagent_profiles import SubagentProfiles
from .plugin_settings import PluginSettings
from ..mcp.settings import delegated_snapshot
from ..storage.atomic import write_text_atomic
from ..runtime.session import utc_now
from ..tools.command.processes import ProcessTree
from ..tools.contracts import ToolError
from ..runtime.subagents import SUBAGENT_TOOLS
from ..tools.questions import QUESTION_TOOL

ACTIVE = frozenset({"starting", "running", "cancelling"})


class SubagentManager:
    def __init__(self, directory: Path, tasks=None) -> None:
        self.directory = directory / "subagents"
        self.profiles = SubagentProfiles(directory)
        self.plugins = PluginSettings(directory / "plugins.json")
        self.tasks = tasks
        self.changed = None
        self._lock = threading.RLock()
        self._records = {}
        self._processes = {}
        self._controls = {}
        self._timers = {}
        self._suppressed = set()
        self._wakes = {}
        self._parent_targets = {}
        self._emit_times = {}
        self._trees = {}
        self._readers = {}
        self.load_errors = []
        self.configuration_error = None
        self._live = {}
        self._load()

    def _load(self) -> None:
        if not self.directory.exists():
            return
        for path in self.directory.glob("*/state.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if UUID(data["child_id"]).hex != path.parent.name or data["status"] not in ACTIVE | {"completed", "failed", "timed_out", "interrupted", "save_failed"}:
                    raise ValueError("子记录归属或状态不正确。")
                UUID(data["parent_id"])
                if data["status"] in ACTIVE:
                    data.update(status="interrupted", error="应用重开；旧执行器不再受本进程管理，未自动请求模型。", finished_at=utc_now(), notice_pending=False)
                    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))
                self._records[data["child_id"]] = data
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.load_errors.append(f"{path}：{type(exc).__name__}；原文件保留。")
        inbox = self.directory / "inbox.json"
        if inbox.exists():
            try:
                data = json.loads(inbox.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("子收件箱须为对象。")
                self._suppressed = set(data.get("suppressed", []))
                self._wakes = data.get("wakes", {})
                self._parent_targets = data.get("parent_targets", {})
                if not isinstance(self._wakes, dict) or any(type(value) is not int or value < 0 for value in self._wakes.values()) or not isinstance(self._parent_targets, dict):
                    raise ValueError("收件额度格式不正确。")
            except (OSError, ValueError, TypeError) as exc:
                self.load_errors.append(f"{inbox}：{type(exc).__name__}；原文件保留。")

    def _save(self, record) -> None:
        write_text_atomic(self.directory / record["child_id"] / "state.json",
                          json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False))

    def _save_inbox(self) -> None:
        if self.load_errors:
            raise ValueError("子记录目录存在读取错误，请先检查原记录；禁止覆盖为新收件箱。")
        write_text_atomic(self.directory / "inbox.json", json.dumps({"version": 1,
                          "suppressed": sorted(self._suppressed), "wakes": self._wakes,
                          "parent_targets": self._parent_targets}, ensure_ascii=False, indent=2))

    def bind(self, conversation_id, run_id, settings, tool_context, *, new_target: bool, human_resume: bool = False):
        with self._lock:
            records = [item for item in self._records.values() if item["parent_id"] == conversation_id]
            enabled = self.plugins.enabled("subagent", default=False)
            try:
                profiles = self.profiles.snapshot() if enabled and not self.load_errors else {}
                self.configuration_error = None
            except (OSError, ValueError, TypeError) as exc:
                self.configuration_error = f"子角色配置不可用：{exc}；未开放启动工具。"
                profiles = {}
            target = self._parent_targets.get(conversation_id, run_id)
            if new_target and not self.load_errors:
                target = run_id
                self._parent_targets[conversation_id] = target
                self._wakes[target] = 0
                self._suppressed.discard(target)
                if profiles or records:
                    self._save_inbox()
            elif human_resume and not self.load_errors:
                self._suppressed.discard(target)
                self._save_inbox()
            lock_name = hashlib.sha256(os.path.normcase(str(tool_context.workspace)).encode()).hexdigest()
            mutation_lock = self.directory / "locks" / (lock_name + ".lock")
            access = ScopedSubagents(self, conversation_id, run_id, target, settings, tool_context, profiles)
            return access, mutation_lock, not self.load_errors and bool(profiles or records)

    def list(self, parent_id):
        with self._lock:
            return tuple(self.public(item) for item in self._records.values() if item["parent_id"] == parent_id)

    def view_access(self):
        return SubagentViews(self)

    @staticmethod
    def public(record):
        keys = ("child_id", "run_id", "name", "status", "started_at", "finished_at", "error", "model", "effort", "parent_id", "target_id")
        return {key: record.get(key) for key in keys}

    def _owned(self, parent_id, child_id):
        item = self._records.get(child_id)
        if item is None or item["parent_id"] != parent_id:
            raise ToolError("subagent_not_found", "该子任务不属于当前父会话。")
        return item

    def _emit(self, record):
        now, signature = time.monotonic(), (record["run_id"], record["status"])
        previous = self._emit_times.get(record["child_id"])
        if previous and previous[1] == signature and now - previous[0] < .12:
            return
        self._emit_times[record["child_id"]] = (now, signature)
        callback = self.changed
        if callback is not None:
            callback(self.public(record))

    def start(self, scope, profile_id, task, context):
        with self._lock:
            previous = next((item for item in self._records.values() if item["parent_id"] == scope.parent_id
                             and item["accept_run_id"] == scope.run_id and item["accept_call_id"] == scope.call_id), None)
            if previous is not None:
                return self.public(previous)
            frozen = scope.profiles.get(profile_id)
            if frozen is None or scope.target_id in self._suppressed:
                raise ToolError("subagent_disabled", "角色未启用或父目标已停止。")
            if sum(item["status"] in ACTIVE for item in self._records.values()) >= 8 or sum(
                    item["status"] in ACTIVE and item["parent_id"] == scope.parent_id for item in self._records.values()) >= 2:
                raise ToolError("subagent_concurrency_limit", "子运行并发已达上限；等待已有任务完成。")
            if sum(item["target_id"] == scope.target_id for item in self._records.values()) >= 8:
                raise ToolError("subagent_start_limit", "同一用户目标的子会话启动额度已用完。")
            child_id, child_run = uuid4().hex, uuid4().hex
            profile = frozen["profile"]
            settings = asdict(scope.settings)
            settings.update(config=frozen["target"]["config"], prompt_config=frozen["prompt"], system="",
                            temperature=profile.get("temperature"), max_output_tokens=profile.get("max_output_tokens"),
                            tool_names=sorted(scope.context.allowed_tools - SUBAGENT_TOOLS - {QUESTION_TOOL}), reasoning_effort=profile["effort"],
                            limits={"max_steps": profile["max_steps"], "max_tool_calls": profile["max_tool_calls"]},
                            model_id=profile["model_id"], connection_id=profile["connection_id"],
                            model_label=frozen["target"].get("label"), connection_label=frozen["target"].get("connection_label"))
            settings["shell_config"] = asdict(scope.context.shell) if scope.context.shell is not None and "run_command" in settings["tool_names"] else None
            settings["search_config"] = asdict(scope.context.search_config) if scope.context.search_config is not None and {"find_files", "search_text"}.intersection(settings["tool_names"]) else None
            record = {"child_id": child_id, "run_id": child_run, "parent_id": scope.parent_id,
                      "target_id": scope.target_id, "accept_run_id": scope.run_id, "accept_call_id": scope.call_id,
                      "name": profile["name"], "status": "starting", "started_at": utc_now(), "finished_at": None,
                      "model": settings["config"]["model"], "effort": profile["effort"], "error": None,
                      "settings": settings, "timeout_seconds": profile["timeout_seconds"],
                      "mutation_lock": str(scope.context.mutation_lock),
                      "record_ids": sorted(identifier for identifier in scope.context.artifact_ids if identifier in context),
                      "record_calls": sorted(scope.context.artifact_calls), "excluded_env": sorted(scope.context.excluded_env),
                      "enabled_plugins": sorted(scope.enabled_plugins),
                      "plugin_snapshot": list(scope.plugin_snapshot),
                      "mcp_snapshot": delegated_snapshot(scope.mcp_snapshot),
                      "skill_roots": list(scope.context.skills.roots) if scope.context.skills is not None else [],
                      "text": "", "usage": None, "notice_pending": False}
            self._save(record)
            self._records[child_id] = record
            try:
                self._schedule(record, task + ("\n\n[父Agent提供的任务资料]\n" + context if context else ""), scope.run_id)
            except (OSError, ValueError) as exc:
                raise ToolError("subagent_launch_failed", "子运行未成功启动；核对记录后再决定重试。",
                                details={"child_id": child_id, "error_type": type(exc).__name__}) from exc
            return {**self.public(record), "accepted": True}

    def _schedule(self, record, message, parent_operation):
        """先持有父目录租约再返回受理；启动与管道写入不堵住GUI取状态。"""
        child_id = record["child_id"]
        run_id = record["run_id"]
        if self.tasks is not None:
            try:
                self.tasks.register(record["run_id"], "subagent", record["parent_id"],
                    lambda: self.cancel(record["parent_id"], child_id, run_id=run_id), workspace=Path(record["settings"]["workspace"]),
                    parent_operation=parent_operation)
            except (OSError, ValueError):
                record.update(status="failed", finished_at=utc_now(), error="父目录租约未接纳此子任务。")
                self._save(record)
                raise
        timer = threading.Timer(record["timeout_seconds"], self._timeout, args=(record["parent_id"], child_id, run_id))
        timer.daemon = True
        self._timers[child_id] = timer
        try:
            timer.start()
            threading.Thread(target=self._launch, args=(record, message), daemon=True).start()
        except BaseException:
            timer.cancel()
            self._timers.pop(child_id, None)
            if self.tasks is not None:
                self.tasks.release(record["run_id"])
            record.update(status="failed", finished_at=utc_now(), error="子启动线程未建立。")
            self._save(record)
            raise

    def _launch(self, record, message):
        """目录登记及Job挂载成功后才向子进程发送启动屏障。"""
        child_id = record["child_id"]
        tree = None
        process = None
        try:
            tree = ProcessTree()
            if record.get("cancel_requested"):
                raise asyncio.CancelledError
            process = subprocess.Popen(worker_command("subagent", ["-m", "desktop.runtime.subagents.runner"]),
                cwd=str(Path(__file__).resolve().parents[2]), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
            tree.attach(process.pid)
            with self._lock:
                self._processes[child_id] = process
                self._trees[child_id] = tree
                self._controls[child_id] = threading.Lock()
            with self._controls[child_id]:
                if record.get("cancel_requested"):
                    raise asyncio.CancelledError
                process.stdin.write(json.dumps({"type": "start", "record": record, "message": message,
                                                 "directory": str(self.directory.parent)}, ensure_ascii=False) + "\n")
                process.stdin.flush()
            threading.Thread(target=self._monitor, args=(child_id, process, tree), daemon=True).start()
        except BaseException:
            cleanup_ok = False
            try:
                if tree is not None:
                    tree.close()
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        if pipe is not None:
                            pipe.close()
                cleanup_ok = True
            except (OSError, subprocess.TimeoutExpired):
                pass
            with self._lock:
                timer = self._timers.pop(child_id, None)
                if timer:
                    timer.cancel()
                record.update(status=("timed_out" if record.get("cancel_reason") == "timeout" else "interrupted"
                                      if record.get("cancel_requested") else "failed") if cleanup_ok else "save_failed",
                              error="子执行器启动／退出未完整确认，请查看子日志。", finished_at=utc_now(),
                              cleanup_confirmed=cleanup_ok, notice_pending=cleanup_ok and record["target_id"] not in self._suppressed)
                if cleanup_ok:
                    self._processes.pop(child_id, None)
                    self._trees.pop(child_id, None)
                    self._controls.pop(child_id, None)
                    if self.tasks is not None:
                        self.tasks.release(record["run_id"])
                try:
                    self._save(record)
                except OSError:
                    record.update(status="save_failed", notice_pending=False)
                self._emit(record)

    def _monitor(self, child_id, process, tree):
        final = None
        def stderr():
            path = self.directory / child_id / "stderr.log"
            output = None
            try:
                try:
                    output = path.open("a", encoding="utf-8")
                except OSError:
                    self._records[child_id]["log_warning"] = "stderr记录保存失败，管道仍继续消费。"
                for line in process.stderr:
                    if output is not None:
                        try:
                            output.write(line)
                        except OSError:
                            output.close()
                            output = None
                            self._records[child_id]["log_warning"] = "stderr记录写入失败，管道仍继续消费。"
            finally:
                if output is not None:
                    output.close()
        reader = threading.Thread(target=stderr, daemon=True)
        reader_started = False
        try:
            reader.start()
            reader_started = True
            self._readers[child_id] = reader
            for line in process.stdout:
                frame = json.loads(line)
                if frame.get("child_id") != child_id:
                    raise ValueError("子进程事件归属不一致。")
                if frame["type"] == "finished":
                    final = frame
                    break
                if frame["type"] == "input":
                    with self._lock:
                        record = self._records[child_id]
                        record["input_ack"] = frame
                        self._save(record)
                if frame["type"] == "progress":
                    with self._lock:
                        if self._records[child_id]["status"] == "starting":
                            self._records[child_id]["status"] = "running"
                        event = frame["event"]
                        live = self._live.setdefault(child_id, {"text_parts": [], "blocks": {}, "version": 0})
                        if event["kind"] == "model_started" or (event["kind"] == "model_attempt" and (event.get("attempt") or {}).get("phase") == "started"):
                            live.update(text_parts=[], blocks={}, reasoning_active=False, running=True, step=event["step"])
                        elif event["kind"] == "text_delta":
                            live["text_parts"].append(event.get("text") or "")
                        elif event.get("reasoning"):
                            delta = event["reasoning"]
                            block = live["blocks"].setdefault(delta["block_id"], {"kind": delta["kind"], "parts": []})
                            block["parts"].append(delta.get("text") or "")
                            live["reasoning_active"] = True
                        if event["kind"] == "model_finished":
                            live.update(running=False, text_parts=[], blocks={}, reasoning_active=False)
                        live["version"] += 1
                self._emit(self._records[child_id])
            process.wait(timeout=5)
        except Exception as exc:
            final = {"status": "interrupted", "error": f"子执行器状态通道异常（{type(exc).__name__}）。"}
        finally:
            terminal = final or {"status": "interrupted", "error": "子进程退出但没有最终回执。"}
            cleanup_ok = False
            try:
                tree.close()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                if reader_started:
                    reader.join(timeout=5)
                if reader_started and reader.is_alive():
                    raise OSError("子日志管道尚未关闭。")
                cleanup_ok = True
            except Exception as exc:
                final = {"status": "save_failed", "error": f"回收未确认（{type(exc).__name__}）。"}
            with self._lock:
                record = self._records[child_id]
                record["cleanup_confirmed"] = cleanup_ok
                timer = self._timers.pop(child_id, None)
                if timer:
                    timer.cancel()
                final = final or {"status": "interrupted", "error": "子进程退出但没有最终回执。"}
                record.update(status=final["status"], error=final.get("error"), text=final.get("text", ""),
                              usage=final.get("usage"), finished_at=utc_now())
                if record["status"] == "save_failed":
                    record["retry_terminal"] = terminal
                if record.get("cancel_reason") == "timeout" and record["status"] != "save_failed":
                    record["status"] = "timed_out"
                record["notice_pending"] = record["target_id"] not in self._suppressed and record["status"] != "save_failed"
                try:
                    self._save(record)
                except OSError:
                    record.update(status="save_failed", notice_pending=False, error="子结果保存未确认，禁止自动续接。", retry_terminal=terminal)
                if cleanup_ok:
                    self._processes.pop(child_id, None)
                    self._trees.pop(child_id, None)
                    self._readers.pop(child_id, None)
                self._controls.pop(child_id, None)
                if self.tasks is not None and record["status"] != "save_failed":
                    self.tasks.release(record["run_id"])
                self._emit(record)
            if cleanup_ok:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()

    def _write_control(self, child_id, value, *, run_id=None):
        with self._lock:
            record = self._records.get(child_id)
            if record is None or run_id is not None and record["run_id"] != run_id:
                return False
            if value["type"] == "message" and record.get("cancel_requested"):
                return False
            process, control = self._processes.get(child_id), self._controls.get(child_id)
        if process is None or process.poll() is not None:
            return False
        if control is None:
            return False
        with control:
            process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            process.stdin.flush()
        return True

    def cancel(self, parent_id, child_id, *, run_id=None):
        with self._lock:
            record = self._owned(parent_id, child_id)
            if record["status"] not in ACTIVE or run_id is not None and record["run_id"] != run_id:
                return False
            run_id = record["run_id"]
            record.update(status="cancelling", notice_pending=False)
            record["cancel_requested"] = True
            self._save(record)
            def deliver():
                try:
                    self._write_control(child_id, {"type": "cancel"}, run_id=run_id)
                except (OSError, ValueError):
                    pass
            threading.Thread(target=deliver, daemon=True).start()
            process = self._processes.get(child_id)
            tree = self._trees.get(child_id)
            if process is not None:
                def force():
                    # 捕获本run的进程树，不在全局锁内等待，也不碰后来继续的新run。
                    if process.poll() is None:
                        try:
                            if tree is not None:
                                tree.close()
                        except OSError:
                            pass
                        finally:
                            if process.poll() is None:
                                try:
                                    process.kill()
                                except ProcessLookupError:
                                    pass
                timer = threading.Timer(5, force)
                timer.daemon = True
                timer.start()
            self._emit(record)
            return True

    def _timeout(self, parent_id, child_id, run_id):
        with self._lock:
            record = self._owned(parent_id, child_id)
            if record["status"] not in ACTIVE or record["run_id"] != run_id:
                return
            record["cancel_reason"] = "timeout"
            self.cancel(parent_id, child_id, run_id=run_id)

    def stop_parent(self, parent_id, target_id=None):
        with self._lock:
            values = [item for item in self._records.values() if item["parent_id"] == parent_id
                      and (target_id is None or item["target_id"] == target_id)]
            self._suppressed.update(item["target_id"] for item in values)
            if parent_id in self._parent_targets:
                self._suppressed.add(self._parent_targets[parent_id])
            if not self.load_errors:
                self._save_inbox()
            for item in values:
                item["notice_pending"] = False
                self._save(item)
                self.cancel(parent_id, item["child_id"])

    def notices(self, parent_id, *, wake=False):
        with self._lock:
            if self.load_errors:
                return []
            values = [item for item in self._records.values() if item["parent_id"] == parent_id and item["notice_pending"]
                      and item["target_id"] not in self._suppressed]
            if wake:
                values = values if self._wakes.get(self._parent_targets.get(parent_id), 0) < 3 else []
            return [{"event_id": item["run_id"], "child_id": item["child_id"], "target_id": item["target_id"],
                     "message": "[程序子任务结果]\n" + json.dumps({**self.public(item), "text": item["text"][:12000],
                         "truncated": len(item["text"]) > 12000, "usage": item.get("usage"),
                         "result_tool": "get_subagent_result"}, ensure_ascii=False)} for item in values]

    def acknowledge(self, parent_id, event_ids, *, wake=False):
        with self._lock:
            targets = set()
            for item in self._records.values():
                if item["parent_id"] == parent_id and item["run_id"] in event_ids:
                    item["notice_pending"] = False
                    self._save(item)
                    targets.add(item["target_id"])
            if wake:
                target = self._parent_targets.get(parent_id)
                if targets and target is not None:
                    self._wakes[target] = self._wakes.get(target, 0) + 1
                self._save_inbox()

    def retry_save(self, parent_id, child_id):
        """确认资源退出后补存原结果；不调用模型，不重做工具。"""
        with self._lock:
            record = self._owned(parent_id, child_id)
            if record["status"] != "save_failed":
                return
            process, tree, reader = self._processes.get(child_id), self._trees.get(child_id), self._readers.get(child_id)
            terminal = deepcopy(record.get("retry_terminal") or {"status": "interrupted", "error": "原执行器结果未确认。"})
        if process is not None:
            tree.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if reader is not None:
                reader.join(timeout=5)
                if reader.is_alive():
                    raise OSError("原日志管道仍未退出。")
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        from .conversations import ConversationApplication
        from ..runtime.conversations import ConversationSettings, ConversationStore
        from ..runtime.conversations.service import ConversationSaveError
        from ..runtime.agent.models import AgentResult
        from ..runtime.conversations.history import _step
        from ..llm.models import ModelRequest
        app = ConversationApplication(self.directory.parent, enable_subagents=False)
        app.store = ConversationStore(self.directory / "conversations")
        settings = ConversationSettings.from_dict(record["settings"])
        service = app._service(settings.config, settings.tool_names, network=False)
        if terminal.get("pending_result") is not None:
            raw = terminal["pending_result"]
            result = AgentResult(**{**raw, "initial_request": ModelRequest(**raw["initial_request"]),
                                  "steps": [_step(item) for item in raw["steps"]]})
            service.retry_save(ConversationSaveError(child_id, record["run_id"], result, terminal.get("pending")))
            terminal.update(status="completed" if result.stop_reason == "completed" else "interrupted",
                            text=result.text, usage=result.total_usage_summary, error=result.error)
        else:
            if not app.store.path(child_id).exists():
                app.store.create(settings, conversation_id=child_id)
                terminal.update(status="failed", error=terminal.get("error") or "子记录创建失败，未开始模型执行。")
            else:
                with app.store.writer(child_id) as journal:
                    service._recover(journal)
        with self._lock:
            record.update(status=terminal["status"], text=terminal.get("text", record["text"]),
                          usage=terminal.get("usage", record.get("usage")), error=terminal.get("error"), cleanup_confirmed=True,
                          notice_pending=record["target_id"] not in self._suppressed)
            record.pop("retry_terminal", None)
            try:
                self._save(record)
            except OSError:
                record.update(status="save_failed", notice_pending=False, retry_terminal=terminal)
                raise
            self._processes.pop(child_id, None)
            self._trees.pop(child_id, None)
            self._readers.pop(child_id, None)
            if self.tasks is not None:
                self.tasks.release(record["run_id"])
            self._emit(record)


class ScopedSubagents:
    def __init__(self, manager, parent_id, run_id, target_id, settings, context, profiles):
        self.manager, self.parent_id, self.run_id, self.target_id = manager, parent_id, run_id, target_id
        self.settings, self.context, self.profiles = deepcopy(settings), context, profiles
        self.enabled_plugins = frozenset()
        self.plugin_snapshot = ()
        self.mcp_snapshot = ()
        self.call_id = ""

    def for_call(self, call_id):
        from copy import copy
        value = copy(self)
        value.call_id = call_id
        return value

    async def dispatch(self, name, arguments):
        def text(key, maximum=65536):
            value = arguments.get(key, "")
            if not isinstance(value, str) or len(value.encode("utf-8")) > maximum or (key != "context" and not value.strip()):
                raise ToolError("invalid_arguments", f"{key}须为允许长度的非空文本。")
            return value
        if name == "subagent":
            return self.manager.start(self, text("profile_id", 100), text("task"), text("context"))
        if name == "list_subagents":
            return {"tasks": self.manager.list(self.parent_id)}
        if name == "wait_subagents":
            ids, duration = arguments.get("child_ids"), arguments.get("timeout_seconds", 30)
            if not isinstance(ids, list) or not 1 <= len(ids) <= 8 or any(not isinstance(item, str) for item in ids):
                raise ToolError("invalid_arguments", "等待须提供1～8个子任务ID。")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 <= duration <= 60:
                raise ToolError("invalid_arguments", "等待时限须在0～60秒之间。")
            deadline = time.monotonic() + duration
            while True:
                with self.manager._lock:
                    records = [self.manager._owned(self.parent_id, item) for item in ids]
                    done = [item for item in records if item["status"] not in ACTIVE]
                    if done:
                        results = [self._result(item, 0) for item in done]
                        return {"wait_expired": False, "results": results}
                if time.monotonic() >= deadline:
                    return {"wait_expired": True, "tasks": [self.manager.public(item) for item in records]}
                await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
        child_id = text("child_id", 100)
        with self.manager._lock:
            record = self.manager._owned(self.parent_id, child_id)
            if name == "cancel_subagent":
                return {"cancel_requested": self.manager.cancel(self.parent_id, child_id), **self.manager.public(record)}
            if name == "get_subagent_result":
                offset = arguments.get("offset", 0)
                if type(offset) is not int or offset < 0:
                    raise ToolError("invalid_arguments", "offset须为非负整数。")
                requested = arguments.get("run_id")
                if requested is not None and requested != record["run_id"]:
                    if not isinstance(requested, str):
                        raise ToolError("invalid_arguments", "run_id须为子运行编号。")
                    from ..runtime.conversations import ConversationStore
                    conversation = ConversationStore(self.manager.directory / "conversations").load_committed(child_id)
                    turn = next((item for item in conversation.turns if item.run_id == requested), None)
                    if turn is None or turn.finished_at is None:
                        raise ToolError("subagent_run_not_found", "子会话没有该已结束运行。")
                    last = turn.steps[-1] if turn.steps else None
                    record = {**record, "run_id": requested, "started_at": turn.started_at, "finished_at": turn.finished_at,
                        "status": "completed" if turn.stop_reason == "completed" else "interrupted", "error": turn.error,
                        "text": (last.assistant.text if last.assistant else last.partial_text) if last else "", "usage": turn.total_usage_summary}
                return self._result(record, offset)
            if name == "send_subagent":
                if Path(record["settings"]["workspace"]).resolve() != self.context.workspace.resolve() or set(record["settings"]["tool_names"]) - self.context.allowed_tools:
                    raise ToolError("subagent_scope_changed", "当前父运行不能继续子任务原目录／工具范围。")
                if record["accept_call_id"] not in self.context.artifact_calls:
                    raise ToolError("subagent_origin_missing", "子任务的原受理调用不在当前有效历史中。")
                message = text("message") + ("\n\n[父Agent资料]\n" + text("context") if arguments.get("context") else "")
                if record["status"] in {"save_failed", "cancelling"} or self.target_id in self.manager._suppressed:
                    raise ToolError("subagent_unavailable", "子记录保存未确认或父目标已停止。")
                if record["status"] in ACTIVE:
                    input_id = uuid4().hex
                else:
                    if sum(item["status"] in ACTIVE for item in self.manager._records.values()) >= 8 or sum(
                            item["parent_id"] == self.parent_id and item["status"] in ACTIVE for item in self.manager._records.values()) >= 2:
                        raise ToolError("subagent_concurrency_limit", "并发额度不足，未继续子会话。")
                    record.update(run_id=uuid4().hex, status="starting", started_at=utc_now(), finished_at=None,
                                  target_id=self.target_id, text="", error=None, notice_pending=False, cancel_requested=False)
                    record.pop("cancel_reason", None)
                    record.pop("input_ack", None)
                    self.manager._save(record)
                    self.manager._schedule(record, message, self.run_id)
                    input_id = None
                response = {**self.manager.public(record), "accepted": True, "input_id": input_id}
        if input_id is not None:
            deadline = time.monotonic() + 5
            delivered = threading.Event()
            delivery = {}
            def write_message():
                try:
                    delivery["written"] = self.manager._write_control(child_id,
                        {"type": "message", "input_id": input_id, "text": message}, run_id=response["run_id"])
                except (OSError, ValueError) as exc:
                    delivery["error_type"] = type(exc).__name__
                finally:
                    delivered.set()
            # 管道阻塞不能占全局状态锁；线程不属于父asyncio.run的默认执行器。
            try:
                threading.Thread(target=write_message, daemon=True).start()
            except RuntimeError as exc:
                raise ToolError("subagent_input_unconfirmed", "补充投递线程未建立，原子记录保留。",
                                details={"child_id": child_id, "run_id": response["run_id"], "input_id": input_id}) from exc
            while time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                with self.manager._lock:
                    if record["run_id"] != response["run_id"]:
                        break
                    ack = record.get("input_ack", {})
                    if ack.get("input_id") == input_id:
                        if not ack.get("accepted"):
                            raise ToolError("subagent_input_rejected", ack.get("error") or "子任务未接纳这条消息。")
                        return response
                if delivered.is_set() and not delivery.get("written"):
                    break
            raise ToolError("subagent_input_unconfirmed", "补充受理尚未确认，请核对子日志，不要重复投递。",
                            details={"child_id": child_id, "run_id": response["run_id"], "input_id": input_id,
                                     "write_finished": delivered.is_set(), **(delivery if delivered.is_set() else {})})
        return response

    def _result(self, record, offset):
        text = record["text"]
        end = min(len(text), offset + 12000)
        if record["status"] not in ACTIVE:
            self.manager.acknowledge(self.parent_id, [record["run_id"]])
        return {**self.manager.public(record), "text": text[offset:end], "total_chars": len(text),
                "next_offset": end if end < len(text) else None, "out_of_range": offset > len(text), "usage": record.get("usage")}


class SubagentViews:
    """Qt页面只有查询、已知子任务停止与只读日志入口，没有启动或模型发送。"""
    def __init__(self, manager):
        self._manager = manager

    def list(self, parent_id):
        return self._manager.list(parent_id)

    def _path(self, parent_id, child_id):
        with self._manager._lock:
            self._manager._owned(parent_id, child_id)
        return self._manager.directory / "conversations" / (UUID(child_id).hex + ".jsonl")

    def stamp(self, parent_id, child_id):
        value = self._path(parent_id, child_id).stat()
        return value.st_size, value.st_mtime_ns

    def conversation(self, parent_id, child_id):
        from ..runtime.conversations import ConversationStore
        self._path(parent_id, child_id)
        return ConversationStore(self._manager.directory / "conversations").load_committed(child_id)

    def live(self, parent_id, child_id):
        with self._manager._lock:
            self._manager._owned(parent_id, child_id)
            live = self._manager._live.get(child_id)
            if not live:
                return {}
            if live.get("render_version") != live["version"]:
                live["rendered"] = {"version": live["version"], "step": live.get("step"), "text": "".join(live["text_parts"]),
                    "reasoning_active": live.get("reasoning_active"), "blocks": [
                        {"block_id": key, "kind": value["kind"], "text": "".join(value["parts"])} for key, value in live["blocks"].items()]}
                live["render_version"] = live["version"]
            return deepcopy(live["rendered"])

    def errors(self):
        return (*self._manager.load_errors, *((self._manager.configuration_error,) if self._manager.configuration_error else ()))

    def cancel(self, parent_id, child_id):
        return self._manager.cancel(parent_id, child_id)

    def retry_save(self, parent_id, child_id):
        return self._manager.retry_save(parent_id, child_id)
