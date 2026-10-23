"""Ren'Py layout, source precedence, and output overlays."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from ...storage import read_file, safe_join, write_json
from ...formats.renpy import statements
from . import renpy_backend as backend


SOURCE_RECORD = "_renpy_sources.json"


def _script_files(data):
    for folder, directories, files in os.walk(data, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in ("cache", "saves")
                                and not (Path(folder) / name).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            if path.suffix.lower() in (".rpy", ".rpyc") and not path.is_symlink():
                yield path


def probe(game):
    for data in (game / "game", game):
        if not data.is_dir():
            continue
        runtime = game / "renpy"
        if not runtime.is_dir() and (game.parent / "renpy").is_dir():
            runtime = game.parent / "renpy"
        evidence = []
        if runtime.is_dir() and (data / "script.rpy").is_file():
            evidence.append("Ren'Py 运行库与 script.rpy")
        elif _source_project(data):
            evidence.append("script.rpy 中的启动 label 与 Ren'Py Character 声明")
        for path in data.glob("*.rpyc"):
            with path.open("rb") as handle:
                if handle.read(10).startswith(b"RENPY RPC2"):
                    evidence.append(path.relative_to(game).as_posix() + ": RPC2 文件头")
                    break
        for path in data.glob("*.rpa"):
            with path.open("rb") as handle:
                if handle.read(8).startswith(b"RPA-") and runtime.is_dir():
                    evidence.append(path.relative_to(game).as_posix() + ": RPA 文件头与 Ren'Py 运行库")
                    break
        if runtime.is_dir() and data == game / "game":
            evidence.append("renpy/ 运行库与 game/ 目录")
        if not evidence:
            continue
        version, magic = _runtime_metadata(runtime)
        return {"family": "renpy", "variant": "native_translation", "version": version,
                "data_dir": str(data), "runtime_dir": str(runtime), "rpyc_magic": magic,
                "translation_language": "gt_zh_cn", "evidence": evidence}
    return None


def _source_project(data):
    path = data / "script.rpy"
    if not path.is_file():
        return False
    try:
        source = read_file(path)
        code = "\n".join(statement.code.strip() for statement in statements(source))
    except (OSError, UnicodeError, ValueError):
        return False
    return bool(re.search(r"(?m)^label\s+start\s*(?:\([^\n]*\))?\s*:", code)
                and re.search(r"(?m)^define\s+\w+\s*=\s*Character\s*\(", code))


def _runtime_metadata(runtime):
    version, magic = "unknown", None
    init = runtime / "__init__.py"
    if init.is_file():
        text = init.read_text(encoding="utf-8-sig")
        match = re.search(r"(?m)^version_tuple\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", text)
        if match:
            version = ".".join(match.groups())
    script = runtime / "script.py"
    if script.is_file():
        match = re.search(r"(?m)^RPYC_MAGIC\s*=\s*(b?[\"'][^\r\n]+)",
                          script.read_text(encoding="utf-8-sig"))
        if match:
            try:
                value = ast.literal_eval(match[1].split(" #", 1)[0].strip())
                magic = value.hex() if isinstance(value, bytes) else value.encode("utf-8").hex()
            except (ValueError, SyntaxError):
                pass
    return version, magic


def _load_options(paths):
    sources = [(path.name, read_file(path)) for path in paths
               if path.suffix.lower() == ".rpy" and "tl" not in path.parts]
    return source_load_options(sources)


def source_load_options(sources):
    options, issues = {}, []
    pattern = r"(?:\$\s*|define\s+)?config\.(archives|force_archives|searchpath|file_open_callback|loadable_callback)\s*=\s*(.+)"
    for file, source in sources:
        for statement in statements(source):
            code = statement.code.strip()
            if re.match(r"(?:\$\s*)?config\.(?:archives|searchpath)\s*(?:\+=|\.\s*(?:append|extend|insert)\s*\()", code):
                issues.append(f"{file}: 资源加载列表被动态修改，需要人工确认顺序")
                continue
            match = re.fullmatch(pattern, code, re.S)
            if match is None:
                continue
            name, value = match.groups()
            try:
                options[name] = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                issues.append(f"{file}: config.{name} 是动态设置，需要人工确认加载关系")
    if options.get("searchpath") or options.get("file_open_callback") or options.get("loadable_callback"):
        issues.append("自定义搜索路径或加载回调需要人工确认")
    return options, issues


def prepare(info, work):
    game, data = Path(info["game_dir"]), Path(info["data_dir"])
    parent = work / "_renpy"
    parent.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="prepare-", dir=parent))
    root = session / "resources"
    root.mkdir()
    loose = list(_script_files(data))
    options, issues = _load_options(loose)
    info["load_issues"] = issues
    info["force_archives"] = bool(options.get("force_archives"))
    selected = {path.relative_to(data).as_posix(): {"path": str(path), "file": path.relative_to(data).as_posix(),
                "origin": "loose"} for path in loose}
    archives = _archives(data, options)
    warnings = _merge_archives(archives, session, selected, info["force_archives"])
    input_files = {path.relative_to(game).as_posix() for path in loose}
    input_files.update(path.relative_to(game).as_posix() for path in archives)
    for name in ("__init__.py", "script.py"):
        path = Path(info["runtime_dir"]) / name
        if path.is_file() and game in path.parents:
            input_files.add(path.relative_to(game).as_posix())
    tl = data / "tl"
    languages = sorted(path.name for path in tl.iterdir() if path.is_dir()) if tl.is_dir() else []
    return {"root": str(root), "session": str(session), "scripts": sorted(selected.values(), key=lambda x: x["file"]),
            "input_files": sorted(input_files), "warnings": warnings,
            "languages": languages,
            "archive_order": [path.relative_to(data).with_suffix("").as_posix() for path in archives],
            "data_relative": data.relative_to(game).as_posix()}


def _archives(data, options):
    if "archives" in options:
        names = options["archives"]
        if not isinstance(names, (list, tuple)) or not all(isinstance(name, str) for name in names):
            raise ValueError("config.archives 不是静态归档名称列表；需要人工确认")
        paths = [safe_join(data, name + ".rpa") for name in names]
        # Ren'Py permits absent optional archives alongside loose resources.
        return [path for path in paths if path.is_file()]
    # Standard Ren'Py startup puts reverse-sorted archive names first.
    return sorted(data.glob("*.rpa"), reverse=True)


def _merge_archives(archives, session, selected, force_archives):
    warnings, archive_names = [], set()
    for index, archive in enumerate(archives):
        directory = session / "archives" / str(index)
        directory.mkdir(parents=True)
        try:
            members = backend.unpack_scripts(archive, directory)
        except Exception as exc:
            # One archive is a tool boundary; keep its failure and work files.
            warnings.append(f"归档 {archive.name} 未读取: {exc}")
            continue
        for name in members:
            if name in archive_names:
                continue
            archive_names.add(name)
            if force_archives or name not in selected:
                selected[name] = {"path": str(safe_join(directory, name)), "file": name,
                                  "origin": "archive", "archive": archive.name}
    return warnings


def compiled_matches_source(info, compiled, source):
    if source is None:
        return True
    if info.get("rpyc_magic") is None:
        return False
    expected = hashlib.md5(Path(source["path"]).read_bytes() + bytes.fromhex(info["rpyc_magic"])).digest()
    with Path(compiled["path"]).open("rb") as handle:
        if handle.seek(0, 2) < 16:
            return False
        handle.seek(-16, 2)
        return handle.read(16) == expected


def save_sources(resources, records, overlays):
    write_json(Path(resources["root"]) / SOURCE_RECORD,
               {"data_relative": resources["data_relative"], "scripts": records, "overlays": overlays})


def copy_resources(game, resources, output, info):
    record = json.loads((resources / SOURCE_RECORD).read_text(encoding="utf-8"))
    shutil.copytree(game, output, dirs_exist_ok=True)
    for name in record["overlays"]:
        source, target = safe_join(resources, name), safe_join(output, name)
        if target.exists() or target.with_suffix(".rpyc").exists():
            raise ValueError(f"Ren'Py 输出路径与原游戏资源冲突: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
