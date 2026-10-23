"""XML leaf/attribute slots; preserve markup outside explicitly replaced values."""
import re
from dataclasses import dataclass
from xml.parsers import expat

from .text_document import declared_locale

ATTRIBUTES = re.compile(rb'''([^\s=<>/]+)\s*=\s*(["'])(.*?)\2''', re.DOTALL)


@dataclass
class Slot:
    start: int
    end: int
    text: str
    quote: str
    tags: tuple
    attributes: dict
    parent_attributes: dict
    attribute: str = ""

    def context(self):
        return {"xml_tags": "/".join(self.tags), "attributes": _context_attributes(self.attributes),
                "parent_attributes": _context_attributes(self.parent_attributes), "attribute": self.attribute}

    def locale(self):
        for attrs in (self.attributes, self.parent_attributes):
            attrs = {key.lower(): value for key, value in attrs.items()}
            for name in ("lang", "language", "locale", "xml:lang"):
                if attrs.get(name):
                    return declared_locale(attrs[name])
        return declared_locale(self.attribute) or declared_locale(self.tags[-1].split(":")[-1])


def _context_attributes(attrs):
    return {key: value[:160] for key, value in list(attrs.items())[:12]}


def _tag_end(data, start):
    quote = None
    for index in range(start, len(data)):
        char = data[index]
        if quote:
            if char == quote:
                quote = None
        elif char in (34, 39):
            quote = char
        elif char == 62:
            return index + 1
    raise ValueError("XML 标签没有结束")


def parse(text):
    # Expat byte positions refer to this UTF-8 representation. Original encoding
    # is restored by text_document, while unrelated XML bytes remain untouched.
    data, slots, stack = text.encode("utf-8"), {}, []
    parser = expat.ParserCreate(encoding="utf-8")

    def start(name, attrs):
        offset = parser.CurrentByteIndex
        end = _tag_end(data, offset)
        parent = stack[-1] if stack else None
        index = parent["children"] if parent else 0
        if parent:
            parent["children"] += 1
        path = (parent["path"] if parent else "") + "/" + str(index)
        tags = (parent["tags"] if parent else ()) + (name,)
        parent_attrs = dict(parent["attrs"]) if parent else {}
        for match in ATTRIBUTES.finditer(data, offset, end):
            key = match[1].decode("utf-8")
            slots[path + "/@" + key] = Slot(match.start(3), match.end(3), attrs[key],
                match[2].decode(), tags, dict(attrs), parent_attrs, key)
        stack.append({"path": path, "tags": tags, "attrs": dict(attrs), "parent": parent_attrs,
                      "start": end, "children": 0, "chunks": [], "opaque": False,
                      "empty": data[offset:end].rstrip().endswith(b"/>")})

    def end(name):
        node = stack.pop()
        if not node["children"] and not node["opaque"] and not node["empty"]:
            slots[node["path"] + "/text"] = Slot(node["start"], parser.CurrentByteIndex,
                "".join(node["chunks"]), "", node["tags"], node["attrs"], node["parent"])

    def chars(value):
        if stack:
            stack[-1]["chunks"].append(value)

    def opaque(*args):
        if stack:
            stack[-1]["opaque"] = True

    def reject_doctype(*args):
        raise ValueError("XML DTD/外部实体不在通用文本路线内，请使用临时适配")

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = chars
    parser.CommentHandler = parser.ProcessingInstructionHandler = opaque
    parser.StartDoctypeDeclHandler = reject_doctype
    # Explicit parser encoding overrides a retained UTF-16 XML declaration.
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise ValueError(f"XML 解析失败: {exc}") from exc
    return slots


def extract(file, text, entries, **extra):
    for path, slot in parse(text).items():
        if not slot.text.strip():
            continue
        locale = slot.locale()
        field_group = "/" + "/".join(slot.tags) + ("/@" + slot.attribute if slot.quote else "/text")
        context = slot.context()
        entries.append({"file": file, "path": path, "text": slot.text, "format": "xml",
            "type": "xml_attribute" if slot.quote else "xml_text", "field_group": field_group,
            "candidate_context": context, "structure": {"xml_field": field_group,
                "language_column": locale or slot.attribute or slot.tags[-1],
                "requires_selection": True}, **extra,
            **({"text_locale": locale} if locale else {})})


def read(text, entries):
    slots = parse(text)
    return [slots[entry["path"]].text for entry in entries]


def _escape(value, quote):
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace("\r", "&#13;")
    if quote:
        value = value.replace(quote, "&quot;" if quote == '"' else "&apos;")
        value = value.replace("\n", "&#10;").replace("\t", "&#9;")
    return value.encode("utf-8")


def replace(text, items):
    slots, spans = parse(text), []
    for entry, translation in items:
        slot = slots[entry["path"]]
        if slot.text != entry["text"]:
            raise ValueError(f"XML 原值已改变: {entry['path']}")
        # Keep entity/CDATA spelling and whitespace when the value is unchanged.
        if translation != slot.text:
            spans.append((slot.start, slot.end, _escape(translation, slot.quote)))
    source, output, cursor = text.encode("utf-8"), [], 0
    for start, end, value in sorted(spans):
        if start < cursor:
            raise ValueError("XML 写回位置重复或重叠")
        output.extend((source[cursor:start], value))
        cursor = end
    output.append(source[cursor:])
    return b"".join(output).decode("utf-8")
