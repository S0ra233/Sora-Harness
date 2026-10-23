"""Production helper: read native IDs using unrpyc's inert AST compatibility.

This process never imports Ren'Py or modules from the game. Original scripts
are not decompiled into the output game. See README.md for backend provenance.
"""
import io
import json
import copy
from pathlib import Path
import struct
import sys
import textwrap
import zlib


def read_slots(path, safe_loads):
    raw = Path(path).read_bytes()
    if not raw.startswith(b"RENPY RPC2"):
        _, nodes = safe_loads(zlib.decompress(raw))
        return nodes, False
    pos, slots = 10, {}
    while pos + 12 <= len(raw):
        slot, offset, length = struct.unpack_from("<III", raw, pos)
        if slot == 0:
            break
        if offset + length > len(raw):
            raise ValueError("RPYC 数据槽超出文件范围")
        slots[slot] = raw[offset:offset + length]
        pos += 12
    slot = 2 if 2 in slots else 1
    _, nodes = safe_loads(zlib.decompress(slots[slot]))
    return nodes, slot == 2


def render(nodes, decompiler):
    buffer, messages = io.StringIO(), []
    decompiler.pprint(buffer, nodes, decompiler.Options(log=messages))
    if messages:
        raise ValueError("; ".join(map(str, messages)))
    code = buffer.getvalue()
    code = code.rsplit("\n# Decompiled by unrpyc:", 1)[0]
    return textwrap.dedent(code).strip() + "\n"


def _children(node):
    kind = type(node).__name__
    if kind == "If":
        return [(str(condition), block) for condition, block in node.entries]
    if kind == "Menu":
        return [(str(text), block) for text, condition, block in node.items if block]
    block = getattr(node, "block", None)
    return [(kind, block)] if isinstance(block, (list, tuple)) else []


def collect(nodes, transformed, decompiler, say_get_code):
    units, strings, languages, warnings = [], [], set(), []

    def walk(block, label="", branch=()):
        for node in block:
            kind = type(node).__name__
            if kind == "Label":
                label = str(node.name)
            if kind in ("Translate", "TranslateSay"):
                language = getattr(node, "language", None)
                if language is not None:
                    languages.add(str(language))
                    continue
                identity = getattr(node, "identifier", None)
                if identity:
                    body = [node] if kind == "TranslateSay" else node.block
                    try:
                        if kind == "TranslateSay":
                            say = copy.copy(node)
                            if getattr(say, "explicit_identifier", None) is False:
                                say.identifier = None
                            code = say_get_code(say) + "\n"
                        else:
                            code = render(body, decompiler)
                        units.append({"id": str(identity).replace(".", "_"), "body": code,
                                      "label": label, "line": getattr(node, "linenumber", 0),
                                      "branch": list(branch), "route": "compiled_native_id"})
                    except (ValueError, AssertionError, AttributeError, TypeError) as exc:
                        warnings.append(f"翻译块 {identity} 无法还原: {exc}")
                continue
            if kind == "TranslateString":
                language = getattr(node, "language", None)
                if language is not None:
                    languages.add(str(language))
                continue
            if kind == "Menu":
                for text, condition, child in node.items:
                    if text:
                        strings.append({"text": str(text), "type": "choice", "label": label,
                                        "line": getattr(node, "linenumber", 0),
                                        "branch": list(branch), "condition": str(condition)})
            if kind == "Say" and not transformed:
                warnings.append(f"第 {getattr(node, 'linenumber', 0)} 行对白没有可靠的原生翻译 ID")
            for description, child in _children(node):
                walk(child, label, (*branch, description))

    walk(nodes)
    return {"units": units, "strings": strings, "languages": sorted(languages), "warnings": warnings}


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    # This is an explicitly configured tool checkout, never a game resource path.
    sys.path.insert(0, request["repository"])
    import decompiler
    from decompiler.renpycompat import pickle_safe_loads
    from decompiler.util import say_get_code
    records = []
    for item in request["files"]:
        record = {"file": item["file"]}
        try:
            nodes, transformed = read_slots(item["path"], pickle_safe_loads)
            record.update(collect(nodes, transformed, decompiler, say_get_code))
            try:
                record["source"] = render(nodes, decompiler)
            except (ValueError, AssertionError, AttributeError, TypeError) as exc:
                record["warnings"].append(f"完整脚本无法还原，界面字符串扫描有缺口: {exc}")
        except Exception as exc:
            record.update(units=[], strings=[], languages=[], warnings=[str(exc)], error=type(exc).__name__)
        records.append(record)
    with Path(sys.argv[2]).open("x", encoding="utf-8") as output:
        json.dump(records, output, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
