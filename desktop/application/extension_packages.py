"""本地扩展版本发布；候选和历史目录保留，不在当前进程热换代码。"""
import json
import ast
from pathlib import Path, PurePosixPath
import stat
from threading import RLock
from uuid import uuid4
import zipfile

from ..storage.atomic import write_text_atomic


_LOCK = RLock()
MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 10000


def managed_versions(root, *, issues=None):
    path = Path(root) / "extension-packages.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("active"), dict):
        raise ValueError("扩展版本目录格式错误；未切换任何版本。")
    result = {}
    base = (Path(root) / "extension-versions").resolve()
    for identifier, relative in data["active"].items():
        try:
            if not isinstance(identifier, str) or not isinstance(relative, str):
                raise ValueError("扩展版本条目错误。")
            target = (base / relative).resolve(strict=True)
            if not target.is_relative_to(base) or not target.is_dir():
                raise ValueError("扩展版本路径无效。")
            result[identifier] = target
        except (OSError, ValueError) as exc:
            if issues is None:
                raise
            # 保留该ID的占位，不悄悄切回另一份同名旧代码；其他扩展继续加载。
            result[identifier] = None
            issues.append(f"托管插件 {identifier} 未加载（{type(exc).__name__}）。")
    return result


def _safe_relative(value):
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"..", "."} or ":" in part for part in path.parts):
        raise ValueError("扩展包包含越界路径。")
    return Path(*path.parts)


def install_package(root, source):
    """先完整复制校验，再原子发布指针；失败不覆盖旧版本和个人配置。"""
    from ..plugin_catalog import PLUGINS, _descriptor
    root, source = Path(root).resolve(), Path(source).resolve(strict=True)
    with _LOCK:
        active = managed_versions(root)
        candidate = root / "extension-versions" / uuid4().hex
        if source.is_dir() and candidate.is_relative_to(source):
            raise ValueError("请选择单个插件目录，不能选择整个应用数据目录。")
        candidate.mkdir(parents=True)
        total = count = 0
        def copy_stream(relative, stream):
            nonlocal total, count
            count += 1
            if count > MAX_FILES:
                raise ValueError("扩展包文件超过10000项。")
            target = candidate / _safe_relative(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                while chunk := stream.read(65536):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("扩展包展开后超过256MiB。")
                    output.write(chunk)
        if source.is_dir():
            for index, path in enumerate(source.rglob("*")):
                if index >= MAX_FILES:
                    raise ValueError("扩展目录条目超过10000项。")
                if path.is_symlink() or path.is_junction():
                    raise ValueError("扩展包不接受符号链接或目录联接。")
                if any(part in {".git", "__pycache__", ".venv"} for part in path.relative_to(source).parts):
                    continue
                if path.is_file():
                    with path.open("rb") as stream:
                        copy_stream(path.relative_to(source).as_posix(), stream)
        elif source.suffix.lower() == ".zip":
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                if len(members) > MAX_FILES:
                    raise ValueError("扩展包条目超过10000项。")
                for item in members:
                    relative = _safe_relative(item.filename)
                    if stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                        raise ValueError("扩展包不接受链接或加密文件。")
                    if any(part in {".git", "__pycache__", ".venv"} for part in relative.parts):
                        continue
                    if not item.is_dir():
                        with archive.open(item) as stream:
                            copy_stream(item.filename, stream)
        else:
            raise ValueError("请选择插件目录或ZIP文件。")
        manifests = [candidate / "plugin.json"] if (candidate / "plugin.json").is_file() else list(candidate.glob("*/plugin.json"))
        if len(manifests) != 1:
            raise ValueError("扩展包须包含唯一的根目录plugin.json。候选目录已保留。")
        directory = manifests[0].parent
        if manifests[0].stat().st_size > 128 * 1024:
            raise ValueError("插件清单超过128KiB。")
        descriptor = _descriptor(directory, manifests[0].read_text(encoding="utf-8-sig"), root)
        for path in directory.rglob("*.py"):
            try:
                ast.parse(path.read_bytes(), filename=str(path.relative_to(directory)))
            except (SyntaxError, ValueError, UnicodeError) as exc:
                raise ValueError(f"插件源码 {path.relative_to(directory)} 静态解析失败；未切换版本。") from exc
        if descriptor.plugin_id in {item.plugin_id for item in PLUGINS}:
            raise ValueError("不能用外部包替换应用内置插件。")
        active[descriptor.plugin_id] = directory
        base = root / "extension-versions"
        write_text_atomic(root / "extension-packages.json", json.dumps({"version": 1,
            "active": {key: value.relative_to(base).as_posix() for key, value in active.items()}}, ensure_ascii=False, indent=2))
        return {"id": descriptor.plugin_id, "name": descriptor.label, "version": descriptor.version,
                "directory": str(directory), "note": "已安装，下次启动加载；旧版本和插件数据保留。"}
