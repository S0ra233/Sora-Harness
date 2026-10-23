"""Interpret WolfTL 0.6.2 JSON without translating editor names or code.

Binary parsing/serialization remains in WolfTL. Locations refer to a dump
document plus a JSON pointer; Entry.file always names the real game resource.
"""
from .tables import get_json_path, apply_json_path, json_pointer


def _entry(file, asset, path, text, kind, scene, structure, field_group, certain):
    if not isinstance(text, str) or not text.strip():
        return None
    return {"file": file, "asset": asset, "path": json_pointer(path),
            "text": text, "type": kind, "format": "wolf",
            "scene": scene, "scene_kind": "event" if "event_id" in structure else "database",
            "structure": structure, "field_group": field_group,
            "candidate_context": {"classification": "display_text" if certain else "needs_review",
                                  **structure}, "display_text": certain}


def _commands(file, asset, commands, prefix, scene, context):
    for position, command in enumerate(commands):
        code = command["code"]
        # Comments/debug strings and label names are not player-facing text.
        if code in (103, 106, 107, 212, 213):
            continue
        args = command.get("intArgs", [])
        picture_text = code == 150 and bool(args) and ((args[0] >> 4) & 7) == 2
        for slot, text in enumerate(command.get("stringArgs", [])):
            if code == 300 and slot == 0:  # CommonEventByName: callable identity.
                continue
            certain = code in (101, 102) or (picture_text and slot == 0)
            kind = {101: "dialogue", 102: "choice"}.get(code, "display" if certain else "candidate")
            structure = {**context, "command_index": command["index"],
                         "code": code, "argument": slot, "integer_args": args}
            yield _entry(file, asset, (*prefix, position, "stringArgs", slot), text,
                         kind, scene, structure, f"command:{code}:argument:{slot}", certain)


def _database(file, asset, document):
    for type_index, table in enumerate(document["types"]):
        scene = f"{file}:type:{type_index}"
        for row_index, row in enumerate(table["data"]):
            context = {"table": table["name"], "type_index": type_index,
                       "record_index": row_index, "record_name": row["name"]}
            prefix = ("types", type_index, "data", row_index)
            # Record names may also be lookup keys. They are review candidates,
            # not automatically changed together with a displayed name field.
            yield _entry(file, asset, (*prefix, "name"), row["name"], "candidate", scene,
                         context, f"database:{type_index}:record_name", False)
            for field_index, field in enumerate(row["data"]):
                value = field["value"]
                if value == "INVALID_IGNORE":
                    continue
                yield _entry(file, asset, (*prefix, "data", field_index, "value"), value,
                    "candidate", scene, {**context, "field": field["name"], "field_index": field_index},
                    f"database:{type_index}:field:{field_index}", False)


def extract(file, asset, document, kind):
    if kind == "map":
        for event_index, event in enumerate(document["events"]):
            for page_index, page in enumerate(event["pages"]):
                context = {"event_id": event["id"], "event_name": event["name"], "page_id": page["id"]}
                yield from _commands(file, asset, page["list"],
                    ("events", event_index, "pages", page_index, "list"),
                    f"{file}:event:{event['id']}:page:{page['id']}", context)
    elif kind == "common":
        yield from _commands(file, asset, document["commands"], ("commands",),
            f"{file}:common:{document['id']}",
            {"event_id": document["id"], "event_name": document["name"]})
    elif kind == "database":
        yield from _database(file, asset, document)
    elif kind == "game":
        for field in ("Title", "TitlePlus", "StartUpMsg", "TitleMsg"):
            yield _entry(file, asset, (field,), document.get(field), "system", file,
                         {"field": field}, "game:" + field, True)


def replace(document, entry, translated):
    actual = get_json_path(document, entry["path"])
    if actual != entry["text"]:
        raise ValueError(f"WOLF 原文已改变: {entry['file']} / {entry['asset']} / {entry['path']}")
    if not entry.get("encoding"):
        raise ValueError(f"尚未确认此 WOLF 资源的写回编码: {entry['file']}")
    if "\0" in translated:
        raise ValueError("WOLF 译文不能包含 NUL 字符")
    # WolfTL's Windows CP932 conversion can substitute '?'; reject beforehand.
    translated.encode(entry["encoding"], errors="strict")
    apply_json_path(document, entry["path"], translated)
