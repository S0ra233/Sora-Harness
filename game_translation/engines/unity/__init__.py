"""Unity tables and indexed serialized assets; UnityPy is loaded on demand."""
import configparser
import io
import shutil
from pathlib import Path
from collections import defaultdict
from ...storage import read_file, write_file, safe_join
from . import unity_assets, unity_code, unity_scenes
from .unity_objects import asset_identity
from .unity_localization import inspect as inspect_localization, make_entries as localization_entries
from .unity_scan import detect_unity
from .unity_build import check_build, finalize_build


def probe(game):
    info = detect_unity(str(game))
    if info:
        info.update(family="unity", variant=info["backend"].lower())
    return info


def prepare(info, work):
    return {"root": info["game_dir"], "data_dir": info["data_dir"]}


def extract(info, resources):
    candidate_mode = bool(resources.get("collect_candidates"))
    result = unity_assets.extract_resources(resources["root"], resources["data_dir"],
                                           collect_candidates=candidate_mode)
    if candidate_mode:
        unity_code.extract(info, resources, result)
    _review_sources(result, candidate_mode)
    result.warnings.extend(f"私有归档未解析: {name}" for name in info.get("private_archives", []))
    for entry in result.entries:
        entry.setdefault("scene_kind", "text_group")
    unity_scenes.annotate_entries(result.entries)
    return result


def _review_sources(result, candidate_mode):
    groups = defaultdict(list)
    for entry in result.entries:
        if entry["format"] == "json" or entry.get("structure", {}).get("requires_selection"):
            groups[(entry["file"], str(entry.get("asset")))].append(entry)
    excluded = set()
    for group in groups.values():
        first = group[0]
        name = first.get("asset", {}).get("name", "")
        paths = {e["path"].split("/")[1] for e in group if e["path"].startswith("/")}
        technical = ((first["file"].lower().endswith("/streamingassets/aa/settings.json")
                      and "m_buildTarget" in paths)
                     or (name == "PerformanceTestRunInfo" and {"Player", "Editor"} <= paths))
        result.reports.append({"file": first["file"], "asset": first.get("asset"),
                               "state": "technical" if technical else "needs_confirmation",
                               "reason": "已确认的 Unity 技术配置" if technical else "文本字段显示用途未确认，请审查候选",
                               "entries": len(group),
                               "samples": [{"path": e["path"], "text": e["text"][:120]} for e in group[:3]]})
        if not candidate_mode:
            excluded.update(id(e) for e in group)
    result.entries = [e for e in result.entries if id(e) not in excluded]


def read(path, entries):
    if any(e["format"] in unity_code.FORMATS for e in entries):
        if not all(e["format"] in unity_code.FORMATS for e in entries):
            raise ValueError("代码字符串不能与资源字段混用同一文件")
        return unity_code.read(path, entries)
    return unity_assets.read(path, entries)


def write(path, items):
    if any(e["format"] in unity_code.FORMATS for e, _ in items):
        if not all(e["format"] in unity_code.FORMATS for e, _ in items):
            raise ValueError("代码字符串不能与资源字段混用同一文件")
        return unity_code.write(path, items)
    return unity_assets.write(path, items)


def font_options(info):
    return ["font_plan", "tmp_assetbundle"]


def prepare_build(root, info, changed_files, *, font_plan=None):
    from . import unity_fonts
    changed = set(changed_files)
    if font_plan is not None:
        jobs = unity_fonts.prepare_plan(root, info, font_plan)
        changed.update(job["path"].relative_to(root).as_posix() for job in jobs)
    return check_build(root, info, sorted(changed))


def configure_font_plan(output, info, font_plan):
    from . import unity_fonts
    return unity_fonts.apply_plan(output, info, font_plan)


def configure_font(output, info, font=None, tmp_font=None):
    from .. import unchanged_font
    if font:
        raise ValueError("Unity 需要 TMP 字体 AssetBundle，不能直接使用普通字体文件")
    if not tmp_font:
        return unchanged_font()
    source = Path(tmp_font).resolve()
    plugins = output / "BepInEx/plugins"
    if not source.is_file() or not plugins.is_dir() or not any(
            "autotranslator" in p.name.lower() for p in plugins.rglob("*.dll")):
        raise ValueError("需要现有 TMP Bundle 和已安装的 XUnity.AutoTranslator")
    target = safe_join(output, source.name)
    if target.exists():
        raise FileExistsError("字体 Bundle 名称与游戏文件冲突，请重命名字体文件")
    shutil.copyfile(source, target)
    config_path = output / "BepInEx/config/AutoTranslatorConfig.ini"
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    if config_path.exists():
        config.read_string(read_file(config_path))
    if not config.has_section("Behaviour"):
        config.add_section("Behaviour")
    config.set("Behaviour", "FallbackFontTextMeshPro", source.name)
    buffer = io.StringIO()
    config.write(buffer)
    write_file(config_path, buffer.getvalue())
    return {"status": "configured", "asset": source.name,
            "note": "已配置 TMP 回退；Bundle 版本兼容性与显示效果需实机检查"}
