"""Discover Unity layouts and resource candidates without loading game code."""
import os
import re
import struct
from pathlib import Path


def unity_header_kind(header, size):
    for signature in (b"UnityFS\0", b"UnityRaw\0", b"UnityWeb\0"):
        if header.startswith(signature):
            return "unity_bundle", "Unity bundle 文件头；内部格式尚未检查"
    if len(header) >= 48:
        metadata, stored_size, version, offset = struct.unpack_from(">4I", header)
        if 9 <= version <= 22 and header[16] in (0, 1) and header[17:20] == bytes(3):
            header_size = 20
            if version == 22:
                metadata, stored_size, offset = struct.unpack_from(">Iqq", header, 20)
                header_size = 48
            if stored_size == size and 0 < metadata <= offset - header_size and offset <= size:
                return "unity_serialized", f"Unity 序列化头 v{version}，长度和偏移一致"
    return None, None


def _header(path):
    with path.open("rb") as handle:
        return handle.read(128), path.stat().st_size


def _data_evidence(data):
    evidence = []
    for path in sorted(data.iterdir()):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name not in {"globalgamemanagers", "data.unity3d", "mainData"} and path.suffix.lower() != ".assets":
            continue
        kind, _ = unity_header_kind(*_header(path))
        if kind:
            evidence.append(path.name)
    return evidence


def detect_unity(game_dir):
    game = Path(game_dir)
    candidates = [game, *(p for p in sorted(game.iterdir())
                         if p.is_dir() and not p.is_symlink() and p.name.lower().endswith("_data"))]
    matches = [(p, evidence) for p in candidates if (evidence := _data_evidence(p))]
    basis = "header_structure"
    if not matches:
        # Keep the existing plain-table workflow usable without UnityPy or binary
        # resources. This is a layout hint, explicitly weaker than a parsed header.
        matches = [(p, ["StreamingAssets"]) for p in candidates
                   if p.name.lower().endswith("_data") and (p / "StreamingAssets").is_dir()
                   and any(f.is_file() and f.suffix.lower() in {".json", ".csv", ".tsv", ".txt", ".xml", ".properties"}
                           for f in (p / "StreamingAssets").rglob("*"))]
        basis = "directory_layout"
    if len(matches) > 1:
        raise ValueError("检测到多个 Unity 数据目录，请指定实际游戏子目录")
    if not matches:
        return None
    data, evidence = matches[0]
    version = "Unknown"
    for name in evidence:
        if not (data / name).is_file():
            continue
        with (data / name).open("rb") as handle:
            header = handle.read(2048)
        match = re.search(rb"(?:[2-9]\d{3}|[3-5])\.\d+\.\d+[abfp]\d+", header)
        if match:
            version = match.group().decode("ascii")
            break
    metadata = data / "il2cpp_data/Metadata/global-metadata.dat"
    binary = data.parent / "GameAssembly.dll"
    backend = "IL2CPP" if metadata.is_file() or binary.is_file() else (
        "Mono" if (data / "Managed").is_dir() else "Unknown")
    return {"engine": f"Unity ({backend})", "family": "Unity", "backend": backend,
            "unity_version": version, "data_dir": str(data),
            "features": ["StreamingAssets"] if (data / "StreamingAssets").is_dir() else [],
            "evidence": [(data / name).relative_to(game).as_posix() for name in evidence],
            "detection_basis": basis,
            "private_archives": []}


def scan_resources(root, data):
    """Check small headers, including renamed bundles. No arbitrary scan cutoff."""
    root, data = Path(root), Path(data)
    assets, texts, issues = [], [], []
    checked = 0
    streaming = {data / "StreamingAssets", root / "StreamingAssets"}

    def onerror(exc):
        issues.append({"file": str(exc.filename), "state": "scan_failed", "reason": str(exc)})

    for folder, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            if path.is_symlink():
                continue
            checked += 1
            relative = path.relative_to(root).as_posix()
            try:
                kind, reason = unity_header_kind(*_header(path))
                suffix = path.suffix.lower()
                named_asset = (suffix in {".assets", ".bundle", ".unity3d"}
                               or name in {"globalgamemanagers", "mainData"}
                               or re.fullmatch(r"level\d+", name))
                if kind or named_asset:
                    assets.append(path)
                elif any(s in path.parents for s in streaming) and suffix in {".json", ".csv", ".tsv", ".txt", ".xml", ".properties"}:
                    texts.append(path)
                elif suffix in {".pak", ".arc", ".dat", ".bin", ".nani", ".xml"} and "Managed" not in path.parts:
                    issues.append({"file": relative, "state": "unrecognized",
                                   "reason": "未进入已支持的资源路线；不据此判断加密"})
            except OSError as exc:
                issues.append({"file": relative, "state": "scan_failed", "reason": str(exc)})
    return {"assets": sorted(assets), "texts": sorted(texts), "issues": issues,
            "files_checked": checked, "truncated": False}
