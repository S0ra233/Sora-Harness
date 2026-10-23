"""Resolve prepare options once at the public API boundary."""
from dataclasses import dataclass
from pathlib import Path

from .candidates import validate_selection
from .localization import normalize_locale


@dataclass(frozen=True)
class PrepareOptions:
    adapter_file: Path | None
    candidates: dict | None
    text_selection: dict | None
    localization: dict | None


def resolve_adapter_file(work, previous, adapter_file):
    if adapter_file is not None:
        adapter_file = Path(adapter_file).resolve()
        if adapter_file.parent != work or not adapter_file.is_file():
            raise ValueError("临时适配文件必须直接放在本次工作目录中")
    old = (previous or {}).get("engine", {}).get("temporary_adapter")
    if old:
        from .engines.temporary import describe
        adapter_file = adapter_file or Path(old["path"])
        if describe(adapter_file) != old:
            raise ValueError("不能更换已有项目的临时适配代码；请使用新工作目录")
    elif previous and adapter_file is not None:
        raise ValueError("不能将已有项目切换为临时适配器；请使用新工作目录")
    return adapter_file


def _inherit(previous, key, value, message):
    if not previous:
        return value
    old = previous.get(key)
    if value is None:
        return old
    if value != old:
        raise ValueError(message)
    return value


def _text_selection(previous, locale, overrides):
    if overrides is not None and (not isinstance(overrides, dict) or
            any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                for k, v in overrides.items())):
        raise ValueError("text_overrides 必须是来源 ID 到语言名称的映射")
    if locale is not None and not normalize_locale(locale):
        raise ValueError("text_locale 不能为空")
    selection = ({"locale": normalize_locale(locale), "overrides": overrides or {}}
                 if locale is not None else None)
    selection = _inherit(previous, "text_selection", selection,
                         "不能改变已有工作目录的文本语言选择，请使用新工作目录")
    if overrides is not None:
        if selection is None:
            raise ValueError("text_overrides 需要同时指定 text_locale")
        if locale is None and overrides != selection["overrides"]:
            raise ValueError("不能隐式改变已有语言确认记录，请使用新工作目录")
    return selection


def resolve_options(game, previous, *, adapter_file, candidate_selection,
                    text_locale, text_overrides, source_locale, target_locale):
    if candidate_selection is not None:
        candidate_selection = validate_selection(candidate_selection, game)
    candidates = _inherit(previous, "candidate_selection", candidate_selection,
                          "不能改变已有项目的候选选择；请使用新工作目录")
    text = _text_selection(previous, text_locale, text_overrides)
    if (source_locale is None) != (target_locale is None):
        raise ValueError("source_locale 与 target_locale 必须一起指定")
    localization = ({"source_locale": source_locale, "target_locale": target_locale}
                    if source_locale is not None else None)
    localization = _inherit(previous, "localization", localization,
                            "不能改变已有工作目录的语言模式，请使用新工作目录")
    if localization and text:
        raise ValueError("文本来源筛选与目标语言补全不能同时使用")
    if candidates and (localization or text or adapter_file is not None):
        raise ValueError("候选选择本轮单独使用；语言字段由 Agent 选组，不与语言筛选、补全或临时适配混用")
    return PrepareOptions(adapter_file, candidates, text, localization)
