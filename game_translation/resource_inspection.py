"""Read-only resource evidence for an Agent to choose a handling route.

Header matches describe containers, never promise text extraction or decryption.
No external tools, game code, or model providers are executed here.
"""
import json
import os
from pathlib import Path

from .engines import detect
from .engines.unity.unity_scan import unity_header_kind


def _header_kind(header, size):
    kind, reason = unity_header_kind(header, size)
    if kind:
        return kind, reason
    if header.startswith(b"RGSSAD\0") and len(header) >= 8:
        return "rgss_archive", f"RGSSAD 文件头，版本 {header[7]}；尚未试解包"
    if header.startswith(b"GDPC") and len(header) >= 20:
        import struct
        version, major, minor, _ = struct.unpack("<4I", header[4:20])
        if major in {2, 3, 4} and version <= 10:
            return "godot_pack", f"GDPC 头结构，Godot {major}.{minor}；尚未尝试解包"
    if header.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "zip_container", "ZIP 文件头；不是游戏引擎判定"
    return None, None


MAX_FILES = 4000
MAX_EVIDENCE = 300
CANDIDATE_SUFFIXES = {".assets", ".bundle", ".unity3d", ".dat", ".bin", ".pak",
                      ".arc", ".xp3", ".rgssad", ".rgss2a", ".rgss3a", ".asar",
                      ".wolf", ".evb", ".vfs", ".exe", ".pck", ".scn", ".res", ".translation"}


def inspect_resources(game_dir, work_dir=None):
    """Collect bounded resource evidence and an optional saved extraction record."""
    game = Path(game_dir).resolve()
    info = detect(game)
    evidence, errors, scanned, truncated = _scan_evidence(game)
    attempt = _previous_extraction(game, work_dir) if work_dir is not None else None
    routes = _handling_routes(info, evidence)
    return {"engine": info, "evidence": evidence, "routes": routes,
            "previous_extraction": attempt, "errors": errors,
            "scan": {"files_checked": scanned, "header_bytes_per_file": 128,
                     "max_files": MAX_FILES, "max_evidence": MAX_EVIDENCE, "truncated": truncated},
            "text_coverage_verified": False,
            "handoff": ["游戏版本、未翻译原文及出现位置", "原资源相对路径",
                        "工具名称与版本、使用步骤", "导出文件及对象类型、名称、PathID（如适用）"],
            "note": "只收集证据与候选路线；未解包、解密、分析程序集或验证第三方工具。"}


def _scan_evidence(game):
    evidence, errors = [], []
    scanned, truncated = 0, False
    def walk_error(exc):
        errors.append({"path": str(exc.filename), "reason": str(exc)})
    for folder, directories, files in os.walk(game, followlinks=False, onerror=walk_error):
        directories[:] = sorted(d for d in directories
                                if not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            if path.is_symlink():
                continue
            if scanned >= MAX_FILES or len(evidence) >= MAX_EVIDENCE:
                truncated = True
                break
            scanned += 1
            relative = path.relative_to(game).as_posix()
            try:
                item = _inspect_file(path, relative)
                if item is not None:
                    evidence.append(item)
            except OSError as exc:
                errors.append({"file": relative, "reason": str(exc)})
        if truncated:
            break

    return evidence, errors, scanned, truncated


def _inspect_file(path, relative):
    size = path.stat().st_size
    with path.open("rb") as handle:
        header = handle.read(128)
    kind, reason = _header_kind(header, size)
    if kind:
        return {"file": relative, "size": size, "kind": kind,
                "basis": reason, "strength": "header_structure"}
    elif path.suffix.lower() == ".asar":
        from .formats.asar import Archive
        try:
            archive = Archive(path)
        except ValueError as exc:
            return {"file": relative, "size": size, "kind": "unrecognized_resource",
                    "basis": f"ASAR 索引未通过解析: {exc}", "strength": "candidate"}
        scripts = [name for name in archive.nodes if name.startswith("data/scenario/") and name.endswith(".ks")]
        return {"file": relative, "size": size, "kind": "asar_container",
                "basis": "ASAR Pickle/JSON 索引及文件偏移可解析；未执行内部代码",
                "strength": "header_structure", "members": len(archive.nodes),
                "tyrano_scripts": len(scripts), "samples": scripts[:3]}
    elif path.name == "Assembly-CSharp.dll" and header.startswith(b"MZ"):
        return {"file": relative, "size": size, "kind": "game_assembly",
                "basis": "程序集名称与 PE 起始标记；未检查类型或加载逻辑",
                "strength": "candidate"}
    elif path.suffix.lower() in CANDIDATE_SUFFIXES or path.name.startswith("level"):
        return {"file": relative, "size": size, "kind": "unrecognized_resource",
                "basis": "仅列作检查候选；当前头结构规则没有匹配",
                "header_hex": header[:32].hex(), "strength": "candidate"}


def _previous_extraction(game, work_dir):
    work = Path(work_dir).resolve()
    record = work / "prepare_diagnostics.json"
    if not record.exists():
        record = work / "project.json"
    if not record.exists():
        raise ValueError("工作目录中没有提取诊断或项目记录")
    saved = json.loads(record.read_text(encoding="utf-8"))
    if Path(saved.get("game_dir", "")).resolve() != game:
        raise ValueError("提取记录属于另一个游戏")
    return {"record": str(record), "entries": saved.get("entries"),
            "warnings": saved.get("warnings", []),
            "note": "已有记录，未重新提取，也未检查源文件是否在记录后变化"}


def _handling_routes(info, evidence):
    kinds = {e["kind"] for e in evidence}
    routes = []
    if info["family"] != "unknown":
        routes.append({"route": "existing_adapter", "basis": info["evidence"],
                       "next": "可尝试现有 prepare；引擎匹配不代表所有资源可读。已有失败记录时先分析失败原因。"})
    if kinds & {"unity_bundle", "unity_serialized"}:
        routes.append({"route": "unity_object_inspection",
                       "basis": "发现 Unity 容器头结构；未确认内部对象的文本布局",
                       "next": "结合已有提取警告，定位对象文件、类型、名称及 PathID；必要时人工导出对象与类型信息。"})
    if "game_assembly" in kinds:
        routes.append({"route": "loader_inspection",
                       "basis": "发现游戏程序集候选；尚未分析加载代码",
                       "next": "若资源结构不明，由 Agent 指导人工查找对应资源的读取类，返回相关字段结构或加载方法。"})
    if "rgss_archive" in kinds:
        routes.append({"route": "rgss_archive_inspection",
                       "basis": "RGSSAD 头标记",
                       "next": "结合版本与 RPG Maker 检测结果判断现有解包器是否适用；未尝试版本不宣称支持。"})
    if "zip_container" in kinds:
        routes.append({"route": "archive_listing",
                       "basis": "ZIP 头标记",
                       "next": "先查看归档目录及内部文件结构，再判断文本处理方式。"})
    if "asar_container" in kinds:
        routes.append({"route": "asar_resources", "basis": "标准 ASAR 索引",
                       "next": "Tyrano 目录可使用内置 prepare/build；其他内部格式由 Agent 判断后接回已有引擎或临时适配。"})
    if "godot_pack" in kinds or info["family"] == "godot":
        routes.append({"route": "godot_resources", "basis": info.get("evidence", []) or "GDPC 头结构",
                       "next": "Godot 可选后端处理标准 PCK/内嵌包；先候选审查，再写回原加载资源。脚本、对话插件及加密变体由 Agent 分析。"})
    if "unrecognized_resource" in kinds or not routes:
        routes.append({"route": "manual_format_identification",
                       "basis": "有资源未匹配已知头结构，或没有足够线索",
                       "next": "Agent 综合数据、目录和加载线索查找对应格式或专用工具；不能从解析失败推断加密。"})
    return routes
