"""Optional WolfTL/UberWolf CLI boundary; all generated files are retained."""
import os
from pathlib import Path
import tempfile
from ...process_control import run_engine_process


TOOLS = {
    "unpack": ("GAME_TRANSLATION_UBERWOLF", "UberWolfCli.exe"),
    "text": ("GAME_TRANSLATION_WOLFTL", "WolfTL.exe"),
}


def executable(kind):
    variable, filename = TOOLS[kind]
    default = Path(__file__).resolve().parents[3] / "tools" / "wolf" / "bin" / filename
    path = Path(os.environ.get(variable) or default).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"缺少可选工具 {filename}；请设置 {variable}，见 tools/wolf/README.md")
    return path


def session(work, purpose):
    parent = Path(work) / "_wolf" if work is not None else Path(tempfile.gettempdir()) / "game-translation-wolf"
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=purpose + "-", dir=parent))


def run(kind, arguments, directory, label):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / (label + ".log")
    # No shell, in-place mode, auto-update, or game executable invocation.
    with log.open("xb") as output:
        result = run_engine_process([str(executable(kind)), *map(str, arguments)],
                                    directory, output, engine="wolf")
    if result.returncode:
        raise ValueError(f"{TOOLS[kind][1]} 失败（{result.returncode}）；保留日志: {log}")


def export(data, directory):
    """Decode actual binary resources, never reuse a previous JSON readback."""
    directory = Path(directory).resolve()
    run("text", [Path(data).resolve(), directory, "--create"], directory, "export")
    dump = directory / "dump"
    if not (dump / "Game.json").is_file():
        raise ValueError(f"WolfTL 没有生成 Game.json；检查 {directory / 'export.log'}")
    return dump


def patch(data, directory):
    run("text", [Path(data).resolve(), Path(directory).resolve(), "--patch"], directory, "patch")
    return Path(directory) / "patched" / "data"
