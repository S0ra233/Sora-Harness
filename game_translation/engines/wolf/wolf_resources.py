"""WOLF resource copies and archive routing, separate from text interpretation."""
from pathlib import Path
import shutil

from ...storage import safe_join
from . import wolf_backend as backend


def header(path, size=32):
    with Path(path).open("rb") as handle:
        return handle.read(size)


def probe(game):
    data = next((game / name for name in ("Data", "data") if (game / name).is_dir()), None)
    if data is None:
        return None
    loose = data / "BasicData" / "Game.dat"
    basic = data / "BasicData.wolf"
    evidence = []
    if loose.is_file() and header(loose)[1:6] == b"W\0\0OL":
        evidence.append(loose.relative_to(game).as_posix() + ": WOLF Game.dat header")
    elif basic.is_file() and any(
            (game / name).is_file() for name in ("Game.exe", "GamePro.exe")):
        kind = "DXArchive header" if header(basic)[:2] == b"DX" else "possibly encrypted archive"
        evidence.append(basic.relative_to(game).as_posix() + ": WOLF core package + executable layout; " + kind)
    if not evidence:
        return None
    archives = [p.relative_to(game).as_posix() for p in sorted(data.rglob("*"))
                if p.is_file() and (p.suffix.lower() == ".wolf" or header(p, 2) == b"DX")]
    return {"family": "wolf", "variant": "wolftl", "data_dir": str(data),
            "data_relative": data.relative_to(game).as_posix(),
            "archives": archives, "evidence": evidence}


def prepare(info, work):
    backend.executable("text")
    if info["archives"]:
        backend.executable("unpack")
    game = Path(info["game_dir"])
    source = safe_join(game, info["data_relative"])
    directory = backend.session(work, "prepare")
    root = directory / "resources"
    data = safe_join(root, info["data_relative"])
    shutil.copytree(source, data)
    for index, name in enumerate(info["archives"]):
        archive = safe_join(root, name)
        target = archive.with_suffix("")
        if target.exists():
            raise ValueError(f"归档和同名松散目录同时存在，需先确定加载来源: {name}")
        backend.run("unpack", [archive], directory, f"unpack-{index}")
        if not target.is_dir() or not any(target.iterdir()):
            raise ValueError(f"未找到归档解包目录: {target}；工作副本已保留")
    game_data = data / "BasicData" / "Game.dat"
    if not game_data.is_file():
        raise ValueError("解包后未找到 WOLF BasicData/Game.dat；请检查后端日志与保留的工作副本")
    # WolfTL confirms the format by exporting Game.json. Do not reject encrypted
    # inner headers that the backend itself may already support.
    return {"root": str(root), "data_dir": str(data),
            "dump": str(backend.export(data, directory / "export")),
            "input_files": [p.relative_to(game).as_posix() for p in sorted(source.rglob("*")) if p.is_file()]}


def documents(data, dump):
    """Map WolfTL's flat dump names back to real files; surface skipped maps."""
    data, dump = Path(data), Path(dump)
    result, issues = [], []
    maps = {}
    for path in sorted(data.rglob("*.mps")):
        if path.stem in maps:
            raise ValueError(f"WolfTL 地图导出名称冲突，不能可靠定位: {maps[path.stem]} / {path}")
        maps[path.stem] = path
    for name, path in maps.items():
        asset = "mps/" + name + ".json"
        if (dump / asset).is_file():
            result.append((path, asset, "map"))
        else:
            issues.append(f"WolfTL 未解析地图: {path.relative_to(data).as_posix()}")
    for path in sorted((dump / "common").glob("*.json")):
        result.append((data / "BasicData/CommonEvent.dat", path.relative_to(dump).as_posix(), "common"))
    for path in sorted((dump / "db").glob("*.json")):
        result.append((data / "BasicData" / (path.stem + ".dat"), path.relative_to(dump).as_posix(), "database"))
    result.append((data / "BasicData/Game.dat", "Game.json", "game"))
    return result, issues


def encoding(path, kind):
    content = header(path)
    if kind == "map":
        magic, slot, start = bytes(10) + b"WOLFM\0\0\0\0\0", 16, 0
    else:
        magic = b"W\0\0OL\0F" + (b"C" if kind == "common" else b"M") + b"\0"
        slot, start = (8 if kind == "game" else 5), 1
    raw = content[start:start + len(magic)]
    utf8 = bytearray(magic)
    utf8[slot] = 0x55
    if raw == bytes(utf8):
        return "utf-8"
    if raw == magic:
        return "cp932"
    # Encrypted/compressed variants can still be read by WolfTL; until the
    # encoding is known we must not let its Windows codepage writer lose text.
    return None


def copy_resources(game, resources, output, info):
    excluded = set(info["archives"])
    def ignore_archives(folder, names):
        return [name for name in names if (Path(folder) / name).relative_to(game).as_posix() in excluded]
    shutil.copytree(game, output, ignore=ignore_archives, dirs_exist_ok=True)
    source = safe_join(resources, info["data_relative"])
    def ignore_cached_archives(folder, names):
        return [name for name in names if (Path(folder) / name).relative_to(resources).as_posix() in excluded]
    shutil.copytree(source, safe_join(output, info["data_relative"]),
                    ignore=ignore_cached_archives, dirs_exist_ok=True)
