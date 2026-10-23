"""Container detection, loading paths and editable resource views."""
from pathlib import Path
import shutil
import struct

from ...formats.godot import config_strings
from ...storage import safe_join
from . import godot_backend as backend


TEXT_SUFFIXES = {".json", ".csv", ".tsv", ".txt", ".properties", ".xml", ".po", ".tscn", ".tres"}
BINARY_SUFFIXES = {".scn", ".res", ".translation"}


def _pack_header(handle, offset):
    handle.seek(offset)
    header = handle.read(24)
    if len(header) < 20 or header[:4] != b"GDPC":
        return None
    version, major, minor, revision = struct.unpack("<4I", header[4:20])
    if major not in {2, 3, 4} or minor > 100 or version > 10:
        return None
    flags = struct.unpack("<I", header[20:24])[0] if version >= 2 and len(header) == 24 else 0
    return {"pck_format": version, "major": major, "minor": minor, "revision": revision,
            "pack_offset": offset, "encrypted_directory": bool(flags & 1)}


def pack_info(path):
    """Confirm a standalone header or the standard embedded-PCK footer."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        result = _pack_header(handle, 0)
        if result:
            return {**result, "embedded": False}
        if size < 12:
            return None
        handle.seek(size - 12)
        footer = handle.read(12)
        if footer[8:] == b"GDPC":
            offset = size - 12 - struct.unpack("<Q", footer[:8])[0]
            if 0 <= offset < size - 12 and (result := _pack_header(handle, offset)):
                return {**result, "embedded": True}
        offset = _pe_pack_offset(handle, size)
        if offset is not None and (result := _pack_header(handle, offset)):
            return {**result, "embedded": True}
    return None


def _pe_pack_offset(handle, size):
    """Also recognize exported Windows executables with a PCK PE section."""
    handle.seek(0)
    dos = handle.read(64)
    if len(dos) < 64 or dos[:2] != b"MZ":
        return None
    position = struct.unpack("<I", dos[60:64])[0]
    if position + 24 > size:
        return None
    handle.seek(position)
    header = handle.read(24)
    if header[:4] != b"PE\0\0":
        return None
    count, optional = struct.unpack("<H", header[6:8])[0], struct.unpack("<H", header[20:22])[0]
    handle.seek(position + 24 + optional)
    for _ in range(count):
        section = handle.read(40)
        if len(section) != 40:
            return None
        if section[:8].rstrip(b"\0").lower() in {b"pck", b".pck"}:
            return struct.unpack("<I", section[20:24])[0]
    return None


def probe(game):
    packages = []
    files = [path for path in sorted(game.iterdir()) if path.is_file()]
    for path in files:
        if (record := pack_info(path)) is not None:
            packages.append({"file": path.relative_to(game).as_posix(), **record})
    project = game / "project.godot"
    binary_project = game / "project.binary"
    binary_config = binary_project.is_file() and binary_project.read_bytes()[:4] == b"ECFG"
    if not packages and not project.is_file() and not binary_config:
        return None
    launchers = _launchers(files, packages)
    evidence = [f"{p['file']}: GDPC 头结构，Godot {p['major']}.{p['minor']}，"
                + ("EXE 内嵌资源包" if p["embedded"] else "独立 PCK") for p in packages]
    if project.is_file():
        evidence.append("project.godot: Godot 项目配置")
    elif binary_config:
        evidence.append("project.binary: ECFG 项目配置头")
    evidence.extend(f"{item['executable']} → {item['container']}: {item['loading']}" for item in launchers)
    info = {"family": "godot", "variant": "pck" if packages else "loose",
            "data_dir": str(game), "containers": packages, "launchers": launchers,
            "evidence": evidence}
    if len(launchers) == 1:
        launcher = launchers[0]
        container = next(p for p in packages if p["file"] == launcher["container"])
        _set_source(info, container, launcher["executable"])
    else:
        container = packages[0] if not launchers and len(packages) == 1 else None
        _set_source(info, container)
        if packages and container is None:
            info["load_issues"].append(
                "尚未确定 Godot 启动入口；请由 Agent 确认实际启动 EXE，使用 --godot-exe 指定。"
                "自定义 --main-pack 或额外资源包的加载关系需另行调查，不能自动合并")
    return info


def _launchers(files, packages):
    """Follow Windows export startup order; pack count does not define its role."""
    by_name = {p["file"].casefold(): p for p in packages}
    launchers = []
    for path in files:
        if path.suffix.lower() != ".exe":
            continue
        embedded = by_name.get(path.name.casefold())
        if embedded and embedded["embedded"]:
            container, loading = embedded, "embedded"
        else:
            container = None
            loading = "same_name"
            for name in (path.stem + ".pck", path.name + ".pck"):
                candidate = by_name.get(name.casefold())
                if candidate and not candidate["embedded"]:
                    container = candidate
                    break
        if container:
            launchers.append({"executable": path.name, "container": container["file"], "loading": loading})
    return launchers


def _set_source(info, container, executable=None):
    info.update(container=container, executable=executable, load_issues=[])
    info["additional_containers"] = [p["file"] for p in info["containers"] if p != container]


def source_identity(info):
    """Small, stable provenance shared by the preview and prepared project."""
    return {"executable": info.get("executable"),
            "container": (info.get("container") or {}).get("file")}


def select_source(info, executable=None, *, expected=None):
    """Select one startup source, or inherit a recorded choice before unpacking."""
    requested = executable if executable is not None else (expected or {}).get("executable")
    if requested is not None:
        game = Path(info["game_dir"])
        path = Path(requested)
        path = (path if path.is_absolute() else game / path).resolve()
        try:
            name = path.relative_to(game).as_posix()
        except ValueError as exc:
            raise ValueError("Godot 启动 EXE 必须位于本次游戏目录中") from exc
        launcher = next((item for item in info["launchers"]
                         if item["executable"].casefold() == name.casefold()), None)
        if launcher is None:
            raise ValueError(f"所选 EXE 没有已确认的内嵌或同名 Godot 包: {name}；自定义加载交给 Agent 调查")
        container = next(p for p in info["containers"] if p["file"] == launcher["container"])
        _set_source(info, container, launcher["executable"])
    elif expected is not None:
        file = expected.get("container")
        container = next((p for p in info["containers"] if p["file"] == file), None)
        if (file is not None and container is None) or (file is None and info["containers"]):
            raise ValueError("已记录的 Godot 主包或松散来源已改变；请使用新工作目录或重新预览")
        _set_source(info, container)
    if expected is not None and any(source_identity(info).get(key) != value for key, value in expected.items()):
        raise ValueError("Godot 启动入口或主包与已有记录不同；请使用新工作目录或重新预览")
    return info


def resource_version(path, fallback=None):
    with path.open("rb") as handle:
        header = handle.read(24)
    if header.startswith(b"RSRC") and len(header) == 24:
        endian = ">" if struct.unpack("<I", header[4:8])[0] else "<"
        major, minor = struct.unpack(endian + "2I", header[12:20])
        return {"major": major, "minor": minor}
    if fallback:
        return {"major": fallback["major"], "minor": fallback["minor"]}
    if path.suffix.lower() in {".tscn", ".tres"}:
        import re
        match = re.search(r"\bformat=(\d+)", path.read_text(encoding="utf-8-sig")[:1000])
        major = {1: 2, 2: 3, 3: 4}.get(int(match[1])) if match else None
        if major:
            return {"major": major, "minor": 0}
    return {"major": 0, "minor": 0}


def prepare(info, work):
    if info["load_issues"]:
        raise ValueError("；".join(info["load_issues"]))
    directory = backend.session(work, "prepare")
    game, container = Path(info["game_dir"]), info["container"]
    root = backend.unpack(safe_join(game, container["file"]), directory) if container else game
    aliases, shadows, dependencies = _loading_paths(root)
    documents, warnings = _documents(root, aliases, shadows, directory, container)
    info["resource_documents"] = documents
    info["resource_warnings"] = warnings
    info["translation_paths"] = _translation_paths(root, directory, aliases, warnings)
    inputs = [container["file"]] if container else []
    if info.get("executable") and info["executable"] not in inputs:
        inputs.append(info["executable"])
    return {"root": str(root), "documents": documents, "source_files": sorted(dependencies),
            "work": str(work), "input_files": inputs}


def _loading_paths(root):
    aliases, shadows, dependencies = {}, set(), set()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".import", ".remap"}:
            continue
        file = path.relative_to(root).as_posix()
        dependencies.add(file)
        fields = config_strings(path.read_text(encoding="utf-8-sig"))
        destinations = fields.get(("remap", "path"), fields.get(("deps", "dest_files"), []))
        source = file.rsplit(".", 1)[0]
        for destination in destinations:
            if destination.startswith("res://"):
                target = destination[6:]
                if safe_join(root, target).is_file():
                    aliases[target] = source
                    shadows.add(source)
    return aliases, shadows, dependencies


def _documents(root, aliases, shadows, directory, container):
    documents, jobs, warnings = [], [], []
    for path in sorted(root.rglob("*")):
        suffix = path.suffix.lower()
        if not path.is_file() or suffix not in TEXT_SUFFIXES | BINARY_SUFFIXES:
            continue
        file = path.relative_to(root).as_posix()
        if file in shadows:
            continue
        document = {"file": file, "resource_path": "res://" + aliases.get(file, file),
                    "view": str(path), "binary": suffix in BINARY_SUFFIXES,
                    **resource_version(path, container)}
        documents.append(document)
        if document["binary"]:
            view = safe_join(directory / "views", file + ".tres")
            document["view"] = str(view)
            jobs.append({"id": file, "operation": "to_text", "input": str(path),
                         "output": str(view), "resource_path": document["resource_path"]})
    if jobs:
        results = backend.bridge(jobs, backend.session(directory, "convert"))
        by_file = {d["file"]: d for d in documents}
        for result in results:
            document = by_file[result["id"]]
            if result.get("error"):
                document["error"] = result["error"]
                warnings.append(f"Godot 资源未转换: {document['file']}：{result['error']}")
            else:
                document["resource_type"] = result["resource_type"]
                details = result.get("resource_info", {})
                if details.get("ver_major") in {2, 3, 4}:
                    document.update(major=details["ver_major"], minor=details.get("ver_minor", document["minor"]))
    return documents, warnings


def _translation_paths(root, directory, aliases, warnings):
    config = root / "project.godot"
    if not config.is_file() and (root / "project.binary").is_file():
        try:
            config = backend.project_config(root / "project.binary", directory)
        except ValueError as exc:
            warnings.append(f"Godot 项目语言配置未读取: {exc}")
            return None
    if not config.is_file():
        return None
    fields = config_strings(config.read_text(encoding="utf-8-sig"))
    originals = fields.get(("locale", "translations"), fields.get(("internationalization", "locale/translations"), []))
    inverse = {source: target for target, source in aliases.items()}
    return [inverse.get(name[6:], name[6:]) for name in originals if name.startswith("res://")]


def copy_resources(game, resources, output, info):
    # Packed games keep their native loader: unpacked editor views are not
    # installed beside the executable as if they were runtime resources.
    shutil.copytree(game, output, dirs_exist_ok=True)


def materialize(output, container, work, purpose):
    directory = backend.session(work, purpose)
    root = backend.unpack(safe_join(output, container["file"]), directory) if container else output
    return root, directory
