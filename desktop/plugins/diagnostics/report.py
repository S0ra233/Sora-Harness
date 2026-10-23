"""从已保存事实生成只读诊断；不发模型请求、不把统计写回对话。"""
import json
from pathlib import Path
from uuid import UUID

from ...runtime.conversations.store import ConversationStore
from ...runtime.conversations.history import build_history
from ...runtime.context.budget import estimate_message_tokens
from ...runtime.context.budget import effective_window
from ...llm.messages import ModelMessage
from ...prompts.assembly import build_prompt_prefix
from ...application.usage_sample import from_steps, usage_after_step, cache_percentage, request_seconds


def _metadata(path, limit=256 * 1024):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("诊断清单过大或是链接，未读取。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("诊断清单须为对象。")
    return data


def _request_records(root, conversation):
    rows, warnings = [], []
    directory = root / "request-records"
    # 只查看当前会话最近十轮的按需记录，不读取请求正文、图片编码或认证信息。
    for turn in conversation.turns[-10:]:
        run = directory / UUID(turn.run_id).hex
        if not run.is_dir() or run.is_symlink():
            continue
        try:
            if _metadata(run / "run.json").get("conversation_id") != conversation.conversation_id:
                continue
            for index, manifest in enumerate(run.glob("*/manifest.json")):
                if index == 300:
                    warnings.append("单轮请求记录仅查看前300份。")
                    break
                if manifest.parent.is_symlink():
                    continue
                data = _metadata(manifest)
                if data.get("conversation_id") != conversation.conversation_id:
                    continue
                rows.append({key: data.get(key) for key in ("request_id", "run_id", "purpose", "step", "api_style", "model", "status", "created_at", "bytes", "components", "unified_request_view")})
        except (OSError, ValueError, TypeError) as exc:
            warnings.append(f"运行 {turn.run_id} 的请求诊断未读取：{type(exc).__name__}。")
    rows.sort(key=lambda row: row.get("created_at") or "")
    previous = None
    for row in rows:
        row["observed_changes"] = []
        if previous and (previous["api_style"], previous["model"]) == (row["api_style"], row["model"]):
            old, new = previous.get("components") or {}, row.get("components") or {}
            row["observed_changes"] = [key for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)]
        previous = row
    return rows, warnings


def conversation_report(data_root, conversation_id):
    root = Path(data_root)
    conversation = ConversationStore(root / "conversations").load_committed(conversation_id)
    rows, requests = [], []
    boundary = conversation.latest_compaction
    target = conversation.last_options.model_target
    config = target.config if target is not None else conversation.settings.config
    window = effective_window(config.context_window, conversation.settings.context_policy.context_window)
    latest_sample = None
    for turn in conversation.turns:
        after = usage_after_step(turn.run_id, turn.started_at, boundary)
        sample = from_steps(turn.steps, window=window, after_step=after)
        if sample.latest is not None:
            latest_sample = sample
        children = []
        for step in turn.steps:
            usage = step.token_usage
            if usage is not None:
                requests.append(usage)
            calls = {call.call_id: call.name for call in step.assistant.tool_calls} if step.assistant else {}
            tools = [{"title": calls.get(tool.call_id, tool.call_id), "status": tool.status,
                      "details": {"call_id": tool.call_id, "entered_handler": tool.handler_started,
                                  "dispatch_seconds": tool.elapsed_seconds, "handler_seconds": tool.handler_seconds,
                                  "note": "调度时长包含许可等待；旧记录未记录耗时则为null。"}} for tool in step.tools]
            children.append({"title": f"模型步骤 {step.step}", "status": step.completion_status,
                "details": {"actual_usage": vars(usage) if usage else None, "successful_request_seconds": request_seconds(step.attempts),
                            "attempts": [{"attempt": item.attempt, "phase": item.phase, "seconds": item.duration_seconds} for item in step.attempts]},
                "children": tools})
        rows.append({"title": f"运行 {turn.run_id[:12]}", "status": turn.stop_reason,
                     "details": {"run_id": turn.run_id, "started_at": turn.started_at, "finished_at": turn.finished_at}, "children": children})
    def paired(usage):
        return usage.input_tokens is not None and usage.cache_read_tokens is not None and 0 <= usage.cache_read_tokens <= usage.input_tokens
    known = [usage for usage in requests if paired(usage)]
    latest = latest_sample.latest.usage if latest_sample else None
    summary = {"conversation_id": conversation.conversation_id, "preset": conversation.current_preset_name,
               "latest_actual_input_after_compaction": latest.input_tokens if latest else None,
               "configured_window": window,
               "latest_run_cache": cache_percentage(latest_sample.cache_read, latest_sample.cache_input) if latest_sample else "—",
               "history_reported_usage_steps": len(requests), "history_paired_cache_steps": len(known),
               "paired_history_cache_ratio": cache_percentage(sum(value.cache_read_tokens for value in known), sum(value.input_tokens for value in known)),
               "note": "历史配对比例仅覆盖已报告输入与命中的样本，不是全程消耗；摘要与子任务另列。压缩前输入不计当前窗口。"}
    estimates = {"basis": "本地粗估；不是计费值，不包含尚未装配的当前工具声明。"}
    try:
        history = build_history(conversation, config=config)
        prefix = build_prompt_prefix(conversation.current_prompt, prompt_mode=conversation.settings.prompt_mode,
                                     system_messages=[ModelMessage("system", conversation.settings.system)] if conversation.settings.system else [])
        estimates.update(effective_history_messages=len(history), history_tokens=sum(map(estimate_message_tokens, history)),
                         rules_and_prelude_tokens=sum(map(estimate_message_tokens, prefix)),
                         environment_baseline_tokens=estimate_message_tokens(conversation.runtime_baseline) if conversation.runtime_baseline else None)
    except ValueError:
        estimates["unavailable"] = "当前运行尚未结算，未模拟恢复或拼接未闭合工具历史。"
    compactions = [{"title": f"压缩 {record.compaction_id[:12]}", "status": record.status,
                   "details": {"covered_turns": record.covered_turns, "actual_summary_usage": vars(record.token_usage) if record.token_usage else None,
                               "summary_requests": [{key: value.get(key) for key in ("token_usage", "completion_status", "finish_reason", "duration_seconds")} for value in record.summary_requests],
                               "note": "这是生成摘要的消耗，不是压缩后上下文占用。"}} for record in conversation.compactions[-20:]]
    records, warnings = _request_records(root, conversation)
    children = []
    for index, path in enumerate((root / "subagents").glob("*/state.json")):
        if index >= 1000:
            warnings.append("子任务状态仅扫描前1000份。")
            break
        try:
            state = _metadata(path, limit=2 * 1024 * 1024)
            if state.get("parent_id") == conversation.conversation_id:
                children.append({"title": state.get("name", "子任务"), "status": state.get("status", "未知"),
                                 "details": {key: state.get(key) for key in ("child_id", "run_id", "model", "started_at", "finished_at", "usage")}})
        except (OSError, ValueError):
            warnings.append("一份子任务状态不可读，未推断其消耗。")
    return {"summary": summary, "estimates": estimates, "warnings": warnings,
            "groups": [{"title": "最近运行（最多50轮）", "children": rows[-50:]},
                       {"title": "摘要请求", "children": compactions}, {"title": "子任务（独立用量）", "children": children},
                       {"title": "按需请求记录", "children": [{"title": f"{item.get('purpose')} · {item.get('request_id')}", "details": item} for item in records]}]}
