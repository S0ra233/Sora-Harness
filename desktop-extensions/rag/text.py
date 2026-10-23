"""只转换索引用词；返回给模型及页面的片段保留原文。"""
from collections import Counter
from dataclasses import dataclass
import bisect
import re
import unicodedata


CHUNK_VERSION = "paragraph-window-1:1400:120"
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+")
_WORD = re.compile(r"[^\W_]+(?:[_'-][^\W_]+)*", re.UNICODE)


def terms(text: str, *, query: bool = False) -> Counter:
    """中文相邻双字兼容短词；单字索引只在单字查询时使用。"""
    text = unicodedata.normalize("NFKC", text).casefold()
    result = Counter()
    for match in _CJK.finditer(text):
        word = match.group()
        if not query or len(word) == 1:
            result.update("c:" + char for char in word)
        result.update("b:" + word[index:index + 2] for index in range(len(word) - 1))
    for word in _WORD.findall(_CJK.sub(" ", text)):
        result["w:" + word] += 1
    return result


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    start: int
    end: int
    start_line: int
    end_line: int
    text: str


def chunks(text: str):
    """尽量在段落、行或句末切开；字符偏移始终指向解码后的原文。"""
    newlines = [index for index, char in enumerate(text) if char == "\n"]
    start, ordinal = 0, 0
    while start < len(text):
        end = min(len(text), start + 1400)
        if end < len(text):
            lower = start + 700
            for separator in ("\n\n", "\n", "。", ". ", "！", "？", " "):
                boundary = text.rfind(separator, lower, end)
                if boundary >= lower:
                    end = boundary + len(separator)
                    break
        value = text[start:end]
        if value.strip():
            yield Chunk(ordinal, start, end, bisect.bisect_left(newlines, start) + 1,
                        bisect.bisect_left(newlines, end - 1) + 1, value)
            ordinal += 1
        if end == len(text):
            break
        start = max(start + 1, end - 120)
