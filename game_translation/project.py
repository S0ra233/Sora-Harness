"""Persistent Scene/Task/Entry index, shared by the Skill and future desktop UI.

Entries describe occurrences. Tasks own mutable translation state. Scenes own
ordered task IDs. JSON dictionaries deliberately match the on-disk API.
Public operations are synchronous: callers must serialize writes to one work dir.
"""
from collections import Counter, defaultdict
import json
from pathlib import Path
import uuid

from .engines import adapter, detect
from .extraction import ExtractionResult, extract_resources, summarize_source_context
from .glossary import initialize
from .storage import (file_hash, load_tasks, read_file, safe_join, save_tasks,
                      separate_directories, stable_id, write_file, write_json)
from .text import protect_tags
from .prepare_options import resolve_adapter_file, resolve_options
from .candidates import select_candidates
from .localization import align_localization, select_missing, select_text_sources
from .process_control import check_cancelled

SCHEMA_VERSION = 3


def index_entries(entries, family):
    """Core creates all identities; adapters supply only locations and grouping."""
    tasks, scenes = [], {}
    scene_entries = defaultdict(list)
    seen = set()
    for entry in entries:
        location = [family, entry["file"], entry.get("asset"), entry["path"]]
        identity = stable_id(location)
        if identity in seen:
            raise ValueError("适配器产生重复文本定位")
        seen.add(identity)
        sid = "s_" + stable_id([family, entry.get("scene", entry["file"])])
        tid = "t_" + identity
        entry.update(entry_id="e_" + identity, task_id=tid, scene_id=sid)
        scene = scenes.setdefault(sid, {"id": sid,
            "title": entry.get("scene_title", entry.get("scene", entry["file"])),
            "kind": entry.get("scene_kind", "database"), "task_ids": []})
        protected, codes = protect_tags(entry["text"],
                                        syntax=entry.get("structure", {}).get("text_syntax"))
        source = {"text": entry["text"], "type": entry["type"],
                  "speaker": entry.get("speaker", ""), "structure": entry.get("structure", {})}
        task = {"id": tid, "scene_id": sid, "order": len(scene["task_ids"]), **source,
                "source_hash": stable_id({**source, "target_original": entry["original"]}
                                         if "original" in entry else source),
                "protected_text": protected, "codes": codes,
                "translation": "", "status": "NONE", "revision": 0}
        tasks.append(task)
        scene["task_ids"].append(tid)
        scene_entries[sid].append(entry)
    by_id = {t["id"]: t for t in tasks}
    for scene in scenes.values():
        group = scene_entries[scene["id"]]
        if group[0].get("source_context"):
            scene["source_context"] = summarize_source_context(group)
        context_hash = stable_id([scene, [by_id[t]["source_hash"] for t in scene["task_ids"]]])
        for tid in scene["task_ids"]:
            by_id[tid]["context_hash"] = context_hash
    return tasks, list(scenes.values())


def prepare(game_dir, work_dir, *, source_locale=None, target_locale=None,
            text_locale=None, text_overrides=None, adapter_file=None, candidate_selection=None,
            godot_executable=None):
    check_cancelled()
    game, work = Path(game_dir).resolve(), Path(work_dir).resolve()
    previous_manifest, previous, adapter_file = _prepare_workspace(game, work, adapter_file)
    options = resolve_options(game, previous_manifest, adapter_file=adapter_file,
        candidate_selection=candidate_selection, text_locale=text_locale,
        text_overrides=text_overrides, source_locale=source_locale, target_locale=target_locale)
    info = _detect_for_prepare(game, adapter_file, previous_manifest, options.candidates, godot_executable)
    engine = adapter(info)
    if options.candidates and info["family"] not in ("unity", "wolf", "godot"):
        raise ValueError("候选选择目前只支持 Unity、WOLF 和 Godot")
    resources = engine.prepare(info, work)
    check_cancelled()
    result, extraction, localization = _extract_entries(engine, info, resources, options)
    check_cancelled()
    entries, warnings = result.entries, result.warnings
    if extraction["resources"] or options.text_selection:
        write_json(work / "extraction_report.json", extraction)
        warnings.append("语言选择及技术资源/未知候选见 extraction_report.json；不代表游戏文本已全部覆盖")
    if not entries:
        return _empty_extraction(work, info, options, warnings, extraction, localization)
    tasks, scenes = index_entries(entries, info["family"])
    _restore_task_state(tasks, previous)
    manifest = _prepare_manifest(game, info, resources, result, scenes, previous_manifest, options)
    # 发布项目索引前再检查停止；已经产生的解包文件不删除。
    check_cancelled()
    _save_prepared_project(work, manifest, entries, tasks, previous, localization)
    return _status_summary(manifest, entries, tasks)


def _detect_for_prepare(game, adapter_file, previous, candidates, godot_executable):
    info = detect(game, adapter_file=adapter_file, godot_executable=godot_executable)
    expected = (candidates or {}).get("godot_source")
    old_engine = (previous or {}).get("engine", {})
    old_godot = old_engine.get("family") == "godot" and not old_engine.get("temporary_adapter")
    if info["family"] != "godot" or info.get("temporary_adapter"):
        if expected is not None or old_godot:
            raise ValueError("Godot 来源记录与当前检测引擎不一致，请使用新工作目录或重新预览")
        return info
    engine = adapter(info)
    old_source = engine.source_identity(old_engine) if old_godot else None
    if old_source is not None and "executable" not in old_engine:
        # Older projects recorded a pack, without recording its launcher.
        old_source.pop("executable")
    engine.select_source(info, godot_executable, expected=expected if expected is not None else old_source)
    if old_source is not None and any(engine.source_identity(info).get(key) != value
                                      for key, value in old_source.items()):
        raise ValueError("不能改变已有项目的 Godot 启动入口或主包；请使用新工作目录")
    return info


def _prepare_workspace(game, work, adapter_file):
    separate_directories(game, work)
    previous = json.loads(read_file(work / "project.json") or "null")
    adapter_file = resolve_adapter_file(work, previous, adapter_file)
    if previous:
        if previous.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("工作目录版本不兼容，请为重构版使用新的工作目录")
        if Path(previous["game_dir"]).resolve() != game:
            raise ValueError("工作目录属于另一个游戏")
    elif work.exists() and any(p.resolve() != adapter_file for p in work.iterdir()):
        raise ValueError("工作目录非空且没有项目记录，请使用空目录")
    return previous, load_tasks(work / "tasks.jsonl"), adapter_file


def _extract_localization(engine, info, selection):
    if not hasattr(engine, "inspect_localization") or not hasattr(engine, "localization_entries"):
        raise ValueError("此引擎尚不支持补全已有语言")
    inventory = engine.inspect_localization(info)
    report = align_localization(inventory)
    selected, skipped = select_missing(report, **selection)
    report["selection"] = {**selection, "selected": len(selected), "skipped": skipped}
    entries = engine.localization_entries(selected, **selection)
    warnings = ([f"多语言检查有 {len(report['issues'])} 项问题；只处理可靠对齐的条目，详见 localization_report.json"]
                if report["issues"] else [])
    dependencies = set(inventory.get("dependencies", []))
    dependencies.update(e["source_location"]["file"] for e in entries)
    dependencies.update(e["shared_file"] for e in entries)
    return ExtractionResult(entries, warnings, dependencies=dependencies), report


def _extract_entries(engine, info, resources, options):
    if options.candidates:
        resources["collect_candidates"] = True
    localization = None
    if options.localization:
        result, localization = _extract_localization(engine, info, options.localization)
    else:
        result = extract_resources(engine, info, resources)
    if options.candidates:
        result.entries = select_candidates(result, options.candidates, resources["root"])
    extraction = {"text_selection": options.text_selection,
                  "resources": result.reports, "sources": []}
    if options.text_selection:
        result.entries, extraction["sources"] = select_text_sources(
            result.entries, options.text_selection["locale"], options.text_selection["overrides"])
    extraction["selected_entries"] = len(result.entries)
    return result, extraction, localization


def _empty_extraction(work, info, options, warnings, extraction, localization):
    result = {"project_updated": False, "entries": 0, "tasks": 0, "warnings": warnings}
    if options.text_selection:
        return {**result, "extraction": extraction,
                "note": "没有匹配且可确认的文本来源；未创建或更新项目"}
    if localization is not None:
        # Disappearing language gaps must not overwrite an existing project.
        return {**result, "localization": localization,
                "note": "没有可补全条目；未创建或更新项目"}
    write_json(work / "prepare_diagnostics.json", {
        "game_dir": info["game_dir"], "engine": info, "entries": 0, "warnings": warnings})
    raise ValueError("没有提取到文本；诊断已保存到 prepare_diagnostics.json。"
                     "请运行 inspect GAME --work WORK 收集处理线索；不能将未支持资源当作提取成功")


def _restore_task_state(tasks, previous):
    old_tasks = {t["id"]: t for t in previous}
    for task in tasks:
        old = old_tasks.get(task["id"])
        if not old:
            continue
        same = all(old.get(k) == task[k] for k in ("source_hash", "context_hash"))
        task.update(translation=old.get("translation", ""), revision=old.get("revision", 0))
        if same:
            for field in ("status", "review_reason", "last_submission"):
                if field in old:
                    task[field] = old[field]
        else:
            task["revision"] += 1
            if task["translation"]:
                task.update(status="NEEDS_HUMAN", review_reason="原文或场景上下文改变，请重新审核")


def _prepare_manifest(game, info, resources, result, scenes, previous_manifest, options):
    manifest = {"schema_version": SCHEMA_VERSION,
                "project_id": previous_manifest["project_id"] if previous_manifest else uuid.uuid4().hex,
                "game_dir": str(game), "resource_root": resources["root"], "engine": info,
                "scenes": scenes, "warnings": result.warnings, "entries_hash": stable_id(result.entries),
                "source_hashes": {name: file_hash(safe_join(resources["root"], name))
                                  for name in sorted(result.source_files())}}
    if options.localization:
        manifest["localization"] = options.localization
    if options.text_selection:
        manifest["text_selection"] = options.text_selection
    if options.candidates:
        manifest["candidate_selection"] = options.candidates
    if info.get("archive"):
        manifest["archive_hash"] = file_hash(info["archive"])
    if resources.get("input_files"):
        manifest["input_hashes"] = {name: file_hash(safe_join(game, name))
                                    for name in resources["input_files"]}
    return manifest


def _save_prepared_project(work, manifest, entries, tasks, previous, localization_report):
    work.mkdir(parents=True, exist_ok=True)
    if previous:
        write_file(work / "tasks.previous.jsonl", read_file(work / "tasks.jsonl"))
    initialize(work, tasks, target_locale=manifest.get("localization", {}).get("target_locale"))
    if localization_report is not None:
        write_json(work / "localization_report.json", localization_report)
    if manifest.get("candidate_selection"):
        write_json(work / "candidate_selection.json", manifest["candidate_selection"])
    write_json(work / "entries.json", entries)
    save_tasks(work / "tasks.jsonl", tasks)
    # Last file is a commit marker: load_project rejects incomplete index updates.
    write_json(work / "project.json", manifest)


def load_project(work_dir):
    work = Path(work_dir).resolve()
    manifest = json.loads(read_file(work / "project.json") or "null")
    if not manifest or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("缺少版本 3 项目，请先 prepare 到独立工作目录")
    entries = json.loads(read_file(work / "entries.json") or "null")
    if not isinstance(entries, list) or not entries or stable_id(entries) != manifest["entries_hash"]:
        raise ValueError("文本定位索引与项目记录不一致，请重新 prepare")
    tasks = load_tasks(work / "tasks.jsonl")
    expected, scenes = index_entries([dict(e) for e in entries], manifest["engine"]["family"])
    if scenes != manifest["scenes"] or [t["id"] for t in tasks] != [t["id"] for t in expected]:
        raise ValueError("任务或 Scene 与定位索引不匹配")
    mutable = {"translation", "status", "revision"}
    for actual, original in zip(tasks, expected):
        if any(actual.get(k) != v for k, v in original.items() if k not in mutable):
            raise ValueError("任务原文或上下文被修改，请通过 prepare 更新")
        if (not isinstance(actual.get("translation"), str) or
                type(actual.get("revision")) is not int or actual["revision"] < 0):
            raise ValueError("任务译文或版本无效")
    return manifest, entries, tasks


def save_state(work, tasks):
    """One atomic mutable-state write, with the immediately previous version."""
    work = Path(work)
    write_file(work / "tasks.previous.jsonl", read_file(work / "tasks.jsonl"))
    save_tasks(work / "tasks.jsonl", tasks)


def status(work_dir):
    manifest, entries, tasks = load_project(work_dir)
    return _status_summary(manifest, entries, tasks)


def _status_summary(manifest, entries, tasks):
    by_id = {t["id"]: t for t in tasks}
    return {"project_id": manifest["project_id"], "engine": manifest["engine"],
            "text_selection": manifest.get("text_selection"),
            "localization": manifest.get("localization"),
            "entries": len(entries), "tasks": len(tasks),
            "counts": dict(Counter(t["status"] for t in tasks)),
            "scenes": [{**s, "counts": dict(Counter(by_id[t]["status"] for t in s["task_ids"]))}
                       for s in manifest["scenes"]], "warnings": manifest["warnings"]}
