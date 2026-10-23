"""Review readable engine text by source/field before creating translation Tasks."""
from collections import defaultdict
from pathlib import Path

from .engines import adapter, detect
from .extraction import extract_resources, representative_entries, summarize_source_context
from .storage import file_hash, safe_join, stable_id, separate_directories


def group_entries(entries):
    groups = {}
    members = defaultdict(list)
    for entry in entries:
        context = entry.get("source_context", {})
        if context.get("engine") in {"unity", "godot"}:
            identity = {key: context[key] for key in ("file", "asset", "format", "field_group", "text_locale")}
        else:
            identity = _legacy_group_identity(entry)
        gid = "cg_" + stable_id(identity)
        group = groups.setdefault(gid, {"id": gid, **identity, "count": 0, "samples": []})
        if entry.get("write_supported") is False:
            group["write_supported"] = False
        group["count"] += 1
        members[gid].append(entry)
    for gid, group in groups.items():
        group_members = members[gid]
        first = group_members[0]
        family = first.get("source_context", {}).get("engine")
        if family in {"unity", "godot"}:
            group.update(scene_id="s_" + stable_id([family, first["scene"]]),
                         scene_title=first["scene_title"],
                         source_context=summarize_source_context(group_members))
            samples = representative_entries(group_members)
        else:
            samples = group_members[:3]
        group["samples"] = [{"path": entry["path"], "text": entry["text"][:300],
                             "context": entry.get("candidate_context", {}),
                             "structure": entry.get("structure", {})} for entry in samples]
    return list(groups.values()), members


def _legacy_group_identity(entry):
    """Retain grouping for WOLF and existing callers without source metadata."""
    fmt = entry["format"]
    if fmt == "wolf":
        pattern = entry["field_group"]
    elif fmt in ("il2cpp-string", "mono-string"):
        pattern = entry["path"]
    elif fmt in ("json", "component-string"):
        pattern = "/".join("*" if part.isdecimal() else part for part in entry["path"].split("/"))
    elif fmt in ("csv", "tsv"):
        pattern = "/*/" + entry["path"].rsplit("/", 1)[-1]
    elif fmt == "xml":
        pattern = entry["field_group"] + "::" + (entry.get("text_locale") or "unknown")
    else:
        structure = entry.get("structure", {})
        pattern = str([structure.get("sheet"), structure.get("language_column"), entry.get("text_locale")])
    return {"file": entry["file"], "asset": entry.get("asset"),
            "format": fmt, "field_group": pattern}


def collect_candidates(game_dir, *, work_dir=None, godot_executable=None):
    """Group candidates without Tasks; WOLF/Godot prepare in a separate work dir."""
    info = detect(game_dir, godot_executable=godot_executable)
    if info["family"] in {"wolf", "godot"}:
        if work_dir is None:
            raise ValueError("WOLF/Godot 候选预览需要独立 work_dir；CLI 使用 candidates --work，不能使用后续 prepare 的目录")
        work = Path(work_dir).resolve()
        separate_directories(info["game_dir"], work)
        if work.exists() and any(work.iterdir()):
            raise ValueError("候选工作目录非空，请使用新目录保留已有内容")
        resources = adapter(info).prepare(info, work)
    elif info["family"] == "unity":
        resources = {"root": info["game_dir"], "data_dir": info["data_dir"]}
    else:
        raise ValueError("候选预览目前支持 Unity、WOLF 和 Godot；其他引擎继续使用原流程")
    resources["collect_candidates"] = True
    result = extract_resources(adapter(info), info, resources)
    groups, _ = group_entries(result.entries)
    report = {"schema_version": 1, "game_dir": info["game_dir"], "engine": info,
            "source_hashes": {name: file_hash(safe_join(resources["root"], name))
                              for name in sorted(result.source_files())},
            "groups": groups, "selected_groups": [], "reviews": {}, "candidate_count": len(result.entries),
            "resource_hints": result.reports, "warnings": result.warnings,
            "note": "主 Agent 在 reviews 记录用途、理由与决定，在 selected_groups 选择任务来源；样本不代表整组已确认。"}
    if info["family"] == "godot":
        report["godot_source"] = adapter(info).source_identity(info)
    return report


def locate_text(game_dir, query, *, limit=100):
    """Search every parsed candidate, not just the three preview samples per group."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("请输入游戏实际显示的文字或其中一段")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit 必须为正整数")
    info = detect(game_dir)
    if info["family"] != "unity":
        raise ValueError("来源定位目前支持 Unity")
    resources = {"root": info["game_dir"], "data_dir": info["data_dir"], "collect_candidates": True}
    result = extract_resources(adapter(info), info, resources)
    _, members = group_entries(result.entries)
    matches, total = [], 0
    for gid, group in members.items():
        for entry in group:
            if query not in entry["text"]:
                continue
            total += 1
            if len(matches) < limit:
                matches.append({"group_id": gid, "group_count": len(group), **entry})
    return {"game_dir": info["game_dir"], "query": query, "matches": matches,
            "total_matches": total, "truncated": total > limit,
            "candidate_count": len(result.entries), "warnings": result.warnings,
            "resource_hints": result.reports,
            "note": "精确子串搜索全部已解析候选；未命中不表示游戏没有该文本。未创建或选中任务。"}


def validate_selection(selection, game):
    if not isinstance(selection, dict) or selection.get("schema_version") != 1:
        raise ValueError("候选选择记录版本无效")
    if Path(selection.get("game_dir", "")).resolve() != Path(game).resolve():
        raise ValueError("候选选择记录属于另一个游戏")
    ids = selection.get("selected_groups")
    if not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids):
        raise ValueError("请先由主 Agent 在 selected_groups 中选择至少一组候选")
    if len(set(ids)) != len(ids) or not isinstance(selection.get("source_hashes"), dict):
        raise ValueError("候选组重复或缺少来源摘要")
    # Persist decisions and provenance, not the potentially large sample report.
    normalized = {"schema_version": 1, "game_dir": str(Path(game).resolve()),
                  "source_hashes": selection["source_hashes"], "selected_groups": sorted(ids)}
    if "godot_source" in selection:
        source = selection["godot_source"]
        if not isinstance(source, dict) or set(source) != {"executable", "container"}:
            raise ValueError("godot_source 必须保留 executable 和 container 两项来源信息")
        for value in source.values():
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise ValueError("Godot 来源必须为游戏内的相对文件路径或 null")
                safe_join(game, value)
        normalized["godot_source"] = dict(source)
    if "reviews" in selection:
        normalized["reviews"] = _validate_reviews(selection["reviews"], set(ids))
    return normalized


def _validate_reviews(reviews, selected):
    if not isinstance(reviews, dict):
        raise ValueError("reviews 必须为候选组 ID 到审查记录的字典")
    normalized = {}
    for gid, review in reviews.items():
        if not isinstance(gid, str) or not isinstance(review, dict):
            raise ValueError("候选审查记录格式无效")
        decision = review.get("decision")
        if decision not in ("translate", "skip", "pending"):
            raise ValueError(f"候选决定必须为 translate / skip / pending: {gid}")
        if any(not isinstance(review.get(key), str) for key in ("purpose", "reason")):
            raise ValueError(f"候选用途和理由必须为字符串: {gid}")
        if (decision == "translate") != (gid in selected):
            raise ValueError(f"候选决定与 selected_groups 不一致: {gid}")
        normalized[gid] = {key: review[key] for key in ("purpose", "reason", "decision")}
    return normalized


def select_candidates(result, selection, root):
    entries, files = result.entries, result.source_files()
    hashes = selection["source_hashes"]
    if files != set(hashes):
        raise ValueError("候选资源集合已改变，请重新预览并选择")
    for name in files:
        if file_hash(safe_join(root, name)) != hashes[name]:
            raise ValueError(f"候选资源已改变，请重新预览并选择: {name}")
    _, members = group_entries(entries)
    if set(selection["selected_groups"]) - set(members):
        raise ValueError("选中的候选组不存在，请重新预览")
    if set(selection.get("reviews", {})) - set(members):
        raise ValueError("审查记录包含不存在的候选组，请重新预览")
    chosen = {id(e) for gid in selection["selected_groups"] for e in members[gid]}
    selected = [e for e in entries if id(e) in chosen]
    if any(e.get("write_supported") is False for e in selected):
        raise ValueError("所选候选包含暂不支持写回的来源；请查看 write_supported 和来源诊断")
    return selected
