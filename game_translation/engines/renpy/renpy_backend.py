"""Optional unrpa/unrpyc boundaries; tools operate only on work resources."""
import io
import json
import os
from pathlib import Path
import pickle
import shutil
import zlib

from ...storage import safe_join, write_json
from process_support.launcher import worker_command


class _IndexReader(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) == ("_codecs", "encode"):
            return _index_bytes
        # An RPA index consists of dictionaries, tuples and primitive values.
        raise ValueError("RPA 索引包含非数据对象；需要专用解包工具")


def _index_bytes(value, encoding):
    # Python 3 protocol-2 indices encode bytes using this primitive reducer.
    if encoding not in ("latin1", "latin-1", "utf-8", "ascii"):
        raise ValueError("RPA 索引使用未支持的字节编码")
    return value.encode(encoding)


def unpack_scripts(archive, directory):
    """Use unrpa's format handlers; publish only compiled script members."""
    try:
        from unrpa import UnRPA
    except ImportError as exc:
        raise ImportError("读取 RPA 需要可选依赖 unrpa，见 tools/renpy/README.md") from exc
    tool = UnRPA(str(archive), verbosity=-1)
    version = tool.detect_version()
    extracted = []
    with Path(archive).open("rb") as source:
        offset, key = version.find_offset_and_key(source)
        source.seek(offset)
        index = _IndexReader(io.BytesIO(zlib.decompress(source.read())), encoding="bytes").load()
        index = UnRPA.deobfuscate_index(key, index) if key is not None else UnRPA.normalise_index(index)
        for number, (name, parts) in enumerate(index.items()):
            name = name.decode("utf-8") if isinstance(name, bytes) else name
            target = safe_join(directory, name)
            if target.suffix.lower() != ".rpyc":
                continue
            # Multi-part/private entries are not silently reduced to one fragment.
            if len(parts) != 1:
                raise ValueError(f"RPA 脚本使用非标准多段索引: {name}")
            view = tool.extract_file(name, parts, number, len(index), source)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                version.postprocess(view, output)
            extracted.append(name.replace("\\", "/"))
    return extracted


def read_compiled(files, directory):
    repository = Path(os.environ.get("GAME_TRANSLATION_UNRPYC") or
                      Path(__file__).resolve().parents[3] / "tools/renpy/vendor/unrpyc").resolve()
    if repository.is_file():
        repository = repository.parent
    if not (repository / "decompiler/renpycompat.py").is_file():
        raise FileNotFoundError("缺少 unrpyc 仓库；设置 GAME_TRANSLATION_UNRPYC，见 tools/renpy/README.md")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    request, output = directory / "request.json", directory / "compiled.json"
    write_json(request, {"repository": str(repository), "files": files})
    helper = Path(__file__).resolve().parents[3] / "tools/renpy/read_rpyc.py"
    _run(worker_command("renpy-read", ["-B", str(helper)], str(request), str(output)), directory / "read.log")
    return json.loads(output.read_text(encoding="utf-8"))


def generate_templates(game, session, language):
    """Explicit SDK route: initialization may run game code, only in a copy."""
    launcher = os.environ.get("GAME_TRANSLATION_RENPY_SDK")
    if not launcher:
        return None
    launcher = Path(launcher).resolve()
    if not launcher.is_file():
        raise FileNotFoundError("GAME_TRANSLATION_RENPY_SDK 必须指向匹配版本 SDK 的启动程序")
    copied = Path(session) / "generator_game"
    shutil.copytree(game, copied)
    _run([str(launcher), str(copied), "translate", language], Path(session) / "generate.log")
    return copied


def _run(command, log):
    from ...process_control import run_engine_process
    with Path(log).open("xb") as output:
        result = run_engine_process(command, Path(log).parent, output, engine="renpy")
    if result.returncode:
        raise ValueError(f"Ren'Py 后端失败（{result.returncode}）；日志保留在 {log}")
