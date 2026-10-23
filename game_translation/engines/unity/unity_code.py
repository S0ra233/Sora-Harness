"""Code-literal routes; candidates are reviewed before entering Core tasks."""
from pathlib import Path
import base64
import json
import os
import subprocess

from ...formats import il2cpp_strings
from ...storage import write_file


FORMATS = {"il2cpp-string", "mono-string"}


def extract(info, resources, result):
    entries, warnings, report = result.entries, result.warnings, result.reports
    if info["backend"] == "Mono":
        _extract_mono(resources, entries, warnings, report)
        return
    if info["backend"] != "IL2CPP":
        return
    root = Path(resources["root"])
    path = Path(resources["data_dir"]) / "il2cpp_data/Metadata/global-metadata.dat"
    if not path.is_file():
        report.append({"state": "code_source_missing", "route": "il2cpp",
                       "reason": "未发现标准路径的 global-metadata.dat"})
        return
    file = path.relative_to(root).as_posix()
    start = len(entries)
    issues = 0
    try:
        document = il2cpp_strings.parse(path.read_bytes())
        for item in report:
            if item.get("file") == file and item.get("state") == "unrecognized":
                item.update(state="code_source", route="il2cpp", reason="已由 metadata 字符串读取器识别")
        for index, raw in enumerate(document["strings"]):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                issues += 1
                warnings.append(f"{file} / literal {index}: 非 UTF-8 字符串，未建立候选")
                continue
            if not text.strip():
                continue
            entries.append({"file": file, "path": f"/string_literals/{index}",
                "text": text, "format": "il2cpp-string", "type": "code_literal",
                "scene": file + "::code_strings", "scene_kind": "text_group",
                "structure": {"metadata_version": document["version"], "literal_index": index,
                              "shared_literal": True},
                "candidate_context": {"note": "此索引可能被多处代码共享；没有剧情顺序或调用方法信息"}})
        report.append({"file": file, "route": "il2cpp", "state": "partial" if issues else "read",
                       "entries": len(entries) - start, "issues": issues,
                       "metadata_version": document["version"]})
    except (ValueError, OSError) as exc:
        warnings.append(f"{file}: 代码字符串读取失败: {exc}")
        report.append({"file": file, "route": "il2cpp", "state": "read_failed", "reason": str(exc)})


def read(path, entries):
    if _format(entries) == "mono-string":
        document = _mono_call({"operation": "inspect", "path": str(path)})
        values = {row["path"]: row for row in document["literals"]}
        return [_mono_value(document, values, e) for e in entries]
    return il2cpp_strings.read(path.read_bytes(), entries)


def write(path, items):
    if _format([e for e, _ in items]) == "mono-string":
        return _write_mono(path, items)
    result = il2cpp_strings.replace(path.read_bytes(), items)
    expected = [text for _, text in items]
    if il2cpp_strings.read(result, [e for e, _ in items]) != expected:
        raise ValueError("metadata 修改后读回不一致；未保存文件")
    write_file(path, result, binary=True)
    return len(items)


def _format(entries):
    formats = {e["format"] for e in entries}
    if len(formats) != 1 or not formats <= FORMATS:
        raise ValueError("同一代码文件的字符串格式不一致")
    return formats.pop()


def _mono_call(request):
    default = Path(__file__).resolve().parents[3] / "tools/unity_mono/bin/UnityMono.exe"
    helper = Path(os.environ.get("GAME_TRANSLATION_MONO_HELPER", str(default))).resolve()
    if not helper.is_file():
        raise ValueError("Mono 代码字符串需要 dnlib 辅助程序；参见 tools/unity_mono/README.md，"
                         "可通过 GAME_TRANSLATION_MONO_HELPER 指定 UnityMono.exe")
    process = subprocess.run([str(helper)], input=json.dumps(request, ensure_ascii=True),
                             capture_output=True, encoding="utf-8", timeout=120,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if process.returncode:
        raise ValueError("Mono 辅助程序失败: " + process.stderr.strip())
    return json.loads(process.stdout.lstrip("\ufeff"))


def _extract_mono(resources, entries, warnings, report):
    root, data = Path(resources["root"]), Path(resources["data_dir"])
    libraries = sorted((data / "Managed").glob("*.dll"))
    # Framework assemblies are recorded as out of scope, not reported as scanned.
    excluded = [p for p in libraries if p.name.lower().startswith(
        ("unity", "system", "microsoft.", "mono.")) or p.name.lower() in {"mscorlib.dll", "netstandard.dll"}]
    libraries = [p for p in libraries if p not in excluded]
    report.append({"route": "mono", "state": "code_scope", "scope": "Managed non-framework DLLs",
                   "skipped_framework_files": [p.relative_to(root).as_posix() for p in excluded]})
    for path in libraries:
        file = path.relative_to(root).as_posix()
        start = len(entries)
        try:
            document = _mono_call({"operation": "inspect", "path": str(path)})
            invalid = 0
            for literal in document["literals"]:
                text = literal["text"]
                if not text.strip():
                    continue
                try:
                    text.encode("utf-8")
                except UnicodeEncodeError:
                    invalid += 1
                    warnings.append(f"{file} {literal['path']}: 非有效 Unicode 字符串，未建立候选")
                    continue
                entries.append({"file": file, "path": literal["path"], "text": text,
                    "format": "mono-string", "type": "code_literal",
                    "scene": file + "::" + literal["method_name"], "scene_kind": "text_group",
                    "write_supported": document["write_supported"],
                    "structure": {"module_mvid": document["mvid"], "assembly": document["assembly"],
                                  "type_name": literal["type_name"], "method_name": literal["method_name"],
                                  "write_limitation": document["write_limitation"]},
                    "candidate_context": {"note": "静态 ldstr 指令；方法内顺序不保证剧情顺序"}})
            report.append({"file": file, "route": "mono", "state": "partial" if invalid else "read",
                           "entries": len(entries) - start, "issues": invalid,
                           "write_supported": document["write_supported"]})
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"{file}: Mono 代码字符串读取失败: {exc}")
            report.append({"file": file, "route": "mono", "state": "read_failed", "reason": str(exc)})


def _mono_value(document, values, entry):
    structure = entry["structure"]
    value = values.get(entry["path"])
    if document["mvid"] != structure["module_mvid"] or document["assembly"] != structure["assembly"]:
        raise ValueError("Mono 模块身份与提取记录不一致")
    if value is None or any(value[k] != structure[k] for k in ("type_name", "method_name")):
        raise ValueError("Mono 字符串指令位置或方法身份已改变")
    return value["text"]


def _write_mono(path, items):
    original = _mono_call({"operation": "inspect", "path": str(path)})
    old_values = {row["path"]: row for row in original["literals"]}
    changes = []
    for entry, text in items:
        if _mono_value(original, old_values, entry) != entry["text"]:
            raise ValueError("Mono 字符串原文已改变，拒绝写回")
        changes.append({"path": entry["path"], "original": entry["text"], "translation": text})
    result = _mono_call({"operation": "write", "path": str(path), "changes": changes})
    updated = _mono_call({"operation": "inspect", "data": result["data"]})
    new_values = {row["path"]: row for row in updated["literals"]}
    if old_values.keys() != new_values.keys() or original["mvid"] != updated["mvid"]:
        raise ValueError("Mono 写回改变了模块或指令定位；未保存文件")
    expected = {c["path"]: c["translation"] for c in changes}
    for key, old in old_values.items():
        if new_values[key] != {**old, "text": expected.get(key, old["text"])}:
            raise ValueError("Mono 写回的字符串或方法定位不符合预期；未保存文件")
    write_file(path, base64.b64decode(result["data"], validate=True), binary=True)
    return result["written"]
