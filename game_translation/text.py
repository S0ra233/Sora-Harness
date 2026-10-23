"""Protection and validation of game control codes."""
import re
from collections import Counter

HAS_TEXT = re.compile(r"[a-zA-Z\u3040-\u30ff\u3400-\u9fff]")


CONTROL_TAG_RE = re.compile(
    r"\\[a-zA-Z]+(?:\[[^\]\r\n]*\])?|"
    r"\\[.!|^><{}$]|"
    r"</?[a-z][^>\r\n]*>|"
    r"\{(?:\d+|[a-zA-Z_]\w*)(?:[,:][^{}\r\n]+)?\}|"
    r"%(?:\d+\$)?[-+0 #]*\d*(?:\.\d+)?[sdif]|%%|"
    r"\|[a-zA-Z0-9_]+:[^|\r\n]+|"
    r"\[(?:PlayerNick_\d+|[a-zA-Z_]\w*(?:\s+[^\]\r\n]*)?)\]",
    re.I,
)


TOKEN_RE = re.compile(r"\{p(\d+)\}")


def protect_tags(text, *, syntax=None):
    if syntax == "renpy":
        from .formats.renpy import protect_tags as protect_renpy
        return protect_renpy(text)
    if syntax == "godot":
        from .formats.godot import protect_tags as protect_godot
        return protect_godot(text)
    tokens = []

    def replace(match):
        tokens.append(match.group(0))
        return "{p" + str(len(tokens) - 1) + "}"

    return CONTROL_TAG_RE.sub(replace, text), tokens


def restore_tags(text, tokens):
    def replace(match):
        index = int(match.group(1))
        return tokens[index] if index < len(tokens) else match.group(0)
    return TOKEN_RE.sub(replace, text)


def validate_translation(task, translated, protected=False):
    """Return a failure reason. Format arguments may move; tags may not."""
    if not isinstance(translated, str) or not translated.strip():
        return "译文为空或不是字符串"
    if protected:
        expected = Counter(TOKEN_RE.findall(task["protected_text"]))
        if Counter(TOKEN_RE.findall(translated)) != expected:
            return "占位符身份或出现次数不匹配"
        translated = restore_tags(translated, task.get("codes", []))
    if task.get("structure", {}).get("text_syntax") == "renpy":
        from .formats.renpy import validate_codes
        return validate_codes(task["text"], translated)
    if task.get("structure", {}).get("text_syntax") == "godot":
        from .formats.godot import validate_codes
        return validate_codes(task["text"], translated)
    expected_tags = CONTROL_TAG_RE.findall(task["text"])
    actual_tags = CONTROL_TAG_RE.findall(translated)
    if Counter(expected_tags) != Counter(actual_tags):
        return "控制码/标签内容或出现次数不匹配"
    fixed_expected = [t for t in expected_tags if not t.startswith(("{", "%"))]
    fixed_actual = [t for t in actual_tags if not t.startswith(("{", "%"))]
    if fixed_expected != fixed_actual:
        return "控制码/标签顺序改变"
    return None


def is_translatable(text):
    if not isinstance(text, str) or not text.strip():
        return False
    text = text.strip()
    if re.fullmatch(r"[\d\s\-_.,/\\:;=><{}()\[\]\"'#+*!?~|]+", text):
        return False
    if re.match(r"^(?:https?://|[a-zA-Z0-9_]+[/\\])", text):
        return False
    if re.fullmatch(r"[\w.-]+\.(png|jpe?g|mp3|ogg|wav|prefab|asset|mat|shader)", text, re.I):
        return False
    return bool(HAS_TEXT.search(text))


def contains_term(text, term):
    if re.fullmatch(r"[A-Za-z0-9 _'-]+", term):
        return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text) is not None
    return term in text
