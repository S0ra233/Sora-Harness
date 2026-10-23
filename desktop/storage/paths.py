"""集中计算既有数据路径，不创建目录、不搬迁用户文件。"""
from dataclasses import dataclass
import os
from pathlib import Path
import sys


@dataclass(frozen=True)
class LocalPaths:
    root: Path

    @classmethod
    def default(cls, directory: Path | str | None = None) -> "LocalPaths":
        directory = directory if directory is not None else os.environ.get("DESKTOP_HARNESS_DATA_DIR")
        if directory is not None:
            return cls(Path(directory).expanduser().resolve())
        if getattr(sys, "frozen", False):
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            return cls((base / "DesktopHarness").resolve())
        return cls(Path(__file__).resolve().parents[1] / "local")

    @property
    def settings(self) -> Path:
        return self.root / "settings.json"

    @property
    def extensions(self) -> Path:
        return self.root / "extensions"

    @property
    def mcp(self) -> Path:
        return self.root / "mcp.json"

    @property
    def catalog(self) -> Path:
        return self.root / "catalog.json"

    @property
    def environment(self) -> Path:
        return self.root / "environment.json"

    @property
    def conversations(self) -> Path:
        return self.root / "conversations"

    @property
    def drafts(self) -> Path:
        return self.root / "drafts"

    @property
    def turn_options(self) -> Path:
        return self.root / "turn-options"

    @property
    def prompt_presets(self) -> Path:
        return self.root / "prompt-presets.json"

    @property
    def skills(self) -> Path:
        return self.root / "skills.json"

    @property
    def general(self) -> Path:
        return self.root / "general.json"

    @property
    def ui_preferences(self) -> Path:
        return self.root / "ui-state.json"

    @property
    def attachments(self) -> Path:
        return self.root / "attachments"

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def agent_runs(self) -> Path:
        return self.root / "agent-runs"

    @property
    def tool_results(self) -> Path:
        return self.root / "tool-results"

    @property
    def request_records(self) -> Path:
        return self.root / "request-records"
