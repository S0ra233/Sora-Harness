"""RPG Maker detection, extraction, output and font adaptation."""
import json
from pathlib import Path
import shutil
from ...storage import read_file, write_file, write_json, safe_join, file_hash
from ...formats import marshal, rgss, tables
from ...formats.tables import emit_entry, walk_strings, json_pointer
from ...extraction import ExtractionResult

DATABASE_FIELDS = {
    "Actors.json": ("name", "nickname", "profile"),
    "Classes.json": ("name",),
    "Skills.json": ("name", "description", "message1", "message2"),
    "Items.json": ("name", "description"),
    "Weapons.json": ("name", "description"),
    "Armors.json": ("name", "description"),
    "Enemies.json": ("name",),
    "States.json": ("name", "description", "message1", "message2", "message3", "message4"),
}


def _commands(commands, prefix, file, entries):
    scene = file + ":" + json_pointer(prefix)
    speaker = ""
    for index, command in enumerate(commands):
        start = len(entries)
        code, parameters = command.get("code"), command.get("parameters", [])
        position = (*prefix, index, "parameters")
        if code == 101 and len(parameters) >= 5:
            speaker = parameters[4]
            emit_entry(entries, file, json_pointer((*position, 4)), "speaker", speaker, "rpg-json", scene)
        elif code == 101:
            speaker = ""
        slots = {401: ("dialogue", 0), 405: ("scroll", 0),
                 402: ("choice", 1), 320: ("name", 1), 324: ("profile", 1)}
        if code == 102 and parameters and isinstance(parameters[0], list):
            for choice, text in enumerate(parameters[0]):
                emit_entry(entries, file, json_pointer((*position, 0, choice)),
                      "choice", text, "rpg-json", scene, speaker)
        elif code in slots:
            kind, slot = slots[code]
            if slot < len(parameters):
                emit_entry(entries, file, json_pointer((*position, slot)),
                      kind, parameters[slot], "rpg-json", scene, speaker)
        for entry in entries[start:]:
            entry["scene_kind"] = "event"
            entry["structure"] = {"command_index": index, "code": code,
                                  "indent": command.get("indent", 0)}


def probe(game):
    extensions = {".rvdata2": "vx_ace", ".rvdata": "vx", ".rxdata": "xp"}
    archive_types = {".rgss3a": "vx_ace", ".rgss2a": "vx", ".rgssad": "xp"}
    archives = sorted(p for p in game.iterdir() if p.is_file() and p.suffix.lower() in archive_types)
    if len(archives) > 1:
        raise ValueError("存在多个 RGSS 归档，请先确认主归档")
    if archives:
        archive = archives[0]
        return {"family": "rpgmaker", "variant": archive_types[archive.suffix.lower()],
                "archive": str(archive), "data_dir": None, "evidence": [archive.name]}
    for sub in ("www/data", "data", "Data", ""):
        data = game / sub
        if not data.is_dir():
            continue
        files = sorted(p for p in data.iterdir() if p.is_file())
        binary = next((p for p in files if p.suffix.lower() in extensions), None)
        if binary:
            return {"family": "rpgmaker", "variant": extensions[binary.suffix.lower()],
                    "data_dir": str(data), "evidence": [binary.relative_to(game).as_posix()]}
        marker = next((p for p in files if p.name.lower() in
                       ("system.json", "actors.json", "mapinfos.json", "skills.json")), None)
        if marker:
            variant = "mv_mz"
            for filename, name in (("rpg_core.js", "mv"), ("rmmz_core.js", "mz")):
                if any((game / js / filename).is_file() for js in ("js", "www/js")):
                    variant = name
            return {"family": "rpgmaker", "variant": variant, "data_dir": str(data),
                    "evidence": [marker.relative_to(game).as_posix()]}
    return None


def prepare(info, work):
    if info.get("archive"):
        cache = work / "_unpacked" / file_hash(info["archive"])[:16]
        root, data = rgss.unpack_game_dir(info["game_dir"], cache)
        return {"root": root, "data_dir": data}
    return {"root": info["game_dir"], "data_dir": info["data_dir"]}


def extract(info, resources):
    root, data = Path(resources["root"]), Path(resources["data_dir"])
    entries = []
    if info["variant"] in ("mv", "mz", "mv_mz"):
        _extract_rpg_json(data, entries, root)
    else:
        for path in sorted(data.iterdir()):
            if path.suffix.lower() not in (".rvdata2", ".rvdata", ".rxdata"):
                continue
            file = path.relative_to(root).as_posix()
            for entry in marshal.extract_vx(path.name, path.read_bytes()):
                event = ".@list" in entry["path"]
                entry.update(file=file, format="marshal",
                             scene=file + ":" + entry["path"].split(".@list")[0] if event else file,
                             scene_kind="event" if event else "database")
                entries.append(entry)
    return ExtractionResult(entries)


def read(path, entries):
    if entries[0]["format"] != "marshal":
        return tables.read(path, entries)
    obj = marshal.MC.load(path.read_bytes())
    return [marshal.string_value(marshal.resolve_path(obj.root, e["path"])) for e in entries]


def write(path, items):
    if items[0][0]["format"] != "marshal":
        return tables.write(path, items)
    for entry, current in zip((e for e, _ in items), read(path, [e for e, _ in items])):
        tables.check_original(current, entry["text"], entry["path"])
    changed = marshal.apply_vx(path.read_bytes(), [(e["path"], text) for e, text in items])
    marshal.MC.load(changed)
    write_file(path, changed, binary=True)
    return len(items)


def font_options(info):
    return [".ttf", ".otf", ".woff", ".woff2"] if info["variant"] in ("mv", "mz", "mv_mz") else []


def configure_font(output, info, font=None, tmp_font=None):
    from .. import unchanged_font
    if tmp_font:
        raise ValueError("RPG Maker 不支持 TMP 字体 Bundle")
    if not font:
        return unchanged_font()
    source = Path(font).resolve()
    if not source.is_file() or source.suffix.lower() not in font_options(info):
        raise ValueError("MV/MZ 需要现有的 TTF/OTF/WOFF/WOFF2 字体文件")
    # Use the original detection result; staging paths must not be redetected.
    relative = Path(info["data_dir"]).relative_to(info["game_dir"])
    data = output / relative
    root = data.parent if relative.parts else output
    target = safe_join(root, "fonts/game-translation" + source.suffix.lower())
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    system_path = data / "System.json"
    system = json.loads(read_file(system_path) or "{}")
    if "mainFontFilename" in system.get("advanced", {}):
        system["advanced"]["mainFontFilename"] = target.name
        write_json(system_path, system)
    else:
        css_path = root / "fonts/gamefont.css"
        css = read_file(css_path) or ""
        rule = '\n@font-face { font-family: GameFont; src: url("' + target.name + '"); }\n'
        write_file(css_path, css + rule)
    return {"status": "configured", "asset": target.relative_to(output).as_posix(),
            "note": "已配置字体；字形覆盖、字号、换行仍需实机检查"}


def _extract_rpg_json(data_dir, entries, resource_root=None):
    root = Path(resource_root or data_dir)
    for path in sorted(Path(data_dir).glob("*.json")):
        file = path.relative_to(root).as_posix()
        obj = json.loads(read_file(path))
        name = path.name
        if name in DATABASE_FIELDS and isinstance(obj, list):
            for index, item in enumerate(obj):
                if not isinstance(item, dict):
                    continue
                for key in DATABASE_FIELDS[name]:
                    emit_entry(entries, file, json_pointer((index, key)), key,
                          item.get(key), "rpg-json")
        elif name == "System.json":
            for key in ("gameTitle", "currencyUnit", "terms", "elements",
                        "skillTypes", "weaponTypes", "armorTypes", "equipTypes"):
                for pointer, text in walk_strings(obj.get(key), (key,)):
                    emit_entry(entries, file, pointer, "system", text, "rpg-json")
        if name == "CommonEvents.json":
            for index, event in enumerate(obj):
                if event:
                    _commands(event.get("list", []), (index, "list"), file, entries)
        elif name == "Troops.json":
            for index, troop in enumerate(obj):
                if troop:
                    for page, contents in enumerate(troop.get("pages", [])):
                        _commands(contents.get("list", []), (index, "pages", page, "list"), file, entries)
        elif name.startswith("Map") and name != "MapInfos.json" and isinstance(obj, dict):
            emit_entry(entries, file, "/displayName", "map_name", obj.get("displayName"), "rpg-json")
            for index, event in enumerate(obj.get("events") or []):
                if event:
                    for page, contents in enumerate(event.get("pages", [])):
                        _commands(contents.get("list", []),
                                  ("events", index, "pages", page, "list"), file, entries)
