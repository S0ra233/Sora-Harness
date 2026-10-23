"""Godot text resources: locate string values without executing expressions.

Only selected string spans are replaced. Headers, references, dictionary keys,
typed constructors and unrelated formatting remain in the original document.
"""
import re
from collections import Counter

from .tables import check_original, json_pointer


SECTIONS = {"gd_scene", "gd_resource", "node", "resource", "sub_resource",
            "ext_resource", "connection", "editable"}
HEADER = re.compile(r"(?m)^[ \t]*\[([a-z_]+)\b")
ASSIGNMENT = re.compile(r"(?m)^[ \t]*([\w./]+)[ \t]*=")
TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|[A-Za-z_][\w./]*|[^\s]', re.S)
REFERENCE_TYPES = {"ExtResource", "SubResource", "NodePath", "StringName"}
ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
           "v": "\v", "a": "\a", '"': '"', "'": "'", "\\": "\\", "/": "/"}


def unquote(value):
    """Decode Godot's quoted strings, including literal multiline strings."""
    if not value.startswith('"') or not value.endswith('"'):
        raise ValueError("Godot 字符串缺少引号")
    result, index = [], 1
    while index < len(value) - 1:
        char = value[index]
        if char != "\\":
            result.append(char)
            index += 1
            continue
        index += 1
        escape = value[index]
        if escape in ESCAPES:
            result.append(ESCAPES[escape])
            index += 1
        elif escape in {"u", "U"}:
            width = 4 if escape == "u" else 6
            result.append(chr(int(value[index + 1:index + 1 + width], 16)))
            index += width + 1
        else:
            raise ValueError(f"未支持的 Godot 字符串转义: \\{escape}")
    return "".join(result)


def quote(value):
    if "\0" in value:
        raise ValueError("Godot 文本不能包含 NUL")
    escapes = {v: "\\" + k for k, v in ESCAPES.items() if k not in {"'", "/"}}
    return '"' + "".join(escapes.get(char, char) for char in value) + '"'


def _lex(text, offset=0):
    """Ignore comments outside strings; keep exact source spans."""
    tokens, index = [], 0
    while index < len(text):
        if text[index].isspace():
            index += 1
        elif text[index] in "#;":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end + 1
        else:
            match = TOKENS.match(text, index)
            token = match.group()
            if token == '"':
                raise ValueError("Godot 文本存在未闭合字符串")
            tokens.append((token, offset + match.start(), offset + match.end()))
            index = match.end()
    return tokens


def _header_end(text, start):
    quoted, escaped = False, False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char == "]":
            return index + 1
    raise ValueError("Godot 资源段头未闭合")


def _header_fields(text):
    tokens = _lex(text)
    return {tokens[i][0]: unquote(tokens[i + 2][0]) if tokens[i + 2][0].startswith('"')
            else tokens[i + 2][0]
            for i in range(len(tokens) - 2) if tokens[i + 1][0] == "="}


def _masked(text):
    parts, cursor = [], 0
    for token, start, end in _lex(text):
        if token.startswith('"'):
            parts.extend((text[cursor:start], "".join(c if c in "\r\n" else " " for c in token)))
            cursor = end
    return "".join([*parts, text[cursor:]])


def _sections(text):
    # Mask once so large scenes do not compare every header to every string.
    headers = [m for m in HEADER.finditer(_masked(text)) if m[1] in SECTIONS]
    if not headers or headers[0][1] not in {"gd_scene", "gd_resource"}:
        raise ValueError("不是 Godot 文本场景或资源")
    for index, match in enumerate(headers):
        end = _header_end(text, match.start())
        stop = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        yield match[1], _header_fields(text[match.end():end - 1]), end, stop


def _values(tokens):
    stack, ordinal = [], 0
    for index, (token, start, end) in enumerate(tokens):
        if token == "(":
            stack.append(tokens[index - 1][0] if index else "")
        elif token == ")":
            if stack:
                stack.pop()
        elif token.startswith('"'):
            previous = tokens[index - 1][0] if index else ""
            following = tokens[index + 1][0] if index + 1 < len(tokens) else ""
            # &"..." is a StringName; quoted dictionary keys are identifiers.
            if previous in {"&", "^"} or following == ":" or any(t in REFERENCE_TYPES for t in stack):
                continue
            yield ordinal, unquote(token), start, end
            ordinal += 1


def parse(text):
    """Return precise string occurrences and compact section/property facts."""
    records = {}
    for kind, fields, start, stop in _sections(text):
        if kind not in {"node", "resource", "sub_resource"}:
            continue
        identity = (fields.get("parent", "") + "/" + fields.get("name", "")) if kind == "node" else fields.get("id", "root")
        body = text[start:stop]
        assignments = list(ASSIGNMENT.finditer(_masked(body)))
        for index, match in enumerate(assignments):
            end = assignments[index + 1].start() if index + 1 < len(assignments) else len(body)
            value_tokens = _lex(body[match.end():end], start + match.end())
            for ordinal, value, begin, finish in _values(value_tokens):
                path = json_pointer((kind, identity, match[1], ordinal))
                if path in records:
                    raise ValueError(f"Godot 资源存在重复定位: {path}")
                records[path] = {"text": value, "start": begin, "end": finish,
                    "section": kind, "section_id": identity, "node_type": fields.get("type", ""),
                    "property": match[1], "ordinal": ordinal, "section_fields": fields}
    return records


def config_strings(text):
    """Read string values in Godot ConfigFile documents, not resource scenes."""
    section, result = "", {}
    matches = list(re.finditer(r"(?m)^\[([^\]\r\n]+)\]|^[ \t]*([\w./]+)[ \t]*=", _masked(text)))
    for index, match in enumerate(matches):
        if match[1] is not None:
            section = match[1]
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[(section, match[2])] = [value for _, value, _, _ in _values(_lex(text[start:end]))]
    return result


def read(text, entries):
    records = parse(text)
    return [records[e["path"]]["text"] for e in entries]


def replace(text, items):
    records, replacements = parse(text), []
    for entry, translation in items:
        record = records[entry["path"]]
        check_original(record["text"], entry["text"], entry["path"])
        replacements.append((record["start"], record["end"], quote(translation)))
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def _control_pattern():
    from ..text import CONTROL_TAG_RE
    return re.compile(r"\[/?[a-zA-Z_]\w*(?:=[^\]\r\n]*|\s+[^\]\r\n]*)?\]|"
                      r"%[-+0 #]*\d*(?:\.\d+)?[sdioxXfcv]|" + CONTROL_TAG_RE.pattern, re.I)


def protect_tags(text):
    codes = []

    def replace_code(match):
        codes.append(match.group())
        return "{p" + str(len(codes) - 1) + "}"

    return _control_pattern().sub(replace_code, text), codes


def validate_codes(original, translated):
    expected, actual = _control_pattern().findall(original), _control_pattern().findall(translated)
    if Counter(expected) != Counter(actual):
        return "Godot 占位符或 BBCode 内容/数量改变"
    fixed = lambda codes: [code for code in codes if not code.startswith(("%", "{"))]
    if fixed(expected) != fixed(actual):
        return "Godot BBCode/控制码顺序改变"
    return None
