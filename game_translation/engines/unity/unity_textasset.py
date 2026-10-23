"""TextAsset and external table text, sharing the existing format parsers."""
import csv
import io
import json
from ...formats import key_value, naninovel, xml_text
from ...formats.text_document import decode_text, detect_format, declared_locale
from ...formats.tables import extract_table, get_json_path, replace_table, table_header
from .unity_objects import asset_identity, read_tree, save_tree, candidate_context


def table_entries(file, text, fmt, entries, candidate_mode=False, **extra):
    if fmt == "key-value":
        return key_value.extract(file, text, entries, **extra)
    if fmt == "xml":
        xml_text.extract(file, text, entries, **extra)
        return
    start = len(entries)
    document = extract_table(file, text, fmt, entries, candidate_mode=candidate_mode, **extra)
    if fmt == "json":
        for entry in entries[start:]:
            parent_path = entry["path"].rsplit("/", 1)[0]
            parent = get_json_path(document, parent_path)
            field = entry["path"].rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
            structure = entry.setdefault("structure", {})
            structure["field"] = field
            if isinstance(parent, dict):
                entry["candidate_context"] = candidate_context(parent)
                entry["row_fields"] = list(parent)
            locale = declared_locale(field)
            if locale:
                entry["text_locale"] = locale
    if fmt in ("csv", "tsv"):
        has_header = table_header(document)
        header = document[0] if has_header else []
        for entry in entries[start:]:
            column = int(entry["path"].split("/")[-1])
            name = header[column] if column < len(header) else ""
            locale = declared_locale(name)
            row = document[int(entry["path"].split("/")[1])]
            fields = {header[i] if i < len(header) else "column_" + str(i): value
                      for i, value in enumerate(row)}
            entry.setdefault("structure", {})["language_column"] = name
            entry["row_fields"] = list(fields)
            entry["candidate_context"] = candidate_context(fields)
            if locale:
                entry["text_locale"] = locale


def document_entries(file, document, name, entries, candidate_mode=False, **extra):
    """Use one content/encoding pipeline for TextAssets and external documents."""
    fmt, basis = detect_format(document.text, name)
    if fmt is None:
        return None
    start = len(entries)
    needs_selection = fmt in {"xml", "key-value"} or (fmt in {"csv", "tsv"} and basis == "content")
    table_entries(file, document.text, fmt, entries,
                  candidate_mode=candidate_mode or needs_selection, **extra)
    for entry in entries[start:]:
        structure = entry.setdefault("structure", {})
        structure.update(text_encoding=document.encoding, format_detection=basis)
        if needs_selection:
            structure["requires_selection"] = True
    return fmt


def extract(obj, tree, file, entries, warnings, candidate_mode):
    identity = asset_identity(obj, tree)
    script = tree.get("m_Script", "")
    document_text = decode_text(script)
    text = document_text.text
    document = naninovel.parse(text)
    if document is not None:
        for key, block in document["blocks"].items():
            if block["text"].strip():
                entries.append({"file": file, "asset": identity, "path": key,
                    "key": key, "text": block["text"], "format": "naninovel",
                    "type": "dialogue", "scene": file + "::" + str(identity["path_id"]),
                    "structure": {"target_locale": document["target_locale"]},
                    "text_locale": document["target_locale"]})
        return
    fmt = document_entries(file, document_text, tree.get("m_Name", ""), entries,
                           candidate_mode=candidate_mode, asset=identity,
                           scene=file + "::" + str(identity["path_id"]))
    if fmt is None:
        if text.strip():
            warnings.append(f"{file}::{identity['name']}: TextAsset 内容未匹配标准文本格式，需 Agent 分析")
        return


def _load(obj, entry, path, reader):
    tree, node = read_tree(obj, path, reader)
    if obj.type.name != "TextAsset" or asset_identity(obj, tree) != entry["asset"]:
        raise ValueError("资产名称或类型与提取时不一致")
    return tree, node


def _parse(text, fmt):
    if fmt == "naninovel":
        document = naninovel.parse(text)
        if document is None:
            raise ValueError("Naninovel 文档格式已改变")
        return document
    if fmt == "json":
        return json.loads(text)
    if fmt in ("csv", "tsv"):
        return list(csv.reader(io.StringIO(text), delimiter="\t" if fmt == "tsv" else ","))
    if fmt == "text":
        return text.splitlines()
    if fmt == "xml":
        return xml_text.parse(text)
    raise ValueError(f"未知 TextAsset 格式: {fmt}")


def read(obj, entries, path, reader=None):
    tree, _ = _load(obj, entries[0], path, reader)
    script = tree["m_Script"]
    text = decode_text(script).text
    if entries[0]["format"] == "xml":
        return xml_text.read(text, entries)
    if entries[0]["format"] == "key-value":
        return key_value.read(text, entries)
    document = _parse(text, entries[0]["format"])
    values = []
    for entry in entries:
        if entry["format"] == "naninovel":
            if document["target_locale"] != entry["structure"]["target_locale"]:
                raise ValueError("Naninovel 目标语言与任务不一致")
            values.append(document["blocks"][entry["key"]]["text"])
        else:
            values.append(get_json_path(document, entry["path"]))
    return values


def write(obj, items, path, reader=None):
    entry = items[0][0]
    tree, node = _load(obj, entry, path, reader)
    script, fmt = tree["m_Script"], entry["format"]
    document = decode_text(script)
    text = document.text
    translated = (naninovel.replace(text, items) if fmt == "naninovel"
                  else replace_table(text, fmt, items))
    encoded = document.encode(translated)
    tree["m_Script"] = encoded.decode("utf-8", "surrogateescape") if isinstance(script, str) else encoded
    save_tree(obj, tree, node)
    return len(items)
