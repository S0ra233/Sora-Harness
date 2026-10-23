"""Font discovery and explicit asset replacement; no runtime plugin injection."""
from collections import defaultdict
from copy import deepcopy
import hashlib
from pathlib import Path
import struct

from ...storage import safe_join, write_file, file_hash
from .unity_objects import load_asset, asset_identity
from .unity_scan import scan_resources
from .unity_types import ComponentReader, TMP_CLASSES

FONT_CLASSES = {"TMPro.TMP_FontAsset", "UIFont"}
TEXT_CLASSES = TMP_CLASSES | {"UnityEngine.UI.Text", "UILabel"}


def pointers(value, path=()):
    if isinstance(value, dict):
        if set(value) == {"m_FileID", "m_PathID"}:
            yield path, value
        else:
            for key, item in value.items():
                yield from pointers(item, (*path, key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from pointers(item, (*path, index))


def pointer_key(obj, ptr):
    file_id, path_id = ptr["m_FileID"], ptr["m_PathID"]
    if not path_id:
        return None
    if type(file_id) is not int or file_id < 0 or file_id > len(obj.assets_file.externals):
        raise ValueError("字体资源文件引用无效")
    file = (str(obj.assets_file.name) if file_id == 0 else
            str(obj.assets_file.externals[file_id - 1].path).replace("\\", "/").rsplit("/", 1)[-1])
    return file, path_id


def _tree(reader, obj):
    identity = reader.identify(obj) if obj.type.name == "MonoBehaviour" else None
    tree, node = reader.read(obj, identity)
    return tree, node, identity


def inspect(info, characters=""):
    root, data = Path(info["game_dir"]), Path(info["data_dir"])
    inventory = scan_resources(root, data)
    issues = list(inventory["issues"])
    records = _collect_font_records(inventory["assets"], root, ComponentReader(data), characters, issues)
    used = _resolve_references(records)
    return {"schema_version": 1, "game_dir": str(root), "fonts": [r for r in records if r["role"] == "font"],
            "text_components": [r for r in records if r["role"] == "text_component"],
            "support_assets": [r for r in records if r["role"] == "support" and id(r) in used],
            "issues": issues, "runtime_verified": False,
            "note": "按对象和 PPtr 定位；字符表缺字不等于最终显示缺字。未解析对象及动态加载仍需调查。"}


def _collect_font_records(paths, root, reader, characters, issues):
    records = []
    for path in paths:
        file = path.relative_to(root).as_posix()
        try:
            env = load_asset(path)
        except Exception as exc:
            issues.append({"file": file, "reason": str(exc)})
            continue
        for obj in env.objects:
            if obj.type.name not in {"Font", "Material", "Texture2D", "Shader", "MonoBehaviour"}:
                continue
            try:
                record = _font_record(obj, reader, file, characters)
                if record is not None:
                    records.append(record)
            except Exception as exc:
                issues.append({"file": file, "path_id": obj.path_id, "reason": str(exc)})
    return records


def _font_record(obj, reader, file, characters):
    identity = reader.identify(obj) if obj.type.name == "MonoBehaviour" else None
    cls = identity["class"] if identity else None
    if obj.type.name == "MonoBehaviour" and cls not in FONT_CLASSES | TEXT_CLASSES:
        return None
    tree, _ = reader.read(obj, identity)
    refs = []
    for parts, ptr in pointers(tree):
        if not ptr["m_PathID"] or parts in (("m_GameObject",), ("m_Script",)):
            continue
        ref = {"field": "/" + "/".join(map(str, parts)), "pointer": ptr}
        try:
            ref["target_key"] = pointer_key(obj, ptr)
        except ValueError as exc:
            ref.update(target_key=None, reason=str(exc))
        refs.append(ref)
    role = "font" if obj.type.name == "Font" or cls in FONT_CLASSES else (
        "text_component" if cls in TEXT_CLASSES else "support")
    row = {"file": file, "asset": asset_identity(obj, tree), "role": role,
           "component": cls, "unity_version": obj.assets_file.unity_version,
           "platform": int(obj.platform),
           "object_hash": hashlib.sha256(obj.get_raw_data()).hexdigest(), "references": refs}
    if role == "font":
        row["embedded_font_bytes"] = len(tree.get("m_FontData", []))
        row["atlas_population_mode"] = tree.get("m_AtlasPopulationMode")
        # Only evidence about THIS serialized character table, never
        # claim fallback/dynamic rendering coverage from this set.
        table = tree.get("m_CharacterTable")
        if isinstance(table, list):
            codes = {r.get("m_Unicode") for r in table if isinstance(r, dict)}
            row["missing_from_character_table"] = sorted(set(characters) - {chr(c) for c in codes
                if type(c) is int and 0 <= c <= 0x10FFFF})
            row["character_count"] = len(codes)
    return row


def _resolve_references(records):
    by_key = defaultdict(list)
    for row in records:
        by_key[(row["asset"]["file"], row["asset"]["path_id"])].append(row)
    used, edges = set(), {}
    for row in records:
        edges[id(row)] = []
        for ref in row["references"]:
            matches = by_key.get(tuple(ref["target_key"]), []) if ref["target_key"] else []
            local = [r for r in matches if r["file"] == row["file"]]
            matches = local or matches
            ref["resolved"] = [{"file": r["file"], "asset": r["asset"]} for r in matches]
            ref["state"] = "resolved" if len(matches) == 1 else "missing_or_ambiguous"
            edges[id(row)].extend(id(r) for r in matches)
            if row["role"] != "support":
                used.update(id(r) for r in matches)
    pending = list(used)
    while pending:
        for linked in edges[pending.pop()]:
            if linked not in used:
                used.add(linked)
                pending.append(linked)
    return used


def _object(root, location, cache):
    path = safe_join(root, location["file"])
    env = cache.setdefault(str(path), None)
    if env is None:
        env = cache[str(path)] = load_asset(path)
    asset = location["asset"]
    matches = [o for o in env.objects if str(o.assets_file.name) == asset["file"] and o.path_id == asset["path_id"]]
    if len(matches) != 1:
        raise ValueError("字体资产定位缺失或不唯一")
    obj = matches[0]
    if hashlib.sha256(obj.get_raw_data()).hexdigest() != location["object_hash"]:
        raise ValueError("字体对象已改变，请重新扫描并确认替换清单")
    return path, env, obj


def _font_bytes(path):
    raw = Path(path).read_bytes()
    if len(raw) < 12 or raw[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        raise ValueError("字体需要单字体 TTF/OTF；不支持 WOFF/TTC")
    count = struct.unpack_from(">H", raw, 4)[0]
    if not count or 12 + count * 16 > len(raw):
        raise ValueError("字体表目录不完整")
    for i in range(count):
        offset, size = struct.unpack_from(">II", raw, 12 + i * 16 + 8)
        if offset + size > len(raw):
            raise ValueError("字体数据表超出文件范围")
    return raw


def prepare_plan(root, info, plan):
    """Resolve all replacement objects before remapping their shared references."""
    replacements = _plan_items(info, plan)
    reader = ComponentReader(Path(root) / Path(info["data_dir"]).relative_to(Path(info["game_dir"])))
    cache, jobs, mapping = {}, [], {}
    for item in replacements:
        job = _prepare_replacement(root, reader, item, plan, cache)
        if any(j["obj"] is job["obj"] for j in jobs):
            raise ValueError("字体目标对象重复")
        if "source" in job:
            source = job["source"]
            key = (str(source.assets_file.name), source.path_id)
            if key in mapping:
                raise ValueError("来源字体对象映射不唯一，请分批处理")
            mapping[key] = job["obj"]
        jobs.append(job)
    for job in jobs:
        if "source" in job:
            _remap_font_references(job, mapping)
    return jobs


def _plan_items(info, plan):
    # Only errors while decoding user fields are labelled as plan errors. Errors
    # from UnityPy or our replacement code retain their original traceback.
    try:
        if plan.get("schema_version") != 1 or Path(plan.get("game_dir", "")).resolve() != Path(info["game_dir"]).resolve():
            raise ValueError("字体替换清单版本或游戏目录不匹配")
        replacements = plan.get("replacements")
        if not isinstance(replacements, list) or not replacements:
            raise ValueError("字体清单需包含 replacements")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"字体清单字段缺失或结构无效: {exc}") from exc
    return replacements


def _prepare_replacement(root, reader, item, plan, cache):
    path, env, obj = _object(root, item["target"], cache)
    tree, node, identity = _tree(reader, obj)
    if asset_identity(obj, tree) != item["target"]["asset"]:
        raise ValueError("目标字体身份不一致")
    cls = identity["class"] if identity else None
    if obj.type.name not in {"Font", "Material", "Texture2D"} and cls not in FONT_CLASSES:
        raise ValueError("字体清单只允许字体、材质和字形图集对象")
    job = dict(path=path, env=env, obj=obj, tree=tree, node=node)
    if "font_file" in item:
        if obj.type.name != "Font" or not tree.get("m_FontData"):
            raise ValueError("直接 TTF/OTF 替换仅适用于包含 m_FontData 的 Font 对象")
        replacement = deepcopy(tree)
        if file_hash(item["font_file"]) != item["font_hash"]:
            raise ValueError("来源 TTF/OTF 摘要不一致")
        raw = _font_bytes(item["font_file"])
        replacement["m_FontData"] = raw if isinstance(tree["m_FontData"], bytes) else list(raw)
        job["replacement"] = replacement
    else:
        source_root = Path(plan["donor_game_dir"]).resolve()
        _, _, source_obj = _object(source_root, item["source"], cache)
        source_reader = ComponentReader.for_asset(safe_join(source_root, item["source"]["file"]))
        replacement, _, source_identity = _tree(source_reader, source_obj)
        if (obj.type != source_obj.type or identity != source_identity
                or obj.platform != source_obj.platform
                or obj.assets_file.unity_version != source_obj.assets_file.unity_version
                or set(tree) != set(replacement)):
            raise ValueError("新旧字体资产的类型、平台、Unity 版本或顶层结构不匹配")
        if asset_identity(source_obj, replacement) != item["source"]["asset"]:
            raise ValueError("来源字体身份不一致")
        job.update(source=source_obj, replacement=deepcopy(replacement))
    return job


def _remap_font_references(job, mapping):
    tree, replacement, obj = job["tree"], job["replacement"], job["obj"]
    old_pointers = dict(pointers(tree))
    for parts, ptr in pointers(replacement):
        # These links belong to the target object/type/shader environment.
        if parts in (("m_GameObject",), ("m_Script",)):
            ptr.update(old_pointers[parts])
            continue
        if parts == ("m_Shader",):
            from UnityPy.classes import PPtr
            source_shader = PPtr(**ptr, assetsfile=job["source"].assets_file).deref()
            target_shader = PPtr(**old_pointers[parts], assetsfile=obj.assets_file).deref()
            if source_shader.get_raw_data() != target_shader.get_raw_data():
                raise ValueError("新旧字体材质的 Shader 不一致，需制作兼容字体资产")
            ptr.update(old_pointers[parts])
            continue
        key = pointer_key(job["source"], ptr)
        if key is None:
            continue
        target = mapping.get(key)
        if target is None:
            raise ValueError(f"来源字体引用未映射: {parts} -> {key}；请将依赖对象加入清单")
        if target.assets_file is obj.assets_file:
            fid = 0
        else:
            matches = [i + 1 for i, ext in enumerate(obj.assets_file.externals)
                       if str(ext.path).replace("\\", "/").rsplit("/", 1)[-1] == str(target.assets_file.name)]
            if len(matches) != 1:
                raise ValueError("目标字体依赖未在原文件引用表中，需人工制作兼容资产")
            fid = matches[0]
        ptr.update(m_FileID=fid, m_PathID=target.path_id)
    replacement["m_Name"] = tree.get("m_Name", "")
    if obj.type.name == "Texture2D" and replacement.get("m_StreamData", {}).get("path"):
        raw = job["source"].read().get_image_data()
        replacement["image data"] = raw if isinstance(replacement["image data"], bytes) else list(raw)
        replacement["m_StreamData"].update(path="", offset=0, size=0)


def apply_plan(root, info, plan):
    jobs = prepare_plan(root, info, plan)
    files = {}
    for job in jobs:
        job["serialized"] = job["obj"].save_typetree(job["replacement"], nodes=job["node"])
        files[job["path"]] = job["env"]
    for path, env in files.items():
        write_file(path, env.file.save(), binary=True)
        saved = load_asset(path)
        for job in (j for j in jobs if j["path"] == path):
            obj = job["obj"]
            matches = [o for o in saved.objects if o.path_id == obj.path_id
                       and str(o.assets_file.name) == str(obj.assets_file.name)]
            if (len(matches) != 1 or matches[0].get_raw_data() != job["serialized"]
                    or matches[0].read_typetree(nodes=job["node"]) != job["replacement"]):
                raise ValueError("字体保存后对象读回不一致，保留构建副本供检查")
    return {"status": "assets_replaced", "objects": len(jobs),
            "changed_files": sorted(p.relative_to(root).as_posix() for p in files),
            "runtime_verified": False, "note": "按明确清单替换字体资产；缺字、材质和排版效果需实机检查"}
