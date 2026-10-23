"""Built-in registry with explicit project-local adapters.

Contract: probe(game) -> detection or None; prepare(info, work) -> resources;
extract(info, resources) -> ExtractionResult; read/write handle one file;
configure_font(output, info, font, tmp_font) -> structured report.
Optional check_build(root, info, changed_files) -> {can_build, status, ...}:
read-only preflight, called before copying and included in verification reports.
Optional finalize_build(output, info, checks) updates resource loading metadata
after text/font writes and returns updated load checks. Unity uses this for catalogs.
Optional prepare_build(root, info, changed_files, *, font_plan=None) returns
load checks before copying; it includes font changes in the files to check.
An adapter accepting font_plan also provides configure_font_plan(output, info,
font_plan). Existing configure_font and check_build adapters remain compatible.
finalize_build receives the load checks returned by prepare_build/check_build.
Optional copy_resources(game, resources, output, info) materializes an engine's
unpacked resources in the new output copy. Default copying is unchanged.
Optional write_many(output, groups, work) -> count and read_many(output, groups,
work) -> {file: values} avoid decoding an entire dataset once per file. Group
values are (Entry, translation) pairs for writes, Entries for reads, in order.
prepare may return input_files (game-relative source inputs). ExtractionResult
owns entries, warnings, reports and resource-root-relative dependencies; extract
does not write metadata into resources or Entry. Core normalizes old tuple
results from trusted single-file adapters through extraction.extract_resources.
All paths passed by Core are pathlib.Path; functions do not print or exit.
"""
from pathlib import Path
from . import rpgmaker, unity, tyrano, wolf, renpy, godot

ADAPTERS = {"rpgmaker": rpgmaker, "unity": unity, "tyrano": tyrano, "wolf": wolf, "renpy": renpy, "godot": godot}


def adapter(info):
    if info.get("temporary_adapter"):
        from .temporary import load
        return load(info["temporary_adapter"])
    try:
        return ADAPTERS[info["family"]]
    except KeyError as exc:
        raise ValueError("没有对应的引擎适配器，不能继续提取或写回") from exc


def detect(game_dir, *, adapter_file=None, godot_executable=None):
    game = Path(game_dir).resolve()
    if not game.is_dir():
        raise ValueError(f"游戏目录不存在: {game}")
    if adapter_file is not None:
        if godot_executable is not None:
            raise ValueError("godot_executable 只用于内置 Godot 适配，不与临时适配器混用")
        from .temporary import describe, load
        record = describe(adapter_file)
        engine = load(record)
        match = engine.probe(game)
        if not isinstance(match, dict) or not match.get("family") or not match.get("evidence"):
            raise ValueError("临时适配器未确认适用游戏；probe 必须返回 family 与非空 evidence")
        info = {**match, "game_dir": str(game), "temporary_adapter": record}
        info["font_options"] = engine.font_options(info)
        return info
    matches = [info for engine in ADAPTERS.values() if (info := engine.probe(game))]
    if len(matches) > 1:
        raise ValueError("目录同时匹配多个引擎，请指定实际游戏子目录")
    info = matches[0] if matches else {"family": "unknown", "variant": "unknown",
                                        "evidence": [], "data_dir": None}
    info["game_dir"] = str(game)
    if godot_executable is not None:
        if info["family"] != "godot":
            raise ValueError("godot_executable 只用于 Godot 游戏")
        godot.select_source(info, godot_executable)
    info["font_options"] = adapter(info).font_options(info) if matches else []
    return info


def unchanged_font():
    return {"status": "unchanged", "note": "未指定字体；字形覆盖和界面显示需要实机检查"}
