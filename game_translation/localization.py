"""Engine-independent alignment and selection of localized resource records.

Adapters supply tables (table_id, locale, keys, location) and records
(table_id, locale, key, text, location). Locations are opaque to this module.
This inventory is separate from Entry: empty strings and absent keys matter.
"""
from collections import Counter, defaultdict
import re

from .storage import stable_id


def normalize_locale(value):
    value = (value or "").strip().lower().replace("_", "-")
    return {"japanese": "ja", "english": "en", "chinesesimplified": "zh-hans",
            "chinese": "zh", "korean": "ko", "ja-jp": "ja"}.get(value, value)


def select_text_sources(entries, locale, overrides=None):
    """Select whole sources, so kanji-only lines survive in a Japanese table.

    Overrides are explicit user decisions keyed by the source IDs in the report.
    Kana sampling is evidence for a guess, not proof of every cell's language.
    """
    groups = defaultdict(list)
    for entry in entries:
        structure = entry.get("structure", {})
        identity = [entry["file"], entry.get("asset"), structure.get("sheet"),
                    structure.get("language_column"), entry.get("text_locale")]
        groups["src_" + stable_id(identity)].append(entry)
    overrides = overrides or {}
    if set(overrides) - set(groups):
        raise ValueError("语言确认记录包含已不存在的来源，请查看本次资源后更新")
    selected, sources = [], []
    locale = normalize_locale(locale)
    for source_id, group in groups.items():
        first = group[0]
        declared = normalize_locale(first.get("text_locale"))
        detected, evidence = declared, "declared" if declared else "unknown"
        if source_id in overrides:
            detected, evidence = normalize_locale(overrides[source_id]), "user"
        elif not declared:
            texts = [e["text"] for e in group if e["text"].strip()][:200]
            kana = sum(bool(re.search(r"[\u3041-\u3096\u30a1-\u30fa]", t)) for t in texts)
            hangul = any(re.search(r"[\uac00-\ud7af]", t) for t in texts)
            if kana >= 2 and kana / len(texts) >= 0.5 and not hangul:
                detected, evidence = "ja", "inferred_from_column_sample"
        state = "selected" if detected == locale else "other_language" if detected else "needs_confirmation"
        if state == "selected":
            selected.extend(group)
        sources.append({"source_id": source_id, "file": first["file"], "asset": first.get("asset"),
                        "sheet": first.get("structure", {}).get("sheet"),
                        "column": first.get("structure", {}).get("language_column"),
                        "locale": detected or None, "evidence": evidence, "state": state,
                        "entries": len(group), "samples": [e["text"][:120] for e in group[:3]]})
    # Keep extraction order stable rather than regrouping Tasks implicitly.
    selected_ids = {id(e) for e in selected}
    return [e for e in entries if id(e) in selected_ids], sources


def align_localization(inventory):
    """Preserve conflicting records; never choose one translation silently."""
    groups = {}
    issues = list(inventory.get("issues", []))
    for table in inventory["tables"]:
        group = groups.setdefault(table["table_id"], {
            "locales": set(), "keys": set(), "records": defaultdict(list),
            "tables": defaultdict(list)})
        group["locales"].add(table["locale"])
        group["keys"].update(table["keys"])
        group["tables"][table["locale"]].append(table["location"])
    for record in inventory["records"]:
        group = groups[record["table_id"]]
        group["keys"].add(record["key"])
        group["records"][(record["key"], record["locale"])].append(record)
    result = []
    for table_id, group in sorted(groups.items()):
        locales = sorted(group["locales"])
        counts = {locale: Counter() for locale in locales}
        rows = []
        conflicts = {locale for locale, locations in group["tables"].items()
                     if len(locations) > 1}
        for locale in sorted(conflicts):
            issues.append({"kind": "duplicate_table", "table_id": table_id,
                           "locale": locale, "locations": group["tables"][locale]})
        for key in sorted(group["keys"]):
            versions = {}
            for locale in locales:
                records = group["records"][(key, locale)]
                if locale in conflicts or len(records) > 1:
                    state = "unresolved"
                    if len(records) > 1:
                        issues.append({"kind": "duplicate_key", "table_id": table_id,
                                       "key": key, "locale": locale})
                elif not records:
                    state = "missing"
                elif not isinstance(records[0]["text"], str):
                    state = "unresolved"
                    issues.append({"kind": "invalid_text", "table_id": table_id,
                                   "key": key, "locale": locale})
                else:
                    state = "present" if records[0]["text"].strip() else "empty"
                versions[locale] = {"state": state, "records": records}
                counts[locale][state] += 1
            rows.append({"key": key, "versions": versions})
        result.append({"table_id": table_id, "locales": locales,
                       "locations": dict(group["tables"]),
                       "counts": {k: dict(v) for k, v in counts.items()}, "rows": rows})
    return {"locales": sorted({loc for g in groups.values() for loc in g["locales"]}),
            "tables": result, "issues": issues, "complete": not issues}


def select_missing(report, source_locale, target_locale):
    """Select only unambiguous gaps in existing target tables; never invent text."""
    if not source_locale or not target_locale or source_locale == target_locale:
        raise ValueError("必须指定不同的源语言和目标语言")
    if any(locale not in report["locales"] for locale in (source_locale, target_locale)):
        raise ValueError("源语言或目标语言不存在；本接口不新增语言")
    selected, skipped = [], Counter()
    for table in report["tables"]:
        if source_locale not in table["locales"] or target_locale not in table["locales"]:
            skipped["table_language_absent"] += len(table["rows"])
            continue
        target_locations = table["locations"][target_locale]
        if any(not loc.get("writable", True) for loc in target_locations):
            skipped["read_only_source"] += len(table["rows"])
            continue
        for row in table["rows"]:
            source, target = (row["versions"][loc] for loc in (source_locale, target_locale))
            if target["state"] == "present":
                skipped["already_present"] += 1
            elif target["state"] == "unresolved" or source["state"] == "unresolved":
                skipped["unresolved"] += 1
            elif source["state"] != "present":
                skipped["source_unavailable"] += 1
            elif target["state"] == "missing" and not target_locations[0].get("supports_missing", True):
                skipped["missing_block_unsupported"] += 1
            else:
                selected.append({"table_id": table["table_id"], "key": row["key"],
                                 "source": source["records"][0],
                                 "target": target["records"][0] if target["records"] else None,
                                 "target_location": table["locations"][target_locale][0]})
    return selected, dict(skipped)


def inspect_localization(game_dir):
    """Inspect supported multilingual resources without changing game/project files.

complete applies only to the adapter's reported scope, not all game text.
Missing dependencies raise ImportError; an invalid game path raises ValueError.
"""
    from .engines import adapter, detect

    info = detect(game_dir)
    engine = adapter(info) if info["family"] != "unknown" else None
    inspect = getattr(engine, "inspect_localization", None)
    if inspect is None:
        return {"engine": info, "supported": False, "complete": False,
                "scope": None, "locales": [], "tables": [],
                "issues": [{"kind": "unsupported", "message": "此引擎暂不支持多语言检查"}]}
    inventory = inspect(info)
    return {"engine": info, "supported": True, "scope": inventory["scope"],
            **align_localization(inventory)}
