"""Naninovel localization documents, not executable scenario scripts.

Offsets refer to the original string so untouched comments, IDs and whitespace
survive replacement exactly. Joined IDs require a separate fragment adapter.
"""
import re

HEADER = re.compile(r"^; .*?<([^>]+)> to .*?<([^>]+)> localization document for `([^`]+)` naninovel script$")
BLOCK = re.compile(r"^#\s+([^\s]+)\s*$")


def parse(text):
    """Return None for other formats; reject malformed recognized documents."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return None
    match = HEADER.fullmatch(lines[0].lstrip("\ufeff").rstrip("\r\n"))
    if not match:
        return None
    source_locale, target_locale, script = match.groups()
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    offsets.append(offset)
    starts = []
    for index, line in enumerate(lines[1:], 1):
        if line.startswith("#"):
            marker = BLOCK.fullmatch(line.rstrip("\r\n"))
            if not marker or "|" in marker[1]:
                raise ValueError("Naninovel 块 ID 无效或为暂不支持的拼接 ID")
            starts.append((index, marker[1]))
        elif not starts and line.strip() and not line.startswith(";"):
            raise ValueError("Naninovel 文档块外出现正文")
    blocks = {}
    for n, (start, key) in enumerate(starts):
        if key in blocks:
            raise ValueError("Naninovel 文档存在重复块 ID")
        end = starts[n+1][0] if n+1 < len(starts) else len(lines)
        comments, body = [], []
        insert = start + 1
        for index in range(start+1, end):
            line = lines[index].rstrip("\r\n")
            if line.startswith(";"):
                if body:
                    raise ValueError("Naninovel 正文中夹有注释，无法安全确定替换范围")
                insert = index + 1
                if not line.startswith("; >"):
                    comments.append(line[1:].lstrip(" "))
            elif line.strip():
                body.append(index)
        first, last = (body[0], body[-1]+1) if body else (insert, insert)
        value = "".join(lines[first:last]).rstrip("\r\n") if body else ""
        # The range excludes the final line ending, which belongs to the layout.
        finish = offsets[last] - (len(lines[last-1]) - len(lines[last-1].rstrip("\r\n"))) if body else offsets[first]
        blocks[key] = {"source": "\n".join(comments), "text": value,
                       "start": offsets[first], "end": finish}
    return {"source_locale": source_locale, "target_locale": target_locale,
            "script": script, "blocks": blocks}


def replace(text, replacements):
    document = parse(text)
    if document is None:
        raise ValueError("不是 Naninovel 本地化文档")
    edits, seen = [], set()
    for entry, translation in replacements:
        key = entry["key"]
        if key in seen or key not in document["blocks"]:
            raise ValueError("Naninovel 替换 ID 重复或不存在")
        seen.add(key)
        if document["target_locale"] != entry["structure"]["target_locale"]:
            raise ValueError("Naninovel 目标语言与任务不一致")
        block = document["blocks"][key]
        if block["text"] != entry.get("original", entry["text"]):
            raise ValueError("Naninovel 目标原值已改变")
        if "original" in entry and block["text"].strip():
            raise ValueError("Naninovel 目标已有译文，拒绝覆盖")
        if not translation.strip() or any(line.lstrip().startswith(("#", ";")) for line in translation.splitlines()):
            raise ValueError("译文为空或可能被解释为 Naninovel 标记/注释")
        newline = "\r\n" if "\r\n" in text else "\n"
        value = newline.join(translation.splitlines())
        if block["start"] == block["end"]:
            if block["start"] and text[block["start"]-1] not in "\r\n":
                value = newline + value
            value += newline
        edits.append((block["start"], block["end"], value))
    for start, end, value in sorted(edits, reverse=True):
        text = text[:start] + value + text[end:]
    return text
