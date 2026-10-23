"""Public read-only font inventory for Agent and desktop callers."""
from .engines import detect


def inspect_fonts(game_dir, *, characters=""):
    info = detect(game_dir)
    if info["family"] != "unity":
        raise ValueError("字体引用扫描目前支持 Unity")
    from .engines.unity.unity_fonts import inspect
    return inspect(info, characters)
