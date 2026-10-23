"""Ren'Py literals and native translation slots; no game code is executed."""
from dataclasses import dataclass
import ast
import re

from ..storage import read_file, write_file
from .tables import check_original


@dataclass(frozen=True)
class Literal:
    start: int
    end: int
    token: str

    def value(self):
        return decode_string(self.token)


@dataclass(frozen=True)
class Statement:
    start: int
    end: int
    line: int
    code: str
    literals: tuple[Literal, ...]

    @property
    def indent(self):
        return len(self.code) - len(self.code.lstrip(" \t"))


def decode_string(token):
    """Decode a Ren'Py string, whose whitespace/escapes differ from JSON."""
    raw = token.startswith("r")
    token = token[1:] if raw or token.startswith("u") else token
    quote = token[0]
    if token.startswith(quote * 3):
        raise ValueError("三引号对白可能拆成多段；需要原生翻译模板或编译资源")
    body = token[1:-1]
    if raw:
        return body
    body = re.sub(r"[ \n]+", " ", body)

    def unescape(match):
        code = match.group(1)
        if code in ("{", "[", "%"):
            return code * 2
        if code == "n":
            return "\n"
        if code.startswith("u") and len(code) > 1:
            return chr(int(code[1:], 16))
        return code

    return re.sub(r"\\(u[0-9a-fA-F]{1,4}|.)", unescape, body)


def quote_text(text):
    # Escape each space so a literal can round-trip runs of spaces unchanged.
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    escaped = escaped.replace("\r", "\\u000d").replace("\t", "\\u0009")
    return '"' + escaped.replace(" ", "\\ ") + '"'


def _literal_at(source, start):
    pos = start
    if source[pos] in "ru" and source[pos + 1:pos + 2] in ('"', "'", "`"):
        pos += 1
    if source[pos] not in ('"', "'", "`"):
        return None
    quote = source[pos]
    delimiter = quote * 3 if source.startswith(quote * 3, pos) else quote
    cursor = pos + len(delimiter)
    while cursor < len(source):
        if source[cursor] == "\\":
            cursor += 2
        elif source.startswith(delimiter, cursor):
            end = cursor + len(delimiter)
            return Literal(start, end, source[start:end])
        else:
            cursor += 1
    raise ValueError("Ren'Py 脚本包含未闭合字符串")


def statements(source):
    """Keep source spans while splitting comments, strings and logical lines."""
    start = pos = depth = 0
    line = first_line = 1
    literals = []
    comment_start = None
    while pos < len(source):
        char = source[pos]
        literal = None
        if comment_start is None and (char in ('"', "'", "`") or
                (char in "ru" and (pos == 0 or not source[pos - 1].isalnum()))):
            literal = _literal_at(source, pos)
        if literal is not None:
            literals.append(literal)
            line += source[pos:literal.end].count("\n")
            pos = literal.end
            continue
        if comment_start is None:
            if char == "#":
                comment_start = pos
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
        if char == "\n":
            if depth == 0 and (comment_start is not None or pos == 0 or source[pos - 1] != "\\"):
                end = comment_start if comment_start is not None else pos
                yield Statement(start, end, first_line, source[start:end], tuple(literals))
                start = pos + 1
                first_line = line + 1
                literals = []
            comment_start = None
            line += 1
        pos += 1
    if start < len(source):
        end = comment_start if comment_start is not None else len(source)
        yield Statement(start, end, first_line, source[start:end], tuple(literals))


NON_SAY = set("label menu if elif else while jump call return scene show hide image play stop voice queue pause window nvl screen style text textbutton button add use transform define default python init translate old new $".split())
TRANSLATE = re.compile(r"^translate\s+(\w+)\s+([\w.]+)\s*:\s*$")


def say_literal(statement):
    """Locate only a say statement's display text, excluding arguments/paths."""
    if not statement.literals:
        return None, ""
    literal = statement.literals[0]
    if not statement.code[:literal.start - statement.start].strip() and len(statement.literals) > 1:
        second = statement.literals[1]
        between = statement.code[literal.end - statement.start:second.start - statement.start]
        if not between.strip():
            return second, literal.value()
    for literal in statement.literals:
        prefix = statement.code[:literal.start - statement.start].strip()
        speaker = _speaker_expression(prefix)
        if speaker is None:
            continue
        suffix = statement.code[literal.end - statement.start:].strip()
        if not suffix.startswith((':', '+', '%', '=', ',', ')', ']')):
            return literal, speaker
    return None, ""


def _speaker_expression(prefix):
    if not prefix:
        return ""
    if prefix.split(maxsplit=1)[0] in NON_SAY:
        return None
    # Separate a balanced speaker expression from Ren'Py image attributes.
    pos = depth = 0
    while pos < len(prefix):
        literal = _literal_at(prefix, pos) if prefix[pos] in ('"', "'", "`") else None
        if literal:
            pos = literal.end
            continue
        char = prefix[pos]
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char.isspace() and depth == 0:
            break
        pos += 1
    expression, attributes = prefix[:pos], prefix[pos:]
    if not re.fullmatch(r"(?:\s+[-@\w]+)*", attributes):
        return None
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return None
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)):
        return expression
    return expression if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def template_slots(source):
    """Native dialogue IDs and global old-string keys identify writable slots."""
    slots = {}
    block = None
    count = 0
    old = None
    for statement in statements(source):
        code = statement.code.strip()
        header = TRANSLATE.fullmatch(code)
        if header:
            block = (statement.indent, *header.groups())
            count = 0
            old = None
            continue
        if not code:
            continue
        if block is None:
            continue
        if statement.indent <= block[0]:
            block = None
            continue
        _, language, identity = block
        if identity == "strings":
            if code.startswith("old ") and len(statement.literals) == 1:
                old = statement.literals[0].value()
            elif code.startswith("new ") and len(statement.literals) == 1 and old is not None:
                key = ("string", language, old)
                if key in slots:
                    raise ValueError(f"Ren'Py 原生字符串翻译键重复: {old}")
                slots[key] = statement.literals[0]
                old = None
            continue
        if identity in ("python", "style", "early"):
            continue
        literal, _ = say_literal(statement)
        if literal is not None:
            key = ("dialogue", language, identity, count)
            if key in slots:
                raise ValueError(f"Ren'Py 原生对白翻译 ID 重复: {identity}")
            slots[key] = literal
            count += 1
    return slots


def read(path, entries):
    slots = template_slots(read_file(path) or "")
    return [slots[tuple(entry["path"])].value() for entry in entries]


def write(path, items):
    source = read_file(path)
    slots = template_slots(source)
    replacements = []
    for entry, translated in items:
        literal = slots[tuple(entry["path"])]
        check_original(literal.value(), entry.get("original", entry["text"]), entry["path"])
        replacements.append((literal.start, literal.end, quote_text(translated)))
    pieces, cursor = [], 0
    for start, end, value in sorted(replacements):
        pieces.extend((source[cursor:start], value))
        cursor = end
    pieces.append(source[cursor:])
    write_file(path, "".join(pieces))
    return len(items)


def control_spans(text):
    """Recognize tags and balanced interpolations such as [names[index]!t]."""
    pos = 0
    while pos < len(text):
        if text.startswith(("[[", "{{"), pos):
            yield pos, pos + 2
            pos += 2
            continue
        if text[pos] == "{":
            end = text.find("}", pos + 1)
            if end < 0:
                raise ValueError("Ren'Py 文本标签没有闭合")
            yield pos, end + 1
            pos = end + 1
            continue
        if text[pos] != "[":
            pos += 1
            continue
        start = pos
        depth = 1
        pos += 1
        quote = None
        while pos < len(text) and depth:
            char = text[pos]
            if char == "\\":
                pos += 2
                continue
            if quote:
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
            pos += 1
        if depth:
            raise ValueError("Ren'Py 插值表达式没有闭合")
        yield start, pos


def protect_tags(text):
    pieces, codes = [], []
    previous = 0
    for start, end in control_spans(text):
        pieces.extend((text[previous:start], "{p" + str(len(codes)) + "}"))
        codes.append(text[start:end])
        previous = end
    pieces.append(text[previous:])
    return "".join(pieces), codes


def validate_codes(source, translated):
    from collections import Counter
    try:
        expected = [source[a:b] for a, b in control_spans(source)]
        actual = [translated[a:b] for a, b in control_spans(translated)]
    except ValueError as exc:
        return str(exc)
    if Counter(expected) != Counter(actual):
        return "Ren'Py 标签或插值内容/出现次数不匹配"
    # Variables may move with the sentence. Styling and flow tags retain order.
    if ([code for code in expected if code.startswith("{") and code != "{{"] !=
            [code for code in actual if code.startswith("{") and code != "{{"]):
        return "Ren'Py 文本标签顺序改变"
    return None
