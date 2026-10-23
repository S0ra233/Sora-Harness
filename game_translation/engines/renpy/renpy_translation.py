"""Resolve native translation units and export existing Entry dictionaries."""
import ast
from collections import Counter, defaultdict
from pathlib import Path
import re
import textwrap

from ...extraction import ExtractionResult
from ...formats import renpy as script
from ...storage import read_file, safe_join, write_file
from . import renpy_backend as backend
from . import renpy_resources as resources_api


def _template_units(source, file):
    statements = list(script.statements(source))
    units, strings, languages = [], [], set()
    references = list(re.finditer(r"(?m)^#\s+([^\n]+\.rpy):(\d+)\s*$", source))
    reference_index = -1
    for index, statement in enumerate(statements):
        header = script.TRANSLATE.fullmatch(statement.code.strip())
        if not header:
            continue
        language, identity = header.groups()
        if language != "None":
            languages.add(language)
        if identity in ("strings", "python", "style", "early"):
            continue
        end = index + 1
        while end < len(statements) and (not statements[end].code.strip() or
                                        statements[end].indent > statement.indent):
            end += 1
        body_end = statements[end].start if end < len(statements) else len(source)
        body = source[statement.end:body_end]
        originals = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("# "):
                break
            originals.append(stripped[2:])
        if language == "None":
            original = textwrap.dedent(body).strip() + "\n"
        elif originals:
            original = "\n".join(originals) + "\n"
        else:
            continue
        while reference_index + 1 < len(references) and references[reference_index + 1].start() < statement.start:
            reference_index += 1
        location = references[reference_index] if reference_index >= 0 else None
        units.append({"id": identity.replace(".", "_"), "body": original,
                      "file": location[1] if location else file, "label": identity.rsplit("_", 1)[0],
                      "line": int(location[2]) if location else statement.line,
                      "branch": [], "route": "existing_template"})
    for path in script.template_slots(source):
        if path[0] == "string":
            strings.append({"text": path[2], "type": "ui", "file": file, "label": "",
                            "line": 0, "branch": [], "route": "existing_string_key"})
    return units, strings, languages


def _source_strings(source, file):
    """Known display roles only; arbitrary Python data remains a report."""
    strings, dialogue, candidates, blocks = [], [], [], []
    label = ""
    for statement in script.statements(source):
        code = statement.code.strip()
        if not code:
            continue
        while blocks and statement.indent <= blocks[-1][0]:
            blocks.pop()
        if any(kind.startswith("translate ") for _, kind in blocks):
            continue
        if code.startswith("translate "):
            blocks.append((statement.indent, code))
            continue
        match = re.match(r"label\s+([\w.]+)", code)
        if match:
            label = match[1]
        branch = [kind for _, kind in blocks if kind.startswith(("if ", "elif ", "else:", "menu"))]
        metadata = {"file": file, "label": label, "line": statement.line, "branch": branch}
        screen = any(kind.startswith("screen ") for _, kind in blocks)
        python = any("python" in kind.split() for _, kind in blocks) or code.startswith("$")
        for literal in statement.literals:
            prefix = statement.code[:literal.start - statement.start].rstrip()
            suffix = statement.code[literal.end - statement.start:].lstrip()
            marked = re.search(r"(?<![\w.])(?:_|__|___)\(\s*$", prefix) and suffix.startswith(")")
            display = screen and re.fullmatch(r"\s*(?:text|textbutton)\s*", prefix)
            choice = not prefix.strip() and suffix.startswith((":", "if ")) and any(
                kind.startswith("menu") for _, kind in blocks)
            character = re.search(r"\bCharacter\(\s*$", prefix)
            if marked or display or choice or character:
                try:
                    text = ast.literal_eval(literal.token) if marked or character else literal.value()
                except (ValueError, SyntaxError):
                    continue
                if isinstance(text, str) and text.strip():
                    strings.append({**metadata, "text": text,
                                    "type": "choice" if choice else "name" if character else "ui",
                                    "route": "marked_string" if marked else "display_constant"})
            elif python:
                candidates.append({**metadata, "literal": literal.token,
                                   "reason": "Python 字符串未确认实际显示用途"})
        literal, speaker = script.say_literal(statement)
        if literal is not None and not python and not screen:
            try:
                dialogue.append({**metadata, "text": literal.value(), "speaker": speaker})
            except ValueError as exc:
                candidates.append({**metadata, "literal": literal.token, "reason": str(exc)})
        if code.endswith(":"):
            blocks.append((statement.indent, code))
    return strings, dialogue, candidates


def _collect_scripts(info, resources):
    records, warnings, compiled = [], list(resources["warnings"]), []
    files = {item["file"]: item for item in resources["scripts"]}
    for item in resources["scripts"]:
        if item["file"].endswith(".rpy"):
            records.append({**item, "source": read_file(item["path"]), "units": [], "strings": [], "languages": []})
        else:
            source = files.get(str(Path(item["file"]).with_suffix(".rpy")).replace("\\", "/"))
            if resources_api.compiled_matches_source(info, item, source):
                compiled.append(item)
            else:
                warnings.append(f"{item['file']}: 编译缓存与源脚本未确认一致，需匹配版本 SDK 生成模板")
    if compiled:
        try:
            decoded = backend.read_compiled(compiled, Path(resources["session"]) / "compiled")
            records.extend({**files[record["file"]], **record} for record in decoded)
        except (ImportError, OSError, ValueError) as exc:
            warnings.append(str(exc))
    return records, warnings


def extract(info, resources):
    records, warnings = _collect_scripts(info, resources)
    _record_load_options(info, resources, records)
    units, strings, candidates, languages, dialogue = _resolve_records(records, warnings)
    languages.update(resources["languages"])
    for item in resources["scripts"]:
        parts = Path(item["file"]).parts
        if len(parts) >= 3 and parts[0] == "tl":
            languages.add(parts[1])
    language = _choose_language(languages)
    info["translation_language"] = language
    generated = backend.generate_templates(info["game_dir"], resources["session"], language)
    if generated is not None:
        relative = resources["data_relative"]
        for path in sorted((generated / relative / "tl" / language).rglob("*.rpy")):
            found, extra, _ = _template_units(read_file(path), path.relative_to(generated).as_posix())
            for unit in found:
                units[unit["id"]] = unit
            strings.extend(extra)
        warnings.append("已显式调用 SDK 在工作副本生成模板；此路线会执行项目初始化")
    candidates.extend(_missing_dialogue(units, dialogue))
    if any(re.search(r"(?:chinese|schinese|zh[_-]?(?:cn|hans)|中文|简体|繁体)", value, re.I) for value in languages):
        warnings.append("发现名称疑似中文的已有语言；请先确认是否已有可用汉化")
    root = Path(resources["root"])
    prefix = Path(resources["data_relative"])
    template = (prefix / "tl" / language / "game_translation.rpy").as_posix()
    text, entries = _render_entries(units, strings, template, language)
    startup = (prefix / f"zz_game_translation_{language}.rpy").as_posix()
    write_file(safe_join(root, template), text)
    write_file(safe_join(root, startup), f"init 999 python:\n    config.language = {language!r}\n")
    overlays = [template, startup]
    resources_api.save_sources(resources, records, overlays)
    if candidates:
        warnings.append(f"{len(candidates)} 个未知字符串/缺少翻译 ID 的对白见 extraction_report.json")
    warnings.append("Ren'Py 静态提取不覆盖运行时拼接、图片文字、自定义语句；需流程与实机反馈确认")
    report = {"kind": "renpy_native_translation", "language": language, "existing_languages": sorted(languages),
              "native_dialogue_units": len(units), "string_keys": len({s['text'] for s in strings}),
              "candidates": candidates, "load_issues": info.get("load_issues", []),
              "runtime_verified": False}
    return ExtractionResult(entries, warnings, [report], {resources_api.SOURCE_RECORD, startup})


def _record_load_options(info, resources, records):
    sources = [(record["file"], record["source"]) for record in records
               if "source" in record and not record["file"].replace("\\", "/").startswith("tl/")]
    options, issues = resources_api.source_load_options(sources)
    info["force_archives"] = bool(options.get("force_archives", info["force_archives"]))
    names = options.get("archives")
    if names is not None:
        if not isinstance(names, (list, tuple)) or not all(isinstance(name, str) for name in names):
            issues.append("编译脚本的归档配置不是静态名称列表")
        else:
            existing = [name for name in names if safe_join(info["data_dir"], name + ".rpa").is_file()]
            if existing != resources["archive_order"]:
                issues.append("编译脚本的归档配置与准备时的顺序不一致；需要确认实际加载来源")
    info["load_issues"] = list(dict.fromkeys([*info.get("load_issues", []), *issues]))


def _resolve_records(records, warnings):
    units, strings, candidates, languages, sources = {}, [], [], set(), []
    for record in records:
        file = record["file"]
        warnings.extend(f"{file}: {message}" for message in record.get("warnings", []))
        languages.update(record.get("languages", []))
        for unit in record.get("units", []):
            if unit["id"] in units:
                raise ValueError(f"原始脚本中重复的 Ren'Py 翻译 ID: {unit['id']}")
            units[unit["id"]] = {**unit, "file": file}
        strings.extend({**item, "file": file} for item in record.get("strings", []))
        source = record.get("source")
        if source is not None:
            sources.append((file, source))
    for file, source in sources:
        found, extra, locale = _template_units(source, file)
        languages.update(locale)
        for unit in found:
            units.setdefault(unit["id"], unit)
        strings.extend(extra)
    dialogue = []
    for file, source in sources:
        extra, found, unknown = _source_strings(source, file)
        strings.extend(extra)
        candidates.extend(unknown)
        dialogue.extend(found)
    return units, strings, candidates, languages, dialogue


def _missing_dialogue(units, dialogue):
    coverage, missing, groups = _dialogue_coverage(units), [], defaultdict(list)
    for item in dialogue:
        groups[item["file"]].append(item)
    for file, group in groups.items():
        covered = coverage.get(_logical_file(file), Counter()).copy()
        for item in group:
            if covered[item["text"]]:
                covered[item["text"]] -= 1
            else:
                missing.append({**item, "reason": "对白未取得可靠原生 ID；需要 SDK 模板或专用适配"})
    return missing


def _logical_file(file):
    return Path(str(file).replace("\\", "/").removeprefix("game/")).with_suffix(".rpy").as_posix()


def _dialogue_coverage(units):
    coverage = {}
    for unit in units.values():
        counts = coverage.setdefault(_logical_file(unit["file"]), Counter())
        for statement in script.statements(unit["body"]):
            literal, _ = script.say_literal(statement)
            if literal is not None:
                counts[literal.value()] += 1
    return coverage


def _choose_language(existing):
    language, index = "gt_zh_cn", 1
    while language in existing:
        index += 1
        language = f"gt_zh_cn_{index}"
    return language


def _entry(file, path, text, kind, source, *, speaker="", native_id=None):
    label = source.get("label", "")
    return {"file": file, "path": path, "text": text, "type": kind, "format": "renpy-tl",
            "scene": source["file"] + (":" + label if label else ":strings"),
            "scene_kind": "script_label" if native_id else "text_group", "speaker": speaker,
            "structure": {"source_file": source["file"], "source_line": source.get("line", 0),
                          "label": label, "branch": source.get("branch", []), "native_id": native_id,
                          "condition": source.get("condition"),
                          "route": source.get("route", ""), "text_syntax": "renpy"}}


def _render_entries(units, strings, file, language):
    lines, entries = ["# Generated translation overlay. Original scripts remain in the game.\n"], []
    for identity, unit in units.items():
        if not re.fullmatch(r"\w+", identity):
            raise ValueError(f"不支持的 Ren'Py 翻译 ID: {identity}")
        lines.append(f"translate {language} {identity}:\n")
        body = textwrap.dedent(unit["body"]).strip()
        lines.append(textwrap.indent(body, "    ") + "\n\n")
        index = 0
        for statement in script.statements(body):
            literal, speaker = script.say_literal(statement)
            if literal is None:
                continue
            text = literal.value()
            if text.strip():
                entries.append(_entry(file, ["dialogue", language, identity, index], text,
                                      "dialogue" if speaker else "narration", unit,
                                      speaker=speaker, native_id=identity))
            index += 1
    unique, occurrences = {}, {}
    for item in strings:
        if item["text"].strip():
            unique.setdefault(item["text"], item)
            context = {key: item.get(key) for key in ("file", "line", "label", "branch", "condition", "type")}
            contexts = occurrences.setdefault(item["text"], [])
            if context not in contexts:
                contexts.append(context)
    if unique:
        lines.append(f"translate {language} strings:\n")
    for text, item in unique.items():
        lines.append(f"    old {script.quote_text(text)}\n    new {script.quote_text(text)}\n\n")
        entry = _entry(file, ["string", language, text], text, item["type"], item)
        entry["structure"]["occurrences"] = occurrences[text]
        entries.append(entry)
    return "".join(lines), entries
