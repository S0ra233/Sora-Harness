"""Resource fields -> Entries with factual context and matching scene groups."""
from pathlib import Path

from ...extraction import ExtractionResult
from ...formats import godot as scene, key_value, xml_text
from ...formats.tables import extract_table, get_json_path, table_header
from ...formats.text_document import decode_text, detect_format, declared_locale
from ...storage import stable_id
from . import godot_translation as translations


DISPLAY_NODES = {"Label", "RichTextLabel", "Button", "CheckButton", "CheckBox",
                 "LinkButton", "MenuButton", "OptionButton", "LineEdit", "TextEdit",
                 "AcceptDialog", "ConfirmationDialog", "Window", "WindowDialog", "PopupMenu"}
DISPLAY_PROPERTIES = {"text", "bbcode_text", "placeholder_text", "tooltip_text", "hint_tooltip", "dialog_text", "title", "window_title"}


def extract(info, resources):
    root, documents = Path(resources["root"]), resources["documents"]
    native = translations.native_documents(root, documents, resources["work"])
    info["uses_native_translations"] = bool(native)
    known_keys = {r["key"] for result in native.values() for r in result.get("records", []) if "key" in r}
    known_keys.update(_po_keys(documents))
    unresolved = any(result.get("resource_type") in {"OptimizedTranslation", "PHashTranslation"}
                     and (info["translation_paths"] is None or file in info["translation_paths"])
                     for file, result in native.items())
    entries, reports, warnings = [], [], list(info["resource_warnings"])
    dependencies = set(resources["source_files"])
    for document in documents:
        dependencies.add(document["file"])
        _extract_document(document, native, known_keys, unresolved, entries, reports, warnings, info)
    dependencies.update(p.name for p in (root / "project.godot", root / "project.binary") if p.is_file())
    scripts = [p.relative_to(root).as_posix() for p in sorted(root.rglob("*"))
               if p.is_file() and p.suffix.lower() in {".gd", ".gdc", ".gde", ".dtl", ".dialogue", ".gdextension", ".dll"}]
    if scripts:
        reports.append({"kind": "script_or_plugin_resources", "count": len(scripts), "samples": scripts[:20],
                        "route": "Agent 根据脚本或插件版本判断；现有翻译资源可直接走通用流程，独立语法不按普通逐行文本处理"})
    if not resources.get("collect_candidates"):
        entries = [e for e in entries if not e["structure"].get("requires_selection") and e.get("write_supported", True)]
    warnings.append("Godot 数据字段和用途不明的场景字符串需先 candidates 审查；分组不保证剧情顺序。")
    warnings.append("Godot 读回核对仅覆盖已提取位置，不代表完整文本覆盖或当前游戏运行正常；"
                    "脚本、插件私有格式、字体和图片文字需另行处理。")
    return ExtractionResult(entries, warnings, reports, dependencies)


def _po_keys(documents):
    keys = set()
    for document in documents:
        if document.get("error") or not document["file"].lower().endswith(".po"):
            continue
        try:
            _, records = translations.parse_po(decode_text(Path(document["view"]).read_bytes()).text)
            keys.update(record["key"] for record in records.values())
        except (OSError, ValueError):
            # The extraction pass reports the document error with its file name.
            continue
    return keys


def _extract_document(document, native, known_keys, unresolved, entries, reports, warnings, info):
    file, start = document["file"], len(entries)
    report = {"file": file, "resource_path": document["resource_path"], "binary": document["binary"]}
    try:
        if document.get("error"):
            raise ValueError(document["error"])
        if file in native:
            result = native[file]
            if result.get("error"):
                raise ValueError(result["error"])
            entries.extend(translations.native_entries(file, result))
            report.update(kind="native_translation", locale=result.get("locale"), key_recovered=all("key" in r for r in result["records"]))
        else:
            text = decode_text(Path(document["view"]).read_bytes()).text
            if file.lower().endswith(".po"):
                entries.extend(translations.po_entries(file, text))
                report["kind"] = "gettext"
            elif text.lstrip().startswith(("[gd_scene", "[gd_resource")):
                entries.extend(_scene_entries(file, text, known_keys, unresolved))
                report.update(kind="scene_or_resource", unresolved_translation_keys=unresolved,
                              translation_key_policy="已知翻译键的引用不建任务；修改对应语言资源")
            else:
                _table_entries(file, text, entries)
                report["kind"] = "data_document"
        for entry in entries[start:]:
            annotate(entry, document, info)
        report.update(candidates=len(entries) - start,
                      requires_selection=sum(e["structure"].get("requires_selection", False) for e in entries[start:]),
                      read_only=sum(e.get("write_supported") is False for e in entries[start:]))
    except (OSError, ValueError, KeyError) as exc:
        # Discard only this incomplete in-memory batch, never source files.
        entries[start:] = []
        report.update(kind="needs_adapter", error=str(exc), candidates=0)
        warnings.append(f"Godot 资源未提取: {file}：{exc}")
    reports.append(report)


def _scene_entries(file, text, known_keys, unresolved):
    records = scene.parse(text)
    neighbors = {}
    for record in records.values():
        neighbors.setdefault(record["section_id"], {})[record["property"]] = record["text"][:160]
    for path, record in records.items():
        value = record["text"]
        if not value.strip() or value in known_keys:
            continue
        display = record["node_type"] in DISPLAY_NODES and record["property"] in DISPLAY_PROPERTIES
        yield {"file": file, "path": path, "text": value, "format": "godot-resource",
               "type": "display_text" if display else "resource_string",
               "structure": {"section": record["section"], "section_id": record["section_id"],
                 "node_type": record["node_type"], "field": record["property"],
                 "unresolved_translation_keys": unresolved,
                 "requires_selection": not display or unresolved, "text_syntax": "godot"},
               "candidate_context": neighbors[record["section_id"]]}


def _table_entries(file, text, entries):
    fmt, _ = detect_format(text, file)
    if fmt is None:
        raise ValueError("文本文件的结构未识别")
    start = len(entries)
    if fmt == "key-value":
        document = key_value.extract(file, text, entries)
    elif fmt == "xml":
        xml_text.extract(file, text, entries)
        document = None
    else:
        document = extract_table(file, text, fmt, entries, candidate_mode=True)
    for entry in entries[start:]:
        structure = entry.setdefault("structure", {})
        structure.update(requires_selection=True, text_syntax="godot")
        if fmt == "json":
            parent = get_json_path(document, entry["path"].rsplit("/", 1)[0])
            field = entry["path"].rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
            structure["field"] = field
            if isinstance(parent, dict):
                entry["candidate_context"] = _context(parent)
                entry["row_fields"] = list(parent)[:20]
            locale = declared_locale(field)
            if locale:
                entry["text_locale"] = locale
        elif fmt in {"csv", "tsv"}:
            row, column = map(int, entry["path"].strip("/").split("/"))
            header = document[0] if table_header(document) else []
            name = header[column] if column < len(header) else ""
            structure["language_column"] = name
            entry["candidate_context"] = _context({header[i] if i < len(header) else f"column_{i}": v for i, v in enumerate(document[row])})
            locale = declared_locale(name)
            if locale:
                entry["text_locale"] = locale


def _context(fields):
    return {str(key): value[:160] if isinstance(value, str) else value
            for key, value in list(fields.items())[:12] if isinstance(value, (str, int, float, bool)) or value is None}


def annotate(entry, document, info):
    structure = entry.setdefault("structure", {})
    fmt = entry["format"]
    if fmt == "godot-resource":
        field = structure["section"] + "::" + structure["section_id"] + "::" + structure["field"]
    elif fmt in {"godot-translation", "godot-po"}:
        field = "/messages/" + structure.get("context", "")
    elif fmt == "xml":
        field = entry["field_group"]
    elif fmt in {"csv", "tsv"}:
        field = "/*/" + entry["path"].rsplit("/", 1)[-1]
    elif fmt == "json":
        field = "/".join("*" if s.isdecimal() else s for s in entry["path"].split("/"))
    else:
        field = "/value" if fmt == "key-value" else "/lines"
    identity = {"file": entry["file"], "asset": None, "format": fmt,
                "field_group": field, "text_locale": entry.get("text_locale", "")}
    entry.update(field_group=field, scene="godot::" + stable_id(identity),
                 scene_kind="database" if fmt in {"godot-translation", "godot-po"} else "text_group",
                 scene_title=document["resource_path"] + " · " + field,
                 godot={"container": info["container"], "binary": document["binary"],
                        "resource_path": document["resource_path"], "major": document["major"], "minor": document["minor"]})
    entry["source_context"] = {"engine": "godot", **identity, "resource_path": document["resource_path"],
        "runtime_path": "res://" + entry["file"], "binary": document["binary"]}
    entry["source_context"].update({k: structure[k] for k in ("section", "section_id", "node_type", "field", "context", "unresolved_translation_keys") if k in structure})
    if fmt in {"godot-translation", "godot-po"} and info["translation_paths"] is not None:
        configured = entry["file"] in info["translation_paths"]
        entry["source_context"]["configured_translation"] = configured
        if not configured:
            # Unregistered resources can be loaded by scripts; review that route.
            structure["requires_selection"] = True
    if (document["binary"] or fmt == "godot-translation") and document["major"] not in {3, 4}:
        entry["write_supported"] = False
