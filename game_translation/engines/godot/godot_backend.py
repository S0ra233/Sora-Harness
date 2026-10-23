"""Optional GDRETools boundary. Sessions, requests and logs are retained."""
import json
import os
from pathlib import Path
import tempfile

from ...storage import write_json
from ...process_control import run_engine_process


def executable():
    default = Path(__file__).resolve().parents[3] / "tools/godot/bin/gdre_tools.exe"
    path = Path(os.environ.get("GAME_TRANSLATION_GDRE") or default).resolve()
    if not path.is_file():
        raise FileNotFoundError("缺少 GDRETools；设置 GAME_TRANSLATION_GDRE，见 tools/godot/README.md")
    return path


def session(work, purpose):
    parent = Path(work) / "_godot" if work is not None else Path(tempfile.gettempdir()) / "game-translation-godot"
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=purpose + "-", dir=parent))


def run(arguments, directory, label):
    log = Path(directory) / (label + ".log")
    with log.open("xb") as output:
        result = run_engine_process([str(executable()), "--headless", *map(str, arguments)],
                                    directory, output, engine="godot")
    if result.returncode:
        raise ValueError(f"GDRETools 失败（{result.returncode}）；保留日志: {log}")


def unpack(source, directory):
    root = Path(directory) / "resources"
    run([f"--extract={Path(source).resolve()}", f"--output={root}"], directory, "extract")
    if not root.is_dir():
        raise ValueError(f"GDRETools 未生成解包目录；检查 {directory / 'extract.log'}")
    return root


def bridge(jobs, directory):
    """Run our data-only helper, never the game's main scene or scripts."""
    directory = Path(directory)
    request, response = directory / "request.json", directory / "response.json"
    home = directory / "helper"
    home.mkdir()
    (home / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    write_json(request, {"jobs": jobs})
    helper = Path(__file__).resolve().parents[3] / "tools/godot/gdre_bridge.gd"
    run(["--path", home, "--script", helper, "--", request, response], directory, "bridge")
    if not response.is_file():
        raise ValueError(f"GDRETools 未返回辅助接口结果；检查 {directory / 'bridge.log'}")
    result = json.loads(response.read_text(encoding="utf-8"))
    if result.get("error"):
        raise ValueError(f"GDRETools 辅助接口失败: {result['error']}；日志: {directory / 'bridge.log'}")
    return result["results"]


def project_config(source, directory):
    destination = Path(directory) / "config"
    run([f"--bin-to-txt={Path(source).resolve()}", f"--output={destination}"], directory, "config")
    path = destination / "project.godot"
    if not path.is_file():
        raise ValueError("GDRETools 没有生成可读项目配置")
    return path


def patch(source, patches, directory, *, embedded=False):
    """Patch full copies in bounded CLI batches, preserving untouched members."""
    source, directory = Path(source).resolve(), Path(directory)
    batches, batch, size = [], [], 0
    for original, replacement in patches.items():
        argument = f"--patch-file={Path(replacement).resolve()}=res://{original}"
        if batch and size + len(argument) > 20000:
            batches.append(batch)
            batch, size = [], 0
        batch.append(argument)
        size += len(argument) + 3
    if batch:
        batches.append(batch)
    current = source
    for index, arguments in enumerate(batches):
        target = directory / (f"patched-{index}" + (source.suffix if embedded else ".pck"))
        options = [f"--pck-patch={current}", f"--output={target}", *arguments]
        if embedded:
            options.append(f"--embed={current}")
        run(options, directory, f"patch-{index}")
        if not target.is_file():
            raise ValueError(f"未生成 Godot 资源包副本: {target}")
        current = target
    return current
