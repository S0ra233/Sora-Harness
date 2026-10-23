"""Native translation locations and gettext strings, without task state."""
import re

from ...formats.godot import unquote, quote
from ...formats.tables import check_original, json_pointer
from ...localization import normalize_locale
from . import godot_backend as backend


PO_FIELD = re.compile(r'^(msgctxt|msgid_plural|msgid|msgstr(?:\[\d+\])?)\s+("(?:\\.|[^"\\])*")\s*$')


def native_documents(root, documents, work):
    jobs, results = [], {}
    for document in documents:
        if document.get("error"):
            continue
        try:
            if is_native(document):
                jobs.append({"id": document["file"], "operation": "translation_read",
                             "input": str(root / document["file"])})
        except (OSError, ValueError) as exc:
            results[document["file"]] = {"error": str(exc)}
    if jobs:
        results.update({r["id"]: r for r in backend.bridge(jobs, backend.session(work, "translations"))})
    return results


def is_native(document):
    if document.get("resource_type") in {"Translation", "TranslationPO", "OptimizedTranslation", "PHashTranslation"}:
        return True
    if document["file"].lower().endswith(".translation"):
        return True
    if document["file"].lower().endswith(".tres"):
        from pathlib import Path
        header = Path(document["view"]).read_text(encoding="utf-8-sig")[:500]
        return bool(re.search(r'\btype="(?:Translation|TranslationPO|OptimizedTranslation|PHashTranslation)"', header))
    return False


def record_path(record):
    if "key" in record:
        return json_pointer(("messages", record.get("context", ""), record["key"], record.get("plural_index", 0)))
    return json_pointer(("messages", record["index"]))


def native_entries(file, result):
    locale = normalize_locale(result.get("locale"))
    for record in result.get("records", []):
        original = record["text"]
        text = original or record.get("key", "")
        if not text.strip():
            continue
        structure = {"field": "message", "language_column": locale, "text_syntax": "godot"}
        structure.update({k: record[k] for k in ("key", "context", "plural_index", "index", "slot", "key_hash") if k in record})
        if not original:
            structure["requires_selection"] = True
        entry = {"file": file, "path": record_path(record), "text": text,
                 "format": "godot-translation", "type": "localized_text", "text_locale": locale,
                 "structure": structure, "candidate_context": {"key": record.get("key"),
                 "context": record.get("context", ""), "message_index": record.get("index")}}
        if original != text:
            entry["original"] = original
        yield entry


def native_values(result, entries):
    if result.get("error"):
        raise ValueError(result["error"])
    records = {record_path(record): record for record in result["records"]}
    values = []
    for entry in entries:
        record = records[entry["path"]]
        if "slot" in record:
            expected = entry["structure"]
            if (record["slot"], record["key_hash"]) != (expected["slot"], expected["key_hash"]):
                raise ValueError("压缩翻译资源的消息位置或键哈希已改变")
        values.append(record["text"])
    return values


def native_write_job(path, output, items):
    metadata = items[0][0]["godot"]
    return {"id": items[0][0]["file"], "operation": "translation_write", "input": str(path),
            "output": str(output), "major": metadata["major"], "minor": metadata["minor"],
            "items": [{**entry["structure"], "original": entry.get("original", entry["text"]),
                       "translation": translation} for entry, translation in items]}


def parse_po(text):
    """Keep gettext keys, contexts, plural forms and string replacement spans."""
    blocks, block, current, offset = [], {"fields": {}, "flags": []}, None, 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        match = PO_FIELD.fullmatch(stripped)
        new_block = (not stripped or stripped.startswith("#") or (match and match[1] in {"msgctxt", "msgid"}
                     and any(k.startswith("msgstr") for k in block["fields"])))
        if new_block and block["fields"] and (not stripped or any(k.startswith("msgstr") for k in block["fields"])):
            blocks.append(block)
            block, current = {"fields": {}, "flags": []}, None
        if match:
            current = match[1]
            begin = offset + line.index(match[2])
            block["fields"][current] = {"text": unquote(match[2]), "start": begin,
                                         "end": begin + len(match[2])}
        elif stripped.startswith('"') and current:
            value = unquote(stripped)
            record = block["fields"][current]
            record["text"] += value
            record["end"] = offset + line.rindex('"') + 1
        elif stripped.startswith("#, ") or stripped.startswith("#,"):
            block["flags"].append((offset, offset + len(line), line))
        elif stripped and not stripped.startswith("#"):
            raise ValueError("PO 文件包含未支持的语法")
        offset += len(line)
    if block["fields"]:
        blocks.append(block)
    records, locale = {}, ""
    for block in blocks:
        fields = block["fields"]
        msgid = fields.get("msgid", {}).get("text", "")
        if not msgid:
            header = fields.get("msgstr", {}).get("text", "")
            match = re.search(r"(?m)^Language:\s*([^\r\n]+)", header)
            locale = normalize_locale(match[1]) if match else locale
            continue
        context = fields.get("msgctxt", {}).get("text", "")
        for name, value in fields.items():
            if not name.startswith("msgstr"):
                continue
            form = int(name[7:-1]) if name.startswith("msgstr[") else 0
            path = json_pointer(("messages", context, msgid, form))
            if path in records:
                raise ValueError(f"PO 消息定位重复: {path}")
            records[path] = {**value, "key": msgid, "context": context, "plural_index": form,
                             "flags": block["flags"], "source": fields.get("msgid_plural", {}).get("text", msgid) if form else msgid}
    return locale, records


def po_entries(file, text):
    locale, records = parse_po(text)
    for path, record in records.items():
        source = record["text"] or record["source"]
        structure = {"key": record["key"], "context": record["context"],
                     "plural_index": record["plural_index"], "language_column": locale,
                     "field": "message", "text_syntax": "godot"}
        if not record["text"] or any(re.search(r"\bfuzzy\b", line) for _, _, line in record["flags"]):
            structure["requires_selection"] = True
        entry = {"file": file, "path": path, "text": source, "original": record["text"],
                 "type": "localized_text", "format": "godot-po", "text_locale": locale,
                 "structure": structure, "candidate_context": {"key": record["key"], "context": record["context"]}}
        if source.strip():
            yield entry


def read_po(text, entries):
    _, records = parse_po(text)
    return [records[e["path"]]["text"] for e in entries]


def replace_po(text, items):
    _, records = parse_po(text)
    replacements, flags = [], {}
    for entry, translation in items:
        record = records[entry["path"]]
        check_original(record["text"], entry.get("original", entry["text"]), entry["path"])
        replacements.append((record["start"], record["end"], quote(translation)))
        for start, end, line in record["flags"]:
            names = [name.strip() for name in line.strip()[2:].split(",") if name.strip() != "fuzzy"]
            if re.search(r"\bfuzzy\b", line):
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                flags[start] = (start, end, "#, " + ", ".join(names) + ending if names else ending)
    for start, end, value in sorted([*replacements, *flags.values()], reverse=True):
        text = text[:start] + value + text[end:]
    return text
