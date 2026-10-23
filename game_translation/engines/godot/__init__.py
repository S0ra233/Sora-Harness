"""Godot adapter: native resource locations, shared Core translation state."""
import shutil

from ...formats import godot as scenes, tables
from ...formats.text_document import decode_text
from ...storage import safe_join, write_file
from . import godot_backend as backend, godot_translation as translations
from .godot_resources import probe, prepare, copy_resources, materialize, select_source, source_identity
from .godot_text import extract


def _metadata(groups, writing=False):
    entries = [item[0] if writing else item for group in groups.values() for item in group]
    containers = [entry["godot"]["container"] for entry in entries]
    if any(container != containers[0] for container in containers):
        raise ValueError("Godot 批量操作必须对应同一个资源容器")
    return containers[0]


def _bridge_results(jobs, directory):
    if not jobs:
        return {}
    results = backend.bridge(jobs, directory)
    for result in results:
        if result.get("error"):
            raise ValueError(f"Godot 资源处理失败: {result['id']}：{result['error']}；日志: {directory / 'bridge.log'}")
    return {result["id"]: result for result in results}


def _views(root, groups, directory):
    views, jobs = {}, []
    for file, entries in groups.items():
        entry = entries[0]
        path = safe_join(root, file)
        if entry["format"] == "godot-translation":
            jobs.append({"id": file, "operation": "translation_read", "input": str(path)})
        elif entry["godot"]["binary"]:
            view = safe_join(directory / "views", file + ".tres")
            views[file] = view
            jobs.append({"id": file, "operation": "to_text", "input": str(path),
                         "output": str(view), "resource_path": entry["godot"]["resource_path"]})
        else:
            views[file] = path
    results = _bridge_results(jobs, backend.session(directory, "views")) if jobs else {}
    return views, results


def read_many(output, groups, work=None):
    container = _metadata(groups)
    root, directory = materialize(output, container, work, "read")
    views, results = _views(root, groups, directory)
    values = {}
    for file, entries in groups.items():
        fmt = entries[0]["format"]
        if fmt == "godot-translation":
            values[file] = translations.native_values(results[file], entries)
        elif fmt == "godot-resource":
            values[file] = scenes.read(decode_text(views[file].read_bytes()).text, entries)
        elif fmt == "godot-po":
            values[file] = translations.read_po(decode_text(views[file].read_bytes()).text, entries)
        else:
            values[file] = tables.read(views[file], entries)
    return values


def _write_document(source, target, items):
    fmt = items[0][0]["format"]
    document = decode_text(source.read_bytes())
    if fmt == "godot-resource":
        result = scenes.replace(document.text, items)
    elif fmt == "godot-po":
        result = translations.replace_po(document.text, items)
    else:
        result = tables.replace_table(document.text, fmt, items)
    write_file(target, document.encode(result), binary=True)


def write_many(output, groups, work=None):
    container = _metadata(groups, writing=True)
    root, directory = materialize(output, container, work, "write")
    entry_groups = {file: [entry for entry, _ in items] for file, items in groups.items()}
    views, _ = _views(root, entry_groups, directory)
    patches, jobs = {}, []
    for file, items in groups.items():
        entry, _ = items[0]
        target = safe_join(directory / "changed", file)
        metadata = entry["godot"]
        if entry["format"] == "godot-translation":
            jobs.append(translations.native_write_job(safe_join(root, file), target, items))
        elif metadata["binary"]:
            edited = safe_join(directory / "edited", file + ".tres")
            _write_document(views[file], edited, items)
            jobs.append({"id": file, "operation": "to_binary", "input": str(edited),
                         "output": str(target), "major": metadata["major"], "minor": metadata["minor"]})
        else:
            _write_document(views[file], target, items)
        patches[file] = target
    if jobs:
        _bridge_results(jobs, backend.session(directory, "serialize"))
    if container:
        original = safe_join(output, container["file"])
        patched = backend.patch(original, patches, directory, embedded=container["embedded"])
        shutil.copyfile(patched, original)
    else:
        for file, target in patches.items():
            shutil.copyfile(target, safe_join(output, file))
    return sum(len(items) for items in groups.values())


def check_build(root, info, changed_files):
    if info["container"] or info.get("uses_native_translations") or any(d["binary"] for d in info.get("resource_documents", [])):
        backend.executable()
    issues = list(info.get("load_issues", []))
    return {"can_build": not issues, "status": "native_container" if info["container"] else "loose_resources",
            "container": info["container"], "executable": info.get("executable"),
            "additional_containers": info.get("additional_containers", []),
            "issues": issues, "runtime_verified": False,
            "note": "最终资源包读回核对已索引文本；加载、字体和自定义插件需实机确认"}


def font_options(info):
    return []


def configure_font(output, info, font=None, tmp_font=None):
    if font or tmp_font:
        raise ValueError("Godot 初版尚未实现字体替换；请根据游戏 Theme/Font 引用单独处理")
    from .. import unchanged_font
    return unchanged_font()
