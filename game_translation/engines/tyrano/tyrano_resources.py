"""Loose Tyrano scripts and Electron ASAR, sharing the same script parser."""
from pathlib import Path
import shutil
import tempfile

from ...formats.asar import Archive, electron_fuses
from ...storage import safe_join


def probe(game):
    archive_path = game / "resources/app.asar"
    archive = None
    if archive_path.is_file():
        try:
            archive = Archive(archive_path)
        except (ValueError, OSError):
            pass
    if archive and any(name.startswith("data/scenario/") and name.lower().endswith(".ks")
                       for name in archive.nodes) and "package.json" in archive.nodes:
        if (game / "resources/app").is_dir():
            raise ValueError("Electron 同时存在 app.asar 与 app 目录，需先由 Agent 确认该版本实际加载来源")
        return {"family": "tyrano", "variant": "asar", "data_dir": None,
                "asar": {"archive": "resources/app.asar", "mount": "resources/app"},
                "evidence": ["resources/app.asar: ASAR index with package.json and data/scenario/*.ks"]}
    for relative in ("data/scenario", "resources/app/data/scenario"):
        data = game / relative
        if data.is_dir():
            return {"family": "tyrano", "variant": "ks", "data_dir": str(data),
                    "evidence": [relative]}
    return None


def prepare(info, work):
    if not info.get("asar"):
        return {"root": info["game_dir"], "data_dir": info["data_dir"]}
    session_root = work / "_tyrano"
    session_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="prepare-", dir=session_root)) / "resources"
    game, container = Path(info["game_dir"]), info["asar"]
    archive = Archive(safe_join(game, container["archive"]))
    mount = safe_join(root, container["mount"])
    inputs = {container["archive"]}
    for name in sorted(archive.nodes):
        if not name.startswith("data/scenario/") or not name.lower().endswith(".ks"):
            continue
        target = safe_join(mount, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(archive.read(name))
        resolved = archive.resolve(name)
        if archive.nodes[resolved][1]:
            inputs.add(archive.external_path(resolved).relative_to(game).as_posix())
    return {"root": str(root), "data_dir": str(mount / "data/scenario"),
            "input_files": sorted(inputs)}


def copy_resources(game, resources, output, info):
    # ASAR text snapshots only live in WORK. Output keeps the game's layout;
    # write_many replaces script members in its actual archive.
    shutil.copytree(game, output, dirs_exist_ok=True)


def check_build(root, info, changed_files):
    if not info.get("asar"):
        return {"can_build": True, "status": "loose_scripts", "runtime_verified": False}
    checks = []
    for path in sorted(Path(root).glob("*.exe")):
        result = electron_fuses(path)
        checks.append({"file": path.name, **result})
    blocked = [c for c in checks if c.get("embedded_asar_integrity") or c["status"] == "unknown"]
    return {"can_build": not blocked, "status": "needs_agent_loader_support" if blocked else "asar_repack",
            "archive": info["asar"]["archive"], "executables": checks,
            "runtime_verified": False,
            "note": "EXE 内嵌 ASAR 校验需单独处理，未修改 EXE 或关闭校验。" if blocked else
                    "重建输出归档；未确认自定义校验或游戏运行。"}


def entry_container(info, file):
    container = info.get("asar")
    if not container:
        return None
    prefix = container["mount"] + "/"
    if not file.startswith(prefix):
        raise ValueError("Tyrano 剧本不在 ASAR 工作挂载目录中")
    return {"format": "asar", "archive": container["archive"], "path": file[len(prefix):]}


def _archive_for(root, entry, archives):
    container = entry["container"]
    if container.get("format") != "asar":
        raise ValueError("未知 Tyrano 容器类型")
    name = container["archive"]
    if name not in archives:
        archives[name] = Archive(safe_join(root, name))
    return archives[name]


def read_many(output, groups, read_content):
    archives, result = {}, {}
    for file, entries in groups.items():
        first = entries[0]
        if "container" in first:
            _same_container(entries)
            archive = _archive_for(output, first, archives)
            content = archive.read(first["container"]["path"])
        else:
            content = safe_join(output, file).read_bytes()
        result[file] = read_content(content, entries)
    return result


def write_many(output, groups, read_content, apply_content):
    from ...storage import write_file
    archives, replacements, count = {}, {}, 0
    for file, items in groups.items():
        entries = [entry for entry, _ in items]
        first = entries[0]
        if "container" in first:
            _same_container(entries)
            archive = _archive_for(output, first, archives)
            content = archive.read(first["container"]["path"])
        else:
            content = safe_join(output, file).read_bytes()
        if read_content(content, entries) != [entry["text"] for entry in entries]:
            raise ValueError(f"Tyrano 原值与索引不一致: {file}")
        changed = apply_content(content, [(entry["path"], text) for entry, text in items])
        if "container" in first:
            container = first["container"]
            replacements.setdefault(container["archive"], {})[container["path"]] = changed
        else:
            write_file(safe_join(output, file), changed, binary=True)
        count += len(items)
    for name, values in replacements.items():
        archives[name].replace(values)
    return count


def _same_container(entries):
    if any(e.get("container") != entries[0]["container"] for e in entries):
        raise ValueError("同一剧本包含不一致的容器定位")
