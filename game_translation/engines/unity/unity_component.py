"""TMP and generic component fields; retain script identity and exact locations."""
from ...formats.tables import get_json_path, apply_json_path
from .unity_objects import asset_identity, json_candidates, candidate_context, save_tree


def identify(reader, obj):
    try:
        return reader.identify(obj)
    except (ValueError, FileNotFoundError, AttributeError):
        return None


def extract_tmp(obj, tree, identity, file, entries):
    if not isinstance(tree.get("m_text"), str):
        raise ValueError("完整 TMP 对象中没有字符串 m_text 字段")
    if tree["m_text"].strip():
        entries.append({"file": file, "asset": asset_identity(obj, tree),
            "path": "/m_text", "text": tree["m_text"], "type": "ui_text",
            "format": "tmp-text", "scene": file + "::TMP", "scene_kind": "text_group",
            "candidate_context": candidate_context(tree),
            "structure": {"component": identity["class"], "assembly": identity["assembly"],
                          "game_object": tree.get("m_GameObject"), "field": "m_text"}})


def extract(obj, tree, identity, file, entries, warnings):
    for field, text in json_candidates(tree):
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            warnings.append(f"{file} / PathID {obj.path_id} / {field}: 非有效 Unicode 文本，未建立候选")
            continue
        parent = get_json_path(tree, field.rsplit("/", 1)[0])
        entries.append({"file": file, "asset": asset_identity(obj, tree), "path": field,
            "text": text, "format": "component-string", "type": "component_field",
            "scene": file + "::" + str(obj.assets_file.name) + ":" + str(obj.path_id),
            "scene_kind": "text_group", "candidate_context": candidate_context(parent),
            "structure": {"component": (identity or {}).get("class"),
                          "assembly": (identity or {}).get("assembly")}})


def _load(obj, entry, reader):
    expected = entry["structure"]
    identity = identify(reader, obj)
    if identity != ({"class": expected["component"], "assembly": expected["assembly"]}
                    if expected.get("component") else None):
        raise ValueError("组件脚本类型与提取记录不一致")
    tree, node = reader.read(obj, identity)
    if asset_identity(obj, tree) != entry["asset"]:
        raise ValueError("组件资产身份与提取记录不一致")
    if entry["format"] == "tmp-text":
        if entry["path"] != "/m_text" or not isinstance(tree.get("m_text"), str):
            raise ValueError("TMP 对象类型或文本定位已改变")
    return tree, node


def read(obj, entries, path, reader):
    tree, _ = _load(obj, entries[0], reader)
    return [get_json_path(tree, entry["path"]) for entry in entries]


def write(obj, items, path, reader):
    expected = items[0][0]
    tree, node = _load(obj, expected, reader)
    paths = [entry["path"] for entry, _ in items]
    if len(paths) != len(set(paths)):
        raise ValueError("组件字段定位重复，拒绝写回")
    if expected["format"] == "tmp-text" and (len(items) != 1 or paths != ["/m_text"]):
        raise ValueError("TMP 定位重复或对象身份改变")
    for entry, text in items:
        if entry["asset"] != expected["asset"] or entry["structure"] != expected["structure"]:
            raise ValueError("同一组件的身份记录不一致")
        original = get_json_path(tree, entry["path"])
        if not isinstance(original, str) or original != entry["text"]:
            raise ValueError("组件字段类型或原文已改变，拒绝写回")
        tree = apply_json_path(tree, entry["path"], text)
    save_tree(obj, tree, node)
    return len(items)
