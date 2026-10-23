"""Literal, single-line key=value documents; not a Java Properties decoder."""
import re


def _check_value(value):
    if any(char in value for char in "\r\n\0"):
        raise ValueError("键值文本不能包含换行或 NUL")
    if (len(value) - len(value.rstrip("\\"))) % 2:
        raise ValueError("键值文本续行语法需要专用适配")
    if re.search(r"\\u[0-9a-fA-F]{4}", value):
        raise ValueError("键值文本 Unicode 转义需要专用适配")


def parse(text):
    lines, records = [], {}
    # Only CR/LF delimit physical records; Unicode punctuation stays in values.
    for match in re.finditer(r"([^\r\n]*)(\r\n|\r|\n|$)", text):
        if not match.group():
            continue
        body, ending = match.groups()
        index = len(lines)
        lines.append(body + ending)
        if not body.strip() or body.lstrip().startswith(("#", "!", ";", "//")):
            continue
        left, separator, right = body.partition("=")
        key = left.strip()
        if not separator or not key or any(c.isspace() or c in "\\\0" for c in key):
            raise ValueError(f"第 {index + 1} 行不符合简单 key=value 格式")
        value = right.lstrip(" \t")
        _check_value(value)
        records["/" + str(index)] = {"key": key, "value": value,
            "prefix": left + separator + right[:len(right) - len(value)], "ending": ending}
    return {"lines": lines, "records": records}


def extract(file, text, entries, **extra):
    document = parse(text)
    records = list(document["records"].items())
    for index, (path, record) in enumerate(records):
        if not record["value"].strip():
            continue
        context = {"key": record["key"]}
        if index:
            context["previous_key"] = records[index - 1][1]["key"]
        if index + 1 < len(records):
            context["next_key"] = records[index + 1][1]["key"]
        entries.append({"file": file, "path": path, "text": record["value"],
            "type": "key_value", "format": "key-value", "scene": file, **extra,
            "structure": {"key": record["key"], "field": "value", "requires_selection": True},
            "candidate_context": context})
    return document


def _record(document, entry):
    record = document["records"].get(entry["path"])
    if record is None or record["key"] != entry["structure"]["key"]:
        raise ValueError(f"键或行位置已改变: {entry['path']}")
    return record


def read(text, entries):
    document = parse(text)
    return [_record(document, entry)["value"] for entry in entries]


def replace(text, items):
    document = parse(text)
    for entry, translation in items:
        record = _record(document, entry)
        if record["value"] != entry["text"]:
            raise ValueError(f"源文本已改变: {entry['path']}")
        _check_value(translation)
        if translation.startswith((" ", "\t")):
            raise ValueError("键值译文不能以分隔符空白开头")
        index = int(entry["path"][1:])
        document["lines"][index] = record["prefix"] + translation + record["ending"]
    return "".join(document["lines"])
