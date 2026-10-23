"""Read the aligned AdvImportBook layout observed in Fallen (64-bit LE Unity).

Class identity must be checked by the Unity adapter;
the reader validates the entire object, including indexed replacement rows.
Language column names are preserved, not silently mapped to locale codes.
"""
import struct


class _Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError("Utage 数据截断或长度无效")
        value = self.data[self.pos:self.pos+size]
        self.pos += size
        return value

    def integer(self):
        return struct.unpack("<i", self.take(4))[0]

    def count(self):
        count = self.integer()
        if count < 0 or count > (len(self.data)-self.pos)//4:
            raise ValueError("Utage 数组长度无效")
        return count

    def string(self):
        start = self.pos
        text = self.take(self.integer()).decode("utf-8")
        padding = self.take((-self.pos) % 4)
        if any(padding):
            raise ValueError("Utage 字符串对齐不符合已支持布局")
        return {"text": text, "offset": start, "end": self.pos}

    def cells(self):
        return [self.string() for _ in range(self.count())]


def parse(data):
    reader = _Reader(data)
    reader.take(28)  # GameObject PPtr, enabled/alignment, script PPtr (64-bit).
    name = reader.string()["text"]
    book_field = reader.string()["text"]
    sheets = []
    for sheet_index in range(reader.count()):
        rows = []
        for _ in range(reader.count()):
            index = reader.integer()
            cells = reader.cells()
            # Non-empty variants of these two fields have not been established.
            if reader.take(8) != bytes(8):
                raise ValueError("Utage 行附加字段不属于已支持布局")
            rows.append({"index": index, "cells": cells})
        if len({row["index"] for row in rows}) != len(rows):
            raise ValueError("Utage 工作表行号重复")
        source_path = reader.string()["text"]
        reader.take(12)  # Unity object reference retained in the original asset.
        indices = [reader.integer() for _ in range(reader.count())]
        replacements = [reader.cells() for _ in range(reader.count())]
        if len(indices) != len(replacements) or len(set(indices)) != len(indices):
            raise ValueError("Utage 附加行索引与内容不匹配")
        row_indices = {row["index"] for row in rows}
        if any(index not in row_indices for index in indices):
            raise ValueError("Utage 附加行指向不存在的原行")
        sheets.append({"index": sheet_index, "source_path": source_path, "rows": rows,
                       "replacements": dict(zip(indices, replacements))})
    if reader.pos != len(data):
        raise ValueError("Utage 对象存在未解析数据，不接受部分解析结果")
    return {"name": name, "book_field": book_field, "sheets": sheets}


def extract(book):
    """Return known display cells and issues; exclude command/argument columns.

Conservative first version: only named language columns observed in this layout.
Additional language columns require explicit semantics rather than guessing.
"""
    entries, issues = [], []
    for sheet in book["sheets"]:
        if not sheet["rows"]:
            continue
        header = [cell["text"] for cell in sheet["rows"][0]["cells"]]
        if "Command" not in header or "Text" not in header:
            continue
        known = [name for name in ("Text", "English", "ChineseSimplified", "Korean") if name in header]
        if any(header.count(name) != 1 for name in ["Command", *known]):
            raise ValueError("Utage 表头存在重复的命令或语言列")
        command_column = header.index("Command")
        label, label_row = "", -1
        for row in sheet["rows"][1:]:
            cells = row["cells"]
            values = [cell["text"] for cell in cells]
            if command_column >= len(values):
                continue
            command = values[command_column].strip()
            if command.startswith("*"):
                label, label_row = command, row["index"]
                continue
            if command not in ("", "Selection"):
                continue
            replacement = sheet["replacements"].get(row["index"])
            if replacement is not None:
                # Until runtime replacement semantics are proven, don't export
                # competing copies of a display row as independent translations.
                issues.append(f"{book['name']} 表 {sheet['index']} 行 {row['index']}: 显示行有附加版本，暂不提取")
                continue
            for language in known:
                column = header.index(language)
                if column >= len(cells) or not cells[column]["text"].strip():
                    continue
                if cells[column]["text"].strip() in ("<skip_page>", "<skip_text>"):
                    continue
                entries.append({"text": cells[column]["text"], "path": f"/{sheet['index']}/{row['index']}/{column}",
                    "text_locale": {"English": "en", "ChineseSimplified": "zh-Hans", "Korean": "ko"}.get(language, ""),
                    "format": "utage-book", "type": "choice" if command == "Selection" else "dialogue",
                    "scene": f"{sheet['index']}:{label_row}:{label}:{language}",
                    "scene_kind": "event" if label else "text_group", "speaker": "",
                    "structure": {"sheet": sheet['index'], "row": row['index'], "column": column,
                          "language_column": language, "label": label, "label_row": label_row, "command": command,
                        "arguments": values[command_column+1:header.index("Text")],
                        "write_supported": True}, "write_supported": True,
                    "byte_offset": cells[column]["offset"], "byte_end": cells[column]["end"]})
    return entries, issues


def replace(data, items):
    """Patch validated display cells, retaining every other original byte.

    Resolve offsets from the current object, never from persisted Entry offsets.
    Reverse-order edits keep earlier offsets valid when UTF-8 lengths change.
    """
    entries, _ = extract(parse(data))
    available = {entry["path"]: entry for entry in entries}
    patches, expected = [], {}
    for entry, text in items:
        path = entry["path"]
        current = available.get(path)
        if current is None or path in expected:
            raise ValueError(f"Utage 单元格不支持写入或重复: {path}")
        if current["text"] != entry.get("original", entry["text"]):
            raise ValueError(f"Utage 单元格原文已改变: {path}")
        if current["structure"]["language_column"] != entry["structure"]["language_column"]:
            raise ValueError(f"Utage 语言列已改变: {path}")
        if not isinstance(text, str):
            raise ValueError(f"Utage 译文必须为字符串: {path}")
        raw = text.encode("utf-8")
        encoded = struct.pack("<i", len(raw)) + raw + bytes((-len(raw)) % 4)
        patches.append((current["byte_offset"], current["byte_end"], encoded))
        expected[path] = text
    for start, end, encoded in sorted(patches, reverse=True):
        data = data[:start] + encoded + data[end:]
    book = parse(data)
    for path, text in expected.items():
        sheet, row, column = map(int, path.strip("/").split("/"))
        cells = next(r["cells"] for r in book["sheets"][sheet]["rows"] if r["index"] == row)
        if cells[column]["text"] != text:
            raise ValueError(f"Utage 替换后单元格校验失败: {path}")
    return data
