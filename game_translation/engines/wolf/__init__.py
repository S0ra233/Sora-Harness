"""WOLF adapter: native resources -> WolfTL documents -> Core Entries."""
import json
from pathlib import Path
import shutil

from ...formats import wolf as fields
from ...formats.tables import get_json_path
from ...storage import safe_join, write_json
from ...extraction import ExtractionResult
from . import wolf_backend as backend
from . import wolf_resources as resources_io
from .wolf_resources import probe, prepare, copy_resources


def extract(info, resources):
    root, data, dump = map(Path, (resources["root"], resources["data_dir"], resources["dump"]))
    documents, warnings = resources_io.documents(data, dump)
    entries, reports = [], []
    # WolfTL reads the whole dataset, so changes to any parsing input invalidate
    # preparation, including database schema (.project) files.
    dependencies = [p.relative_to(root).as_posix() for p in sorted(data.rglob("*"))
                    if p.is_file() and p.suffix.lower() in (".dat", ".project", ".mps")]
    for path, asset, kind in documents:
        file = path.relative_to(root).as_posix()
        document = json.loads(safe_join(dump, asset).read_text(encoding="utf-8-sig"))
        encoding = resources_io.encoding(path, kind)
        candidates = [e for e in fields.extract(file, asset, document, kind) if e is not None]
        selected = candidates if resources.get("collect_candidates") else [e for e in candidates if e["display_text"]]
        for entry in selected:
            entry.update(data_relative=info["data_relative"], encoding=encoding,
                         write_supported=encoding is not None)
        entries.extend(selected)
        if candidates:
            reports.append({"file": file, "asset": asset, "kind": kind, "encoding": encoding,
                            "candidates": len(candidates), "selected": len(selected),
                            "needs_review": sum(not e["display_text"] for e in candidates)})
    warnings.append("WOLF 默认仅提取明确显示字段；数据库、字符串赋值等需通过 candidates 审查后选择。")
    warnings.append("输出使用解包资源，不重建 .wolf；图片文字、外部自定义文本与实机加载尚未保证。")
    if any(e["write_supported"] is False for e in entries):
        warnings.append("部分资源编码尚未确认，只能读取；候选中 write_supported=false，不能写回。")
    return ExtractionResult(entries, warnings, reports, set(dependencies))


def _dataset(root, groups):
    entries = [e for group in groups.values() for e in group]
    directories = {e["data_relative"] for e in entries}
    if len(directories) != 1:
        raise ValueError("WOLF 批量操作必须属于同一个 Data 目录")
    return safe_join(root, directories.pop())


def read_many(root, groups, work=None):
    data = _dataset(root, groups)
    dump = backend.export(data, backend.session(work, "read"))
    documents, values = {}, {}
    for file, entries in groups.items():
        values[file] = []
        for entry in entries:
            asset = entry["asset"]
            if asset not in documents:
                documents[asset] = json.loads(safe_join(dump, asset).read_text(encoding="utf-8-sig"))
            values[file].append(get_json_path(documents[asset], entry["path"]))
    return values


def write_many(root, groups, work=None):
    data = _dataset(root, {name: [e for e, _ in items] for name, items in groups.items()})
    directory = backend.session(work, "write")
    dump = backend.export(data, directory)
    documents, changed = {}, set()
    for file, items in groups.items():
        for entry, translated in items:
            asset = entry["asset"]
            if asset not in documents:
                documents[asset] = json.loads(safe_join(dump, asset).read_text(encoding="utf-8-sig"))
            fields.replace(documents[asset], entry, translated)
        relative = safe_join(root, file).relative_to(data).as_posix()
        changed.add(relative)
        if any(e["asset"].startswith("db/") for e, _ in items):
            changed.add(str(Path(relative).with_suffix(".project")).replace("\\", "/"))
    for asset, document in documents.items():
        write_json(safe_join(dump, asset), document)
    patched = backend.patch(data, directory)
    # The backend serializes everything. Publish only selected containers and
    # their schema companions; untouched resources keep their original bytes.
    for relative in sorted(changed):
        source = safe_join(patched, relative)
        if not source.is_file():
            raise ValueError(f"WolfTL 未生成待写回资源: {source}")
        shutil.copyfile(source, safe_join(data, relative))
    return sum(len(items) for items in groups.values())


def _root(path, entry):
    root = Path(path).resolve()
    for _ in Path(entry["file"]).parts:
        root = root.parent
    return root


def read(path, entries):
    return read_many(_root(path, entries[0]), {entries[0]["file"]: entries})[entries[0]["file"]]


def write(path, items):
    return write_many(_root(path, items[0][0]), {items[0][0]["file"]: items})


def font_options(info):
    return []


def configure_font(output, info, font=None, tmp_font=None):
    from .. import unchanged_font
    if font or tmp_font:
        raise ValueError("WOLF 字体配置尚未适配；本阶段只处理文本资源")
    return unchanged_font()
