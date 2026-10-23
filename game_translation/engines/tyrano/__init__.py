# -*- coding: utf-8 -*-
"""TyranoScript .ks 文本提取与写回。"""
import re
from pathlib import Path
from ...storage import write_file
from ...extraction import ExtractionResult
from . import tyrano_resources
from .tyrano_resources import probe, prepare, copy_resources, check_build

TRAIL_TAG_RE = re.compile(r"((?:\[[^\]]*\]\s*)+)$")
TEXT_PARAM_RE = re.compile(r'text\s*=\s*"([^"]*)"')
IS_TXT_RE = re.compile(r"[぀-ヿ一-鿿㐀-䶿a-zA-Z]")

def split_dialogue(line):
    line = line.strip()
    m = TRAIL_TAG_RE.search(line)
    if m and m.group(1).strip():
        return line[: m.start()].rstrip(), line[m.start():]
    return line, ""

def is_translatable_text(val):
    if not val or val.startswith("&"):
        return False
    if "=" in val and not IS_TXT_RE.search(val):
        return False
    if not IS_TXT_RE.search(val) or len(val) > 300:
        return False
    return True

def extract_tyrano(file_name, content, entries):
    lines = content.decode("utf-8-sig").split("\n")
    in_script = False
    for i, raw in enumerate(lines):
        line = raw.strip()
        low = line.lower()
        if low.startswith("[iscript"):
            in_script = True
            continue
        if in_script:
            if low.startswith("[endscript"):
                in_script = False
            continue
        if not line or line.startswith(";") or line.startswith("*"):
            continue
        if line.startswith("@"):
            # Command shorthand is executable syntax, not a dialogue line.
            continue
        if line.startswith("["):
            if line.startswith("[emb"):
                stripped_tags = re.sub(r"\[[^\]]*\]", "", line).strip()
                if is_translatable_text(stripped_tags):
                    text, _ = split_dialogue(line)
                    if text:
                        entries.append({"file": file_name, "path": f"$line[{i}]",
                                        "code": None, "type": "dialogue", "text": text})
                        continue
            for n, m in enumerate(TEXT_PARAM_RE.finditer(line)):
                val = m.group(1)
                if is_translatable_text(val):
                    entries.append({"file": file_name, "path": f"$line[{i}].text#{n}",
                                    "code": None, "type": "tagtext", "text": val})
        elif line.startswith("#"):
            name = line[1:].strip()
            if name and not name.startswith("&") and is_translatable_text(name):
                entries.append({"file": file_name, "path": f"$line[{i}]",
                                "code": None, "type": "speaker", "text": name})
        else:
            text, _ = split_dialogue(line)
            if text and is_translatable_text(text):
                entries.append({"file": file_name, "path": f"$line[{i}]",
                                "code": None, "type": "dialogue", "text": text})

def apply_tyrano(content, replacements):
    bom = b"\xef\xbb\xbf" if content.startswith(b"\xef\xbb\xbf") else b""
    lines = content.decode("utf-8-sig").split("\n")
    for path, text in replacements:
        m = re.match(r"\$line\[(\d+)\](?:\.(text#(\d+)|@(\w+)))?", path)
        if not m:
            raise ValueError(f"无效的 Tyrano 定位路径: {path}")
        i = int(m.group(1))
        if i >= len(lines):
            raise ValueError(f"Tyrano 行号不存在: {path}")
        if "\n" in text or "\r" in text:
            raise ValueError(f"Tyrano 单行译文不能包含换行: {path}")
        kind = m.group(2)
        if kind and kind.startswith("text#"):
            if '"' in text:
                raise ValueError(f"Tyrano 标签译文应使用中文引号，不能含 ASCII 双引号: {path}")
            n = int(m.group(3))
            count = 0
            for pm in TEXT_PARAM_RE.finditer(lines[i]):
                if count == n:
                    lines[i] = lines[i][:pm.start(1)] + text + lines[i][pm.end(1):]
                    break
                count += 1
        elif kind and kind.startswith("@"):
            lines[i] = _replace_legacy_field(lines[i], m.group(4), text)
        else:
            vis, suffix = split_dialogue(lines[i].strip())
            leading = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            marker = "#" if lines[i].lstrip().startswith("#") else ""
            ending = "\r" if lines[i].endswith("\r") else ""
            lines[i] = leading + marker + text + suffix.rstrip("\r") + ending
    return bom + "\n".join(lines).encode("utf-8")


def _replace_legacy_field(line, field, text):
    """Legacy direct-call syntax; current extraction/read do not emit these paths.

    Retained for existing local callers rather than extending the Task workflow
    to translate executable script fields without a separate format adapter.
    """
    if field == "val":
        m2 = re.search(r'f\.[A-Za-z_0-9぀-ヿ一-鿿]+\s*=\s*["\']', line)
        if m2:
            q = m2.end() - 1
            quote = line[q]
            end = line.find(quote, q + 1)
            if end > q:
                line = line[:q + 1] + text + line[end:]
    else:
        pat = re.compile(r'["\']?' + field + r'["\']?\s*:\s*["\']([^"\']*)["\']')
        m2 = pat.search(line)
        if m2:
            line = line[:m2.start(1)] + text + line[m2.end(1):]
    return line


def extract(info, resources):
    entries = []
    for path in sorted(Path(resources["data_dir"]).rglob("*.ks")):
        file = path.relative_to(resources["root"]).as_posix()
        found = []
        content = path.read_bytes()
        extract_tyrano(file, content, found)
        labels, label, in_script = {}, "start", False
        for index, line in enumerate(content.decode("utf-8-sig").splitlines()):
            value = line.strip()
            if value.lower().startswith("[iscript"):
                in_script = True
            elif value.lower().startswith("[endscript"):
                in_script = False
            elif not in_script and value.startswith("*"):
                label = str(index) + ":" + value.split("|", 1)[0]
            labels[index] = label
        speaker, previous = "", None
        for entry in found:
            index = int(re.match(r"\$line\[(\d+)\]", entry["path"])[1])
            scene = file + ":" + labels[index]
            if scene != previous:
                speaker = ""
            if entry["type"] == "speaker":
                speaker = entry["text"]
            entry.update(format="tyrano", scene=scene, scene_kind="script_label",
                         speaker=speaker, structure={"line": index, "label": labels[index]})
            container = tyrano_resources.entry_container(info, file)
            if container:
                entry["container"] = container
            entries.append(entry)
            previous = scene
    reports = ([{"state": "read", "route": "asar", "archive": info["asar"]["archive"],
                 "entries": len(entries), "runtime_verified": False}] if info.get("asar") else [])
    return ExtractionResult(entries, reports=reports)


def read(path, entries):
    return read_content(path.read_bytes(), entries)


def read_content(content, entries):
    lines = content.decode("utf-8-sig").split("\n")
    values = []
    for entry in entries:
        match = re.fullmatch(r"\$line\[(\d+)\](?:\.text#(\d+))?", entry["path"])
        if not match:
            raise ValueError("未知 Tyrano 路径")
        line = lines[int(match[1])]
        if match[2] is not None:
            value = list(TEXT_PARAM_RE.finditer(line))[int(match[2])][1]
        elif entry["type"] == "speaker":
            if not line.lstrip().startswith("#"):
                raise ValueError("Tyrano 说话人标记丢失")
            value = line.strip()[1:].strip()
        else:
            value = split_dialogue(line)[0]
        values.append(value)
    return values


def write(path, items):
    entries = [e for e, _ in items]
    if read(path, entries) != [e["text"] for e in entries]:
        raise ValueError("Tyrano 源文本与索引不一致")
    write_file(path, apply_tyrano(path.read_bytes(), [(e["path"], t) for e, t in items]), binary=True)
    return len(items)


def read_many(output, groups, work=None):
    return tyrano_resources.read_many(output, groups, read_content)


def write_many(output, groups, work=None):
    return tyrano_resources.write_many(output, groups, read_content, apply_tyrano)


def font_options(info):
    return []


def configure_font(output, info, font=None, tmp_font=None):
    from .. import unchanged_font
    if font or tmp_font:
        raise ValueError("Tyrano 字体尚需按游戏 CSS 配置适配")
    return unchanged_font()
