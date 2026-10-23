"""Ren'Py adapter: native translation overlays, with shared project state."""
from pathlib import Path
import shutil

from ...formats import renpy as script
from ...storage import safe_join, write_file
from .renpy_resources import probe, prepare, copy_resources
from .renpy_translation import extract


read = script.read
write = script.write


def check_build(root, info, changed_files):
    issues = list(info.get("load_issues", []))
    if info.get("force_archives"):
        issues.append("config.force_archives 会忽略松散翻译文件；需要单独适配归档写回")
    return {"can_build": not issues, "status": "needs_loader_support" if issues else "native_translation_overlay",
            "language": info["translation_language"], "issues": issues, "runtime_verified": False,
            "note": "仅确认当前适配的加载边界；脚本启动与字体显示需实机反馈"}


def font_options(info):
    return [".ttf", ".otf"]


def configure_font(output, info, font=None, tmp_font=None):
    from .. import unchanged_font
    if tmp_font:
        raise ValueError("Ren'Py 不使用 Unity TMP 字体")
    if not font:
        return unchanged_font()
    source = Path(font).resolve()
    if not source.is_file() or source.suffix.lower() not in font_options(info):
        raise ValueError("Ren'Py 需要现有的 TTF/OTF 字体文件")
    data = Path(info["data_dir"]).relative_to(info["game_dir"])
    language = info["translation_language"]
    asset = Path("tl") / language / ("game_translation_font" + source.suffix.lower())
    target = safe_join(output, (data / asset).as_posix())
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    font_name = script.quote_text(asset.as_posix())
    configuration = (f"translate {language} style default:\n    font {font_name}\n\n"
                     f"translate {language} python:\n    if 'gui' in globals():\n"
                     f"        gui.text_font = {font_name}\n        gui.system_font = {font_name}\n")
    write_file(safe_join(output, (data / "tl" / language / "game_translation_font.rpy").as_posix()), configuration)
    return {"status": "configured", "asset": (data / asset).as_posix(), "language": language,
            "note": "已设置原生语言字体与默认样式；自定义样式、字形和界面布局需实机反馈"}
