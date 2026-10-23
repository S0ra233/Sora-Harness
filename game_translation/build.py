"""Build a new game copy and read back each indexed value before publishing it."""
from collections import defaultdict
from pathlib import Path
import shutil
import sys
import tempfile

from .engines import adapter
from .engines.build_steps import prepare_build, configure_fonts
from .project import load_project
from .storage import file_hash, safe_join, separate_directories, write_json
from .translation import approved_translations


def _verify(output, manifest, entries, tasks, approved, work=None):
    groups = defaultdict(list)
    for entry in entries:
        groups[entry["file"]].append(entry)
    engine = adapter(manifest["engine"])
    report = {"output_dir": str(output), "references": len(entries), "matched": 0,
              "missing": [], "mismatches": [], "unchanged": [],
              "warnings": manifest["warnings"], "runtime_verified": False,
              "approved_tasks": len(approved), "total_tasks": len(tasks)}
    values_by_file, batch_error = None, None
    if hasattr(engine, "read_many"):
        try:
            values_by_file = engine.read_many(output, groups, work)
        except Exception as exc:
            batch_error = str(exc)
    for file, group in groups.items():
        try:
            if batch_error is not None:
                raise ValueError(batch_error)
            values = values_by_file[file] if values_by_file is not None else engine.read(safe_join(output, file), group)
            if len(values) != len(group):
                raise ValueError("适配器返回数量与索引数量不同")
            for entry, actual in zip(group, values):
                original = entry.get("original", entry["text"])
                expected = approved.get(entry["task_id"], original)
                if expected == actual:
                    report["matched"] += 1
                else:
                    report["mismatches"].append({"file": file, "path": entry["path"],
                                                 "expected": expected, "actual": actual})
                if actual == original:
                    report["unchanged"].append(entry["entry_id"])
        except Exception as exc:
            report["missing"].append({"file": file, "reason": str(exc)})
    report["indexed_values_match"] = report["matched"] == len(entries)
    report["all_tasks_approved"] = len(approved) == len(tasks)
    return report


def verify(work_dir, output_dir):
    work, output = Path(work_dir).resolve(), Path(output_dir).resolve()
    manifest, entries, tasks = load_project(work)
    approved, _ = approved_translations(tasks, work)
    report = _verify(output, manifest, entries, tasks, approved, work)
    engine = adapter(manifest["engine"])
    if hasattr(engine, "check_build"):
        changed = sorted({e["file"] for e in entries if e["task_id"] in approved})
        report["load_checks"] = engine.check_build(output, manifest["engine"], changed)
    write_json(work / "verify_report.json", report)
    return report


def build(work_dir, output_dir, *, allow_partial=False, font=None, tmp_font=None, font_plan=None):
    work, output = Path(work_dir).resolve(), Path(output_dir).resolve()
    manifest, entries, tasks = load_project(work)
    game = Path(manifest["game_dir"]).resolve()
    separate_directories(game, work)
    separate_directories(game, output)
    separate_directories(work, output)
    if output.exists():
        raise FileExistsError("输出目录已存在，请指定新目录")
    if sum(x is not None for x in (font, tmp_font, font_plan)) > 1:
        raise ValueError("font、tmp_font、font_plan 不能同时提供")
    resources, info, archive = _check_sources(manifest, game, work)
    approved, blocked = approved_translations(tasks, work)
    if blocked and not allow_partial:
        raise ValueError(f"{len(blocked)} 个任务未通过审核；处理后重试，或明确使用 --allow-partial")
    if not approved:
        raise ValueError("没有可写回的已审核译文")
    if any(e.get("write_supported") is False and e["task_id"] in approved for e in entries):
        raise ValueError("选中译文包含暂不支持写回的条目；请重新 prepare 后检查支持状态，未创建输出副本")
    groups = defaultdict(list)
    for entry in entries:
        if entry["task_id"] in approved:
            groups[entry["file"]].append((entry, approved[entry["task_id"]]))
    engine = adapter(info)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"success": False, "output_dir": str(output), "written": 0, "blocked": blocked,
              "stage": "preflight", "retained_dir": None, "runtime_verified": False}
    staging = None
    try:
        checks = prepare_build(engine, game, info, sorted(groups), font_plan)
        if checks is not None:
            report["load_checks"] = checks
            if not checks["can_build"]:
                raise ValueError("引擎加载关系检查未通过，未创建游戏副本；详见 build_report.json 的 load_checks")
        # mkdtemp has no automatic cleanup. Failures retain even partially copied
        # files; successful builds rename this same directory to the final output.
        staging = Path(tempfile.mkdtemp(prefix=".game-translation-build-", dir=output.parent))
        report.update(stage="copy", retained_dir=str(staging))

        if hasattr(engine, "copy_resources"):
            engine.copy_resources(game, resources, staging, info)
        else:
            _copy_game(game, resources, staging, archive)
        report["stage"] = "write"
        _write_translations(engine, staging, groups, report, work)
        report["stage"] = "fonts"
        report["fonts"] = configure_fonts(engine, staging, info, font, tmp_font, font_plan)
        if hasattr(engine, "finalize_build"):
            report["stage"] = "catalogs"
            report["load_checks"] = engine.finalize_build(staging, info, report["load_checks"])
        report["stage"] = "readback"
        checks = _verify(staging, manifest, entries, tasks, approved, work)
        checks["output_dir"] = str(output)
        if "load_checks" in report:
            checks["load_checks"] = report["load_checks"]
        report["verification"] = checks
        if not checks["indexed_values_match"]:
            raise ValueError("写回后重新读取的文本与预期不一致；详见 build_report.json")
        report["stage"] = "publish"
        _publish_directory(staging, output)
        report.update(success=True, stage="report", retained_dir=None)
        write_json(work / "verify_report.json", checks)
        write_json(work / "build_report.json", {**report, "stage": "complete"})
        report["stage"] = "complete"
    except BaseException as exc:
        _record_failure(work, staging, report, exc)
        raise
    return report


def _publish_directory(staging, output):
    if output.exists():
        raise FileExistsError("构建过程中输出目录被创建，请换新目录")
    import gc
    import time
    gc.collect()
    for attempt in range(5):
        try:
            staging.rename(output)
            return
        except PermissionError:
            if attempt == 4:
                raise
            gc.collect()
            time.sleep(0.3)


def _check_sources(manifest, game, work):
    resources = Path(manifest["resource_root"]).resolve()
    if resources != game and work not in resources.parents:
        raise ValueError("解包资源必须在当前工作目录内")
    for name, expected in manifest["source_hashes"].items():
        if file_hash(safe_join(resources, name)) != expected:
            raise ValueError(f"源资源已改变，请重新 prepare: {name}")
    info = manifest["engine"]
    archive = Path(info["archive"]).resolve() if info.get("archive") else None
    if archive and file_hash(archive) != manifest["archive_hash"]:
        raise ValueError("源归档已改变，请重新 prepare")
    for name, expected in manifest.get("input_hashes", {}).items():
        if file_hash(safe_join(game, name)) != expected:
            raise ValueError(f"原游戏输入资源已改变，请重新 prepare: {name}")
    return resources, info, archive


def _win_long(path):
    s = str(Path(path).resolve())
    if sys.platform != "win32" or s.startswith("\\\\?\\"):
        return s
    # UNC shares use a separate namespace from local drive paths.
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def _copy_game(game, resources, staging, archive):
    game_path = Path(_win_long(game))

    def ignore_archive(folder, names):
        return [archive.name] if archive and Path(_win_long(folder)) == game_path else []

    shutil.copytree(game_path, _win_long(staging), ignore=ignore_archive, dirs_exist_ok=True)
    if archive:
        shutil.copytree(_win_long(resources), _win_long(staging), dirs_exist_ok=True)


def _write_translations(engine, staging, groups, report, work=None):
    if hasattr(engine, "write_many"):
        report["written"] += engine.write_many(staging, groups, work)
        return
    for name, items in groups.items():
        report["active_file"] = name
        report["written"] += engine.write(safe_join(staging, name), items)
    report.pop("active_file", None)


def _record_failure(work, staging, report, exc):
    report["error"] = str(exc)
    report["error_type"] = type(exc).__name__
    if staging is not None and staging.exists():
        report["retained_dir"] = str(staging)
    exc.build_report = report
    try:
        write_json(work / "build_report.json", report)
        exc.build_report_path = str(work / "build_report.json")
    except Exception as report_error:
        # Do not replace the actual copy/write failure with a reporting error.
        if hasattr(exc, "add_note"):
            exc.add_note(f"构建报告保存失败: {report_error}；保留目录: {report['retained_dir']}")

