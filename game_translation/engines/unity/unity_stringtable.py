"""Unity StringTable extraction, indexed reads and existing-locale filling."""
from ...formats.tables import emit_entry, json_pointer, get_json_path, replace_table
from .unity_objects import asset_identity, read_tree, save_tree, candidate_context


def localized_row(tree, entry):
    if tree.get("m_LocaleId", {}).get("m_Code") != entry["structure"]["target_locale"]:
        raise ValueError("目标资产语言与任务不一致")
    rows = [row for row in tree["m_TableData"] if str(row.get("m_Id")) == entry["key"]]
    if len(rows) > 1:
        raise ValueError("目标语言存在重复条目 ID")
    return rows[0] if rows else None


def fill_localized(tree, replacements):
    for entry, text in replacements:
        row = localized_row(tree, entry)
        actual = row.get("m_Localized") if row is not None else None
        if actual != entry["original"] or (actual is not None and actual.strip()):
            raise ValueError("目标语言原值已改变或已有内容，拒绝覆盖")
        if row is None:
            # Only the known StringTable row schema is supported for insertion.
            if not tree["m_TableData"] or any(set(r) != {"m_Id", "m_Localized", "m_Metadata"}
                                            for r in tree["m_TableData"]):
                raise ValueError("无法确认新增条目的结构，保留资源不写入")
            row = {"m_Id": int(entry["key"]), "m_Localized": "", "m_Metadata": {"m_Items": []}}
            tree["m_TableData"].append(row)
        row["m_Localized"] = text
    return tree


def extract(obj, tree, file, entries):
    identity = asset_identity(obj, tree)
    for index, row in enumerate(tree["m_TableData"]):
        emit_entry(entries, file, json_pointer(("m_TableData", index, "m_Localized")),
              "string_table", row.get("m_Localized"), "string-table",
              asset=identity, scene=file + "::" + str(identity["path_id"]),
              candidate_context=candidate_context(row),
              text_locale=tree.get("m_LocaleId", {}).get("m_Code", ""))


def _load(obj, entry, path, reader):
    tree, node = read_tree(obj, path, reader)
    if obj.type.name != "MonoBehaviour" or asset_identity(obj, tree) != entry["asset"]:
        raise ValueError("资产类型或身份与提取时不一致")
    return tree, node


def read(obj, entries, path, reader=None):
    tree, _ = _load(obj, entries[0], path, reader)
    values = []
    for entry in entries:
        if entry["format"] == "localized-string-table":
            row = localized_row(tree, entry)
            values.append(row.get("m_Localized") if row is not None else None)
        else:
            values.append(get_json_path(tree, entry["path"]))
    return values


def write(obj, items, path, reader=None):
    entry = items[0][0]
    tree, node = _load(obj, entry, path, reader)
    tree = (fill_localized(tree, items) if entry["format"] == "localized-string-table"
            else replace_table(tree, "string-table", items))
    save_tree(obj, tree, node)
    return len(items)
