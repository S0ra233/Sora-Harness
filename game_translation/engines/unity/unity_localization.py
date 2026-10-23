"""Unity localization inventory and Entry mapping, sharing component readers."""
from collections import defaultdict
from pathlib import Path

from ...formats import naninovel
from ...formats.tables import json_pointer
from .unity_component import identify
from .unity_objects import load_asset, asset_identity
from .unity_scan import scan_resources
from .unity_types import ComponentReader


def inspect(info):
    """Inventory tables, then resolve their shared references across containers.

    Each container is scanned once using the same reader as normal extraction.
    Unreadable objects remain issues because they may hide localization tables.
    """
    import UnityPy

    root, data = Path(info["game_dir"]), Path(info["data_dir"])
    shared, candidates, documents = defaultdict(list), [], []
    result = {"scope": "Unity StringTable and Naninovel localization TextAsset documents",
              "tables": [], "records": [], "issues": []}
    issues = result["issues"]
    inventory = scan_resources(root, data)
    issues.extend(inventory["issues"])
    reader = ComponentReader(data)
    for path in inventory["assets"]:
        _collect_container(path, root, reader, result, shared, candidates, documents)
    for tree, location, reference in candidates:
        try:
            _resolve_table(tree, location, reference, shared, result)
        except Exception as exc:
            issues.append({"kind": "unresolved_table", "location": location,
                           "message": str(exc)})
    _add_naninovel_inventory(result, documents)
    result["dependencies"] = sorted(p.relative_to(root).as_posix() for p in reader.dependencies)
    return result


def _collect_container(path, root, reader, result, shared, candidates, documents):
    file = path.relative_to(root).as_posix()
    try:
        env = load_asset(path)
    except Exception as exc:
        result["issues"].append({"kind": "resource_read_failed", "file": file, "message": str(exc)})
        return
    for obj in env.objects:
        if obj.type.name not in ("MonoBehaviour", "TextAsset"):
            continue
        location = {"file": file, "asset_file": str(obj.assets_file.name),
                    "path_id": obj.path_id}
        try:
            tree, _ = reader.read(obj, identify(reader, obj))
            if obj.type.name == "TextAsset":
                script = tree.get("m_Script", "")
                text = script if isinstance(script, str) else bytes(script).decode("utf-8-sig")
                document = naninovel.parse(text)
                if document is not None:
                    location.update(asset=asset_identity(obj, tree), format="naninovel",
                                    shared_file=file, supports_missing=False)
                    documents.append((document, location))
                continue
            if "m_TableCollectionName" in tree and "m_Entries" in tree:
                shared[(str(obj.assets_file.name), obj.path_id)].append((tree, file))
            if "m_TableData" not in tree or "m_LocaleId" not in tree:
                continue
            pointer = tree["m_SharedData"]
            file_id = pointer["m_FileID"]
            if type(file_id) is not int or file_id < 0:
                raise ValueError("无效的共享表文件引用")
            target_file = str(obj.assets_file.name) if file_id == 0 else str(
                obj.assets_file.externals[file_id - 1].path).replace("\\", "/").rsplit("/", 1)[-1]
            candidates.append((tree, location, (target_file, pointer["m_PathID"])))
        except Exception as exc:
            result["issues"].append({"kind": "object_read_failed", "location": location,
                                     "message": str(exc)})


def _resolve_table(tree, location, reference, shared, result):
    matches = shared.get(reference, [])
    if len(matches) != 1:
        raise ValueError("共享表引用缺失或不唯一")
    shared_tree, shared_file = matches[0]
    # Shared GUID is stable across locales. Never fall back to a name guess.
    guid = shared_tree.get("m_TableCollectionNameGuidString")
    locale = tree["m_LocaleId"].get("m_Code")
    if not isinstance(guid, str) or not guid.strip():
        raise ValueError("共享表缺少 GUID")
    if not isinstance(locale, str) or not locale.strip():
        raise ValueError("语言标识缺失")
    table_id = "unity:" + guid
    location.update(asset=_table_identity(location, tree),
                    shared_file=shared_file, locale=locale)
    keys = [str(row["m_Id"]) for row in shared_tree["m_Entries"]]
    if len(keys) != len(set(keys)):
        raise ValueError("共享表条目 ID 重复")
    records = []
    for index, row in enumerate(tree["m_TableData"]):
        if type(row.get("m_Id")) is not int:
            raise ValueError("本地化条目 ID 无效")
        records.append({"table_id": table_id, "locale": locale,
                        "key": str(row["m_Id"]), "text": row.get("m_Localized"),
                        "location": {**location,
                                     "path": json_pointer(("m_TableData", index, "m_Localized"))}})
    result["tables"].append({"table_id": table_id, "locale": locale,
                             "keys": keys, "location": location})
    result["records"].extend(records)


def _add_naninovel_inventory(result, documents):
    # Source comments may be repeated in each locale document. Coalesce only
    # identical sources; differing comments remain duplicates and become conflicts.
    sources = {}
    for document, location in documents:
        table_id = "naninovel:" + document["script"]
        src, dst = document["source_locale"], document["target_locale"]
        keys = list(document["blocks"])
        result["tables"].append({"table_id": table_id, "locale": dst,
                                 "keys": keys, "location": location})
        source = sources.setdefault((table_id, src), {"keys": set(), "records": {},
                                                       "location": {**location, "writable": False}})
        source["keys"].update(keys)
        for key, block in document["blocks"].items():
            result["records"].append({"table_id": table_id, "locale": dst, "key": key,
                                      "text": block["text"], "location": {**location, "path": key}})
            source["records"].setdefault((key, block["source"]), {
                "table_id": table_id, "locale": src, "key": key, "text": block["source"],
                "location": {**location, "path": key, "writable": False}})
    for (table_id, locale), source in sources.items():
        result["tables"].append({"table_id": table_id, "locale": locale,
                                 "keys": sorted(source["keys"]), "location": source["location"]})
        result["records"].extend(source["records"].values())


def _table_identity(location, tree):
    return {"path_id": location["path_id"], "file": location["asset_file"],
            "name": tree.get("m_Name", ""), "kind": "MonoBehaviour"}


def make_entries(selected, source_locale, target_locale):
    """Map generic gap selections to stable Unity target-key locations."""
    entries = []
    for item in selected:
        target, source = item["target_location"], item["source"]
        is_naninovel = target.get("format") == "naninovel"
        entries.append({"file": target["file"], "asset": target["asset"],
            "path": item["key"] if is_naninovel else "/m_TableData/key/" + item["key"], "key": item["key"],
            "format": "naninovel" if is_naninovel else "localized-string-table",
            "type": "dialogue" if is_naninovel else "string_table",
            "text": source["text"],
            "original": item["target"]["text"] if item["target"] else None,
            "scene": item["table_id"] + ":" + target_locale, "scene_kind": "text_group",
            "structure": {"source_locale": source_locale, "target_locale": target_locale},
            "source_location": source["location"], "shared_file": target["shared_file"]})
    return entries
