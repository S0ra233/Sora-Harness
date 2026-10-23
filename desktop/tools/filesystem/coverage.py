"""只表达已观察的覆盖范围；未知统计保留None，过滤和早停不能冒充全树扫描。"""
from dataclasses import dataclass, field
from time import monotonic


@dataclass
class Coverage:
    backend: str
    started: float = field(default_factory=monotonic)
    scanned_files: int | None = 0
    read_bytes: int | None = 0
    skipped: dict[str, int | None] = field(default_factory=dict)
    limit_reason: str | None = None
    output_clipped: bool = False
    clipped_chars: int | None = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = (self.skipped.get(reason) or 0) + 1

    def result(self, *, complete: bool = True) -> dict:
        return {"backend": self.backend, "scan_complete": complete and self.limit_reason is None,
                "scanned_files": self.scanned_files, "read_bytes": self.read_bytes,
                "skipped": dict(self.skipped), "hit_cap_reached": self.limit_reason == "match_limit",
                "output_clipped": self.output_clipped, "clipped_chars": self.clipped_chars,
                "limit_reason": self.limit_reason, "elapsed_ms": round((monotonic() - self.started) * 1000)}
