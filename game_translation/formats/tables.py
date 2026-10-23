"""Shared JSON, CSV, TSV and line-based text formats."""
import csv
import io
import json
import re
from ..text import is_translatable
from ..storage import write_file
from .text_document import decode_text, declared_locale

def json_pointer(parts):
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _segments(path):
    if path == "":
        return []
    if path.startswith("/"):
        return [s.replace("~1", "/").replace("~0", "~") for s in path[1:].split("/")]
    if path.startswith("$"):
        return [a or b for a, b in re.findall(r"\.([^.\[\]]+)|\[(\d+)\]", path[1:])]
    raise ValueError(f"不支持的 JSON 定位路径: {path}")


def get_json_path(obj, path):
    for key in _segments(path):
        obj = obj[int(key)] if isinstance(obj, list) else obj[key]
    return obj


def apply_json_path(obj, path, value):
    parts = _segments(path)
    if not parts:
        return value
    current = obj
    for key in parts[:-1]:
        current = current[int(key)] if isinstance(current, list) else current[key]
    current[int(parts[-1]) if isinstance(current, list) else parts[-1]] = value
    return obj

INTERNAL_KEYS = {"id", "key", "guid", "hash", "version", "instanceid", "steptype",
                 "path", "file", "filename", "url", "asset", "resource", "speakerid"}


SOURCE_COLUMNS = {"text", "source", "original", "dialogue", "japanese", "english", "ja", "en"}


def table_header(rows):
    header = [cell.strip().lower() for cell in rows[0]] if rows else []
    locales = [declared_locale(name) for name in header if name not in INTERNAL_KEYS]
    return bool(set(header) & (SOURCE_COLUMNS | INTERNAL_KEYS) or sum(bool(x) for x in locales) >= 2)


def emit_entry(entries, file, path, kind, text, fmt, scene="", speaker="", *, candidate_mode=False, **extra):
    selected = isinstance(text, str) and bool(text.strip()) if candidate_mode else is_translatable(text)
    if selected:
        entries.append({"file": file, "path": path, "type": kind, "text": text,
                        "format": fmt, "scene": scene or file, "speaker": speaker, **extra})


def walk_strings(node, parts=(), *, include_internal=False):
    if isinstance(node, dict):
        for key, value in node.items():
            if include_internal or key.lower() not in INTERNAL_KEYS:
                yield from walk_strings(value, (*parts, key), include_internal=include_internal)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk_strings(value, (*parts, index), include_internal=include_internal)
    elif isinstance(node, str):
        yield json_pointer(parts), node


def extract_table(file, text, fmt, entries, *, candidate_mode=False, **extra):
    """Extract text and return its parsed document for field context collection.

    Candidate mode exposes every nonempty JSON string or table cell. Column
    selection belongs to the Agent; normal extraction retains its known rules.
    """
    if fmt == "json":
        document = json.loads(text)
        for path, value in walk_strings(document, include_internal=candidate_mode):
            emit_entry(entries, file, path, "json_field", value, fmt,
                       candidate_mode=candidate_mode, **extra)
        return document
    elif fmt in ("csv", "tsv"):
        rows = list(csv.reader(io.StringIO(text), delimiter="\t" if fmt == "tsv" else ","))
        if not rows:
            return rows
        header = [cell.strip().lower() for cell in rows[0]]
        named_columns = [i for i, cell in enumerate(header) if cell in SOURCE_COLUMNS]
        has_header = table_header(rows)
        for row_index in range(1 if has_header else 0, len(rows)):
            columns = (range(len(rows[row_index])) if candidate_mode else
                       named_columns or [i for i, cell in enumerate(header) if cell not in INTERNAL_KEYS])
            for col in columns:
                if col < len(rows[row_index]):
                    emit_entry(entries, file, json_pointer((row_index, col)), "csv_cell",
                               rows[row_index][col], fmt, candidate_mode=candidate_mode, **extra)
        return rows
    elif fmt == "text":
        for index, line in enumerate(text.splitlines(keepends=True)):
            if not line.lstrip().startswith(("//", "#", ";", "@")):
                emit_entry(entries, file, json_pointer((index,)), "dialogue",
                           line.rstrip("\r\n"), fmt, **extra)


# Keep old names available to trusted project-local adapters. Built-ins use the
# public names above; these aliases do not duplicate parsing implementations.
_emit = emit_entry
_walk_strings = walk_strings
_extract_table = extract_table


def check_original(current, expected, path):
    if current != expected:
        raise ValueError(f"源文本不匹配，拒绝按过期索引写回: {path}")


def replace_table(content, fmt, items):
    if fmt == "key-value":
        from .key_value import replace
        return replace(content, items)
    if fmt == "xml":
        from .xml_text import replace
        return replace(content, items)
    if fmt in ("json", "rpg-json", "string-table"):
        obj = json.loads(content) if isinstance(content, str) else content
        for entry, translation in items:
            check_original(get_json_path(obj, entry["path"]), entry["text"], entry["path"])
            obj = apply_json_path(obj, entry["path"], translation)
        return json.dumps(obj, ensure_ascii=False, indent=2) if isinstance(content, str) else obj
    if fmt in ("csv", "tsv"):
        delimiter = "\t" if fmt == "tsv" else ","
        rows = list(csv.reader(io.StringIO(content), delimiter=delimiter))
        for entry, translation in items:
            check_original(get_json_path(rows, entry["path"]), entry["text"], entry["path"])
            apply_json_path(rows, entry["path"], translation)
        output = io.StringIO()
        csv.writer(output, delimiter=delimiter, lineterminator="\r\n" if "\r\n" in content else "\n").writerows(rows)
        return output.getvalue()
    if fmt == "text":
        lines = content.splitlines(keepends=True)
        for entry, translation in items:
            index = int(entry["path"].lstrip("/"))
            if "\r" in translation or "\n" in translation:
                raise ValueError("逐行文本译文不能增加行数")
            check_original(lines[index].rstrip("\r\n"), entry["text"], entry["path"])
            ending = lines[index][len(lines[index].rstrip("\r\n")):]
            lines[index] = translation + ending
        return "".join(lines)
    raise ValueError(f"没有格式 {fmt} 的写回器")


_check_original = check_original


def read(path, entries):
    content = decode_text(path.read_bytes()).text
    fmt = entries[0]["format"]
    if fmt == "key-value":
        from .key_value import read as read_key_value
        return read_key_value(content, entries)
    if fmt == "xml":
        from .xml_text import read as read_xml
        return read_xml(content, entries)
    if fmt in ("json", "rpg-json"):
        obj = json.loads(content)
    elif fmt in ("csv", "tsv"):
        obj = list(csv.reader(io.StringIO(content), delimiter="\t" if fmt == "tsv" else ","))
    elif fmt == "text":
        obj = content.splitlines()
    else:
        raise ValueError(f"没有格式 {fmt} 的读取器")
    return [get_json_path(obj, e["path"]) for e in entries]


def write(path, items):
    formats = {e["format"] for e, _ in items}
    if len(formats) != 1:
        raise ValueError("同一个文件的格式不一致")
    document = decode_text(path.read_bytes())
    result = replace_table(document.text, formats.pop(), items)
    write_file(path, document.encode(result), binary=True)
    return len(items)
