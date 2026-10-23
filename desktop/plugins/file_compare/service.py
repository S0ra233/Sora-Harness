"""只读文本比对；按块定位后只细分小区间，避免大段重复文本的全局二次比较。"""
import difflib
import hashlib
import os
from pathlib import Path


def _read(path, encoding):
    path = Path(path).expanduser().resolve(strict=True)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(8 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("单份比对文本最多8MiB，请先按章节或文件区间拆分。")
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("读取期间文件改变，请重新比对。")
    if encoding == "auto":
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    text = raw.decode(encoding, errors="strict")
    if "\0" in text:
        raise ValueError("文件含二进制空字符，请先转换成文本。")
    lines = text.splitlines(keepends=True)
    if len(lines) > 200000:
        raise ValueError("单份比对最多200000行，请先拆分。")
    return lines, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "lines": len(lines)}


def compare_files(left, right, encoding="auto"):
    old, left_meta = _read(left, encoding)
    new, right_meta = _read(right, encoding)
    size = 128
    def blocks(lines):
        return [hashlib.sha256("".join(lines[start:start + size]).encode("utf-8")).digest() for start in range(0, len(lines), size)]
    hunks, complete = [], True
    for tag, a, b, c, d in difflib.SequenceMatcher(None, blocks(old), blocks(new), autojunk=True).get_opcodes():
        if tag == "equal":
            continue
        a, b, c, d = a * size, min(b * size, len(old)), c * size, min(d * size, len(new))
        coarse = b - a + d - c > 4000
        changes = [(tag, a, b, c, d)] if coarse else [(kind, a + i, a + j, c + k, c + l)
            for kind, i, j, k, l in difflib.SequenceMatcher(None, old[a:b], new[c:d], autojunk=False).get_opcodes() if kind != "equal"]
        for kind, i, j, k, l in changes:
            if len(hunks) == 2000:
                complete = False
                break
            preview = [f"@@ 左 {i + 1}:{j}　右 {k + 1}:{l} @@"]
            if coarse:
                preview.append("大区间按块展示；不是最小编辑差异。")
            for prefix, values in (("- ", old[i:j]), ("+ ", new[k:l])):
                preview.extend(prefix + line.rstrip("\r\n")[:1000] + (" …" if len(line) > 1000 else "") for line in values[:100])
                if len(values) > 100:
                    preview.append(f"{prefix}… 另有 {len(values) - 100} 行，原文件未截断。")
            hunks.append({"title": f"左 {i + 1}–{j} ／右 {k + 1}–{l}", "text": "\n".join(preview), "coarse": coarse})
        if not complete:
            break
    return {"left": left_meta, "right": right_meta, "identical_bytes": left_meta["sha256"] == right_meta["sha256"],
            "hunks": hunks, "complete": complete, "basis": "decoded_text_with_line_endings; block_bounded; read_only"}
