"""Factual Unity grouping shared by candidate review and Scene construction."""
from pathlib import Path

from ...formats.tables import json_pointer
from ...storage import stable_id


def group_identity(entry):
    fmt, structure = entry["format"], entry.get("structure", {})
    if fmt in {"mono-string", "il2cpp-string"}:
        # A literal is independently selectable; nearby instructions are not a scene.
        field = entry["path"]
    elif fmt in {"json", "component-string", "string-table"}:
        field = "/".join("*" if part.isdecimal() else part for part in entry["path"].split("/"))
    elif fmt in {"csv", "tsv"}:
        field = "/*/" + entry["path"].rsplit("/", 1)[-1]
    elif fmt == "xml":
        field = entry["field_group"]
    elif fmt == "utage-book":
        field = json_pointer((structure["sheet"], structure["label_row"],
                              structure["label"], structure["language_column"]))
    elif fmt == "tmp-text":
        field = "/m_text"
    elif fmt == "key-value":
        field = "/value"
    else:
        field = "/blocks" if fmt == "naninovel" else "/lines"
    return {"file": entry["file"], "asset": entry.get("asset"), "format": fmt,
            "field_group": field, "text_locale": entry.get("text_locale", "")}


def annotate_entries(entries):
    """Adapters provide grouping and evidence; Core still owns Scene/Task IDs."""
    for entry in entries:
        identity = group_identity(entry)
        structure, asset = entry.get("structure", {}), entry.get("asset")
        context = {"engine": "unity", **identity}
        for key in ("language_column", "sheet", "label", "label_row", "component",
                    "assembly", "game_object", "type_name", "method_name",
                    "text_encoding", "format_detection", "shared_literal"):
            if key in structure:
                context[key] = structure[key]
        title = Path(entry["file"]).name
        if asset:
            title += " :: " + (asset.get("name") or asset["kind"]) + f" [PathID {asset['path_id']}]"
        title += " :: " + identity["field_group"]
        if identity["text_locale"]:
            title += " (" + identity["text_locale"] + ")"
        entry.update(scene="unity::" + stable_id(identity), scene_title=title,
                     source_context=context)
