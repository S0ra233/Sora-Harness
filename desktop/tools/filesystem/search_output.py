"""两种搜索后端共用的命中片段和反馈字符预算。"""
from typing import Any
from dataclasses import dataclass, field

from .limits import MAX_OUTPUT_CHARS


OUTPUT_MODES = ("content", "files_with_matches", "count", "summary")


@dataclass
class SearchSummary:
    """共享紧凑结果计数，输出条目有界；不把显示上限当成全范围计数。"""
    mode: str
    limit: int
    matched_lines: int = 0
    files_with_hits: int = 0
    entries: list = field(default_factory=list)
    characters: int = 0
    result_truncated: bool = False

    def add_file(self, path: str, matched_lines: int) -> None:
        if not matched_lines:
            return
        self.matched_lines += matched_lines
        self.files_with_hits += 1
        if self.mode == "summary":
            return
        if len(self.entries) >= self.limit or self.characters + len(path) > MAX_OUTPUT_CHARS:
            self.result_truncated = True
            return
        self.entries.append({"path": path, "matched_lines": matched_lines} if self.mode == "count" else path)
        self.characters += len(path)

    @property
    def stop_at_file_cap(self) -> bool:
        return self.mode == "files_with_matches" and (len(self.entries) >= self.limit or self.result_truncated)

    def result(self) -> dict:
        value = {"output_mode": self.mode, "files_with_hits": self.files_with_hits,
                 "result_truncated": self.result_truncated}
        if self.mode != "files_with_matches":
            value.update(matched_lines=self.matched_lines, count_basis="matched_lines",
                         totals_scope="observed_valid_results; complete only when coverage.scan_complete is true")
        if self.mode == "count":
            value["counts"] = self.entries
        elif self.mode == "files_with_matches":
            value["paths"] = self.entries
        return value


def excerpt(line: str, start: int = 0) -> dict[str, Any]:
    """保留定位字段；长行的截断与整次结果的截断分别表达。"""
    start = min(max(0, start), max(0, len(line) - 300))
    snippet = line[start:start + 300]
    return {
        "text": snippet, "snippet_start_column": start + 1,
        "line_truncated": start > 0 or start + len(snippet) < len(line),
        "clipped_chars": max(0, len(line) - len(snippet)),
        "original_chars": len(line),
    }


def search_stop_reason(limit_reason: str | None, *, io_errors: bool = False) -> str:
    """终止原因和覆盖范围分开；具体预算仍由原limit_reason说明。"""
    if limit_reason == "match_limit":
        return "cap_reached"
    if limit_reason is not None and limit_reason != "search_io_errors":
        return "budget_reached"
    return "io_errors" if io_errors or limit_reason == "search_io_errors" else "completed"


def excerpt_size(match: dict[str, Any]) -> int:
    """重叠窗口也计入预算，不把附近行无限附加到命中结果。"""
    return len(match["text"]) + sum(
        len(item["text"]) for key in ("context_before", "context_after") for item in match.get(key, [])
    )
