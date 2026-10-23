"""Scan Unity containers and dispatch object formats; save each container once."""
from collections import defaultdict
from pathlib import Path

from ...storage import write_file
from ...extraction import ExtractionResult
from ...formats import tables
from ...formats.text_document import decode_text
from . import unity_textasset, unity_component, unity_utage, unity_stringtable
from .unity_objects import load_asset, object_index, find_object
from .unity_types import ComponentReader, TMP_CLASSES
from .unity_scan import scan_resources

# A fixed dispatch table, not a plugin registration framework. Each module owns
# both read and write for its formats; Entry format names remain on-disk API.
# read(obj, entries, path, reader) returns values in the same order;
# write(obj, items, path, reader) edits one object and returns the written count.
# The container layer owns file I/O; handlers reuse one optional ComponentReader.
FORMAT_HANDLERS = {
    "json": unity_textasset, "csv": unity_textasset, "tsv": unity_textasset,
    "text": unity_textasset, "xml": unity_textasset, "naninovel": unity_textasset,
    "key-value": unity_textasset,
    "tmp-text": unity_component, "component-string": unity_component,
    "utage-book": unity_utage,
    "string-table": unity_stringtable, "localized-string-table": unity_stringtable,
}


def extract_resources(game_dir, data_dir, *, collect_candidates=False):
    result = ExtractionResult()
    entries, warnings, report = result.entries, result.warnings, result.reports
    root, data = Path(game_dir), Path(data_dir or game_dir)
    inventory = scan_resources(root, data)
    report.extend(inventory["issues"])
    report.append({"state": "scan_complete", "files_checked": inventory["files_checked"],
                   "truncated": inventory["truncated"], "containers": len(inventory["assets"])})
    _extract_external(inventory["texts"], root, entries, warnings, report, collect_candidates)
    if not inventory["assets"]:
        return result
    try:
        import UnityPy
    except ImportError as exc:
        raise RuntimeError("Unity 资产读取需要依赖：python -m pip install UnityPy") from exc
    reader = ComponentReader(data)
    for path in inventory["assets"]:
        _extract_container(path, root, reader, entries, warnings, report, collect_candidates)
    result.dependencies.update(p.relative_to(root).as_posix() for p in reader.dependencies)
    return result


def _extract_external(paths, root, entries, warnings, report, candidate_mode):
    for path in paths:
        file, start = path.relative_to(root).as_posix(), len(entries)
        try:
            document = decode_text(path.read_bytes())
            fmt = unity_textasset.document_entries(file, document, path.name, entries,
                                                   candidate_mode=candidate_mode)
            if fmt is None:
                raise ValueError("外置文本未匹配标准格式，需 Agent 分析")
            report.append({"file": file, "state": "read", "entries": len(entries) - start})
        except (ValueError, OSError) as exc:
            warnings.append(f"{file}: 外置文本读取失败: {exc}")
            report.append({"file": file, "state": "read_failed", "reason": str(exc)})


def _extract_container(path, root, reader, entries, warnings, report, candidate_mode):
    file = path.relative_to(root).as_posix()
    start, warning_start = len(entries), len(warnings)
    try:
        env = load_asset(path)
    except Exception as exc:
        warnings.append(f"{file}: Unity 资源未读取 ({type(exc).__name__}: {exc})")
        report.append({"file": file, "state": "read_failed", "reason": str(exc)})
        return
    for obj in env.objects:
        if obj.type.name not in ("TextAsset", "MonoBehaviour"):
            continue
        try:
            _extract_object(obj, reader, file, entries, warnings, candidate_mode)
        except Exception as exc:
            # One unsupported object must not hide readable objects in the file.
            warnings.append(f"{file} / PathID {obj.path_id}: {type(exc).__name__}: {exc}")
    report.append({"file": file, "state": "partial" if len(warnings) > warning_start else "read",
                   "entries": len(entries) - start, "issues": len(warnings) - warning_start})


def _extract_object(obj, reader, file, entries, warnings, candidate_mode):
    identity = unity_component.identify(reader, obj)
    if identity and identity["class"] in TMP_CLASSES:
        tree, _ = reader.read(obj, identity)
        unity_component.extract_tmp(obj, tree, identity, file, entries)
        return
    if identity and identity["class"] == "Utage.AdvImportBook":
        book = unity_utage.load_book(obj)
        if book is None:
            raise ValueError("Utage 对象布局不在当前支持范围内")
        unity_utage.extract(obj, book, file, entries, warnings)
        return
    try:
        tree = obj.read_typetree()
    except ValueError:
        book = unity_utage.load_book(obj)
        if book is not None:
            unity_utage.extract(obj, book, file, entries, warnings)
            return
        tree, _ = reader.read(obj, identity)
    if obj.type.name == "TextAsset":
        unity_textasset.extract(obj, tree, file, entries, warnings, candidate_mode)
    elif isinstance(tree.get("m_TableData"), list):
        unity_stringtable.extract(obj, tree, file, entries)
    elif candidate_mode:
        unity_component.extract(obj, tree, identity, file, entries, warnings)


def _object_groups(entries):
    groups = defaultdict(list)
    for index, entry in enumerate(entries):
        identity = entry["asset"]
        groups[(identity["file"], identity["path_id"])].append(index)
    return groups


def _handler(entries):
    formats = {entry["format"] for entry in entries}
    if len(formats) != 1:
        raise ValueError("一个资产不能混用多个解析格式")
    fmt = formats.pop()
    try:
        return FORMAT_HANDLERS[fmt]
    except KeyError as exc:
        raise ValueError(f"未知 Unity 文本格式: {fmt}") from exc


def _component_reader(path, entries):
    if any(e["format"] in {"tmp-text", "component-string"} for e in entries):
        return ComponentReader.for_asset(path)
    return None


def read(path, entries):
    if not any("asset" in entry for entry in entries):
        return tables.read(path, entries)
    objects = object_index(load_asset(path))
    reader = _component_reader(path, entries)
    result = [None] * len(entries)
    for key, indexes in _object_groups(entries).items():
        group = [entries[i] for i in indexes]
        values = _handler(group).read(find_object(objects, key), group, path, reader)
        for index, value in zip(indexes, values):
            result[index] = value
    return result


def _patch_asset(path, items):
    env = load_asset(path)
    objects = object_index(env)
    entries = [entry for entry, _ in items]
    reader = _component_reader(path, entries)
    written = 0
    for key, indexes in _object_groups(entries).items():
        group = [entries[i] for i in indexes]
        replacements = [items[i] for i in indexes]
        written += _handler(group).write(find_object(objects, key), replacements, path, reader)
    write_file(path, env.file.save(), binary=True)
    return written


def write(path, items):
    if any(entry.get("write_supported") is False for entry, _ in items):
        raise ValueError("选中条目暂不支持写回，请重新 prepare 后检查支持状态")
    if any("asset" in e for e, _ in items):
        if not all("asset" in e for e, _ in items):
            raise ValueError("文件不能同时作为资产和明文写回")
        return _patch_asset(path, items)
    return tables.write(path, items)
