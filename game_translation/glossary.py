"""Shared terminology and style; independent of model providers."""
import re
from collections import Counter
from pathlib import Path
from .storage import read_file, write_file
from .text import contains_term

def load_glossary(extract_dir):
    confirmed, pending = [], []
    for line in (read_file(Path(extract_dir) / "glossary.md") or "").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [p.strip().replace(r"\|", "|") for p in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if len(cells) < 2 or cells[0] in ("原文", "Source") or re.fullmatch(r"[-: ]+", cells[0]):
            continue
        source, target = cells[:2]
        status = cells[3] if len(cells) >= 4 else "confirmed"
        if status == "confirmed" and source and target:
            confirmed.append((source, target))
        elif status == "pending" and source:
            pending.append(source)
    return confirmed, pending


def load_style(extract_dir):
    return read_file(Path(extract_dir) / "style.md") or ""

KATAKANA_RE = re.compile(r"[ァ-ヶー]{2,}")


ENGLISH_NAME_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")


KANJI_TERM_RE = re.compile(r"[一-鿿]{2,5}")


STOPWORDS = set("これ それ あれ この その あの から ので けど The And You Are This That For With From Have They".split())


def detect_proper_nouns(tasks, actor_seeds=None):
    actor_seeds = set(actor_seeds or [])
    freq = Counter()

    for t in tasks:
        text = t.get("text", "")
        if not text:
            continue
        # 片假名
        for m in KATAKANA_RE.findall(text):
            if m not in STOPWORDS:
                freq[(m, "片假名(人名/外来语)")] += 1
        # 英文专有名词
        for m in ENGLISH_NAME_RE.findall(text):
            if m not in STOPWORDS:
                freq[(m, "英文名称/角色")] += 1
        # 汉字短词
        for m in KANJI_TERM_RE.findall(text):
            if m not in STOPWORDS:
                freq[(m, "高频汉字术语")] += 1

    candidates = []
    for seed in actor_seeds:
        count = sum(1 for t in tasks if seed in t.get("text", ""))
        if count:
            freq[(seed, "角色名(高置信度)")] = count
    added = set()
    for (word, kind), count in freq.items():
        if word in added:
            continue
        if count >= 3 or word in actor_seeds:
            actual_kind = "角色名(高置信度)" if word in actor_seeds else kind
            candidates.append({"word": word, "kind": actual_kind, "count": count})
            added.add(word)

    candidates.sort(key=lambda x: (x["kind"] != "角色名(高置信度)", -x["count"]))
    return candidates


def term_error(text, translation, terms):
    confirmed, pending = terms
    if any(contains_term(text, term) for term in pending):
        return "包含待确认术语"
    if any(contains_term(text, src) and dst not in translation for src, dst in confirmed):
        return "译文不符合已确认术语"
    return None


def initialize(work, tasks, *, target_locale=None):
    seeds = [t["text"] for t in tasks if t["type"] in ("name", "speaker")]
    candidates = detect_proper_nouns(tasks, seeds)
    defaults = {
        "glossary.md": "# 术语表\n\n| 原文 | 译文 | 说明 | 状态 |\n|---|---|---|---|\n\n状态使用 confirmed 或 pending。候选词不自动视为已确认。\n",
        "style.md": f"# 翻译风格\n\n目标语言：{target_locale or '简体中文'}。对白自然，界面简洁，保留人物语气。\n",
    }
    for name, content in defaults.items():
        if not (work / name).exists():
            write_file(work / name, content)
    # This generated list is separate from user decisions in glossary.md.
    lines = ["# 术语候选（重新提取时更新）", "", "确认后将映射填入 glossary.md。", "",
             "| 原文 | 次数 | 类型 |", "|---|---|---|"]
    for candidate in candidates[:100]:
        word = candidate["word"].replace("|", r"\|").replace("\n", " ")
        lines.append(f"| {word} | {candidate['count']} | {candidate['kind']} |")
    write_file(work / "proper_nouns.md", "\n".join(lines) + "\n")
