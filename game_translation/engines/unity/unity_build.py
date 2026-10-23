"""Preflight and catalog updates for explicitly mapped local Addressables bundles.

Standard direct-file providers only; remote/cache routes remain unsupported.
"""
import json
import os
import struct
import base64
import hashlib
import subprocess
import zlib
from pathlib import Path

from ...storage import safe_join, file_hash, write_file
from .unity_scan import unity_header_kind


def check_build(root, info, changed_files):
    root = Path(root)
    bundles, catalogs, issues = [], [], []
    for name in sorted(set(changed_files)):
        path = safe_join(root, name)
        try:
            with path.open("rb") as handle:
                header = handle.read(128)
            kind, _ = unity_header_kind(header, path.stat().st_size)
            if kind == "unity_bundle":
                bundles.append(name)
        except OSError as exc:
            issues.append({"file": name, "reason": str(exc)})
    result = {"status": "no_bundle_changes", "can_build": not issues,
              "changed_bundles": bundles, "catalogs": catalogs, "issues": issues,
              "runtime_verified": False, "catalogs_modified": False,
              "note": "仅检查本次修改是否涉及 Bundle 与 Addressables；不证明游戏运行或覆盖完整"}
    if issues:
        result["status"] = "source_read_failed"
        return result
    if not bundles:
        return result

    data_relative = Path(info["data_dir"]).relative_to(Path(info["game_dir"]))
    data = root / data_relative
    aa_roots = {data / "StreamingAssets/aa", root / "StreamingAssets/aa"}

    catalogs.extend(_scan_catalogs(root, aa_roots, issues))

    aa_changed = [name for name in bundles if any(p in (root / name).parents for p in aa_roots)]
    if catalogs or aa_changed or issues:
        result.update(status="needs_catalog_support", can_build=False,
                      note="待修改 Bundle 存在 Addressables 线索或检查未完成；尚未可靠解析目录到 Bundle 的关联。"
                           "保留 catalog 与哈希原样，需适配对应目录格式后再构建；没有关闭 CRC 校验。")
        if not issues:
            try:
                result["catalog_plan"] = plan_catalogs(root, info, bundles, catalogs)
                result.update(status="catalog_update_ready", can_build=True,
                              note="已定位本地 Bundle，校验原 CRC 并验证 catalog 往返；写回后更新 CRC/大小及目录缓存标识。")
            except (ValueError, OSError, RuntimeError) as exc:
                issues.append({"reason": str(exc)})
    else:
        result.update(status="no_addressables_evidence",
                      note="未发现已知位置或 catalog 名称下的 Addressables 线索；"
                           "自定义或改名目录、运行时加载及外部校验仍未验证。")
    return result


def _scan_catalogs(root, aa_roots, issues):
    catalogs = []
    def onerror(exc):
        issues.append({"file": str(exc.filename), "reason": str(exc)})

    for folder, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            if path.is_symlink() or not name.lower().startswith("catalog"):
                continue
            if path.suffix.lower() not in {".json", ".bin", ".hash"}:
                continue
            relative = path.relative_to(root).as_posix()
            try:
                with path.open("rb") as handle:
                    header = handle.read(16)
                in_aa = any(p in path.parents for p in aa_roots)
                if path.suffix.lower() == ".hash":
                    if in_aa:
                        catalogs.append({"file": relative, "format": "hash_record",
                                         "basis": "Addressables 目录中的 catalog 哈希记录"})
                elif len(header) >= 8 and struct.unpack_from("<I", header)[0] == 0x0DE38942:
                    catalogs.append({"file": relative, "format": "binary",
                                     "version": struct.unpack_from("<I", header, 4)[0],
                                     "basis": "Addressables 二进制 catalog 魔数"})
                elif path.suffix.lower() == ".json":
                    document = json.loads(path.read_text(encoding="utf-8-sig"))
                    if isinstance(document, dict) and isinstance(document.get("m_InternalIds"), list):
                        catalogs.append({"file": relative, "format": "json",
                                         "basis": "catalog JSON 包含 m_InternalIds"})
                    elif in_aa:
                        catalogs.append({"file": relative, "format": "unrecognized",
                                         "basis": "Addressables 目录中的未支持 catalog 结构"})
                elif in_aa:
                    catalogs.append({"file": relative, "format": "unrecognized",
                                     "basis": "Addressables 目录中的 catalog 候选，未确认格式"})
            except (OSError, ValueError) as exc:
                issues.append({"file": relative, "reason": str(exc)})

    return catalogs


def catalog_call(path, operation="inspect", changes=()):
    helper = Path(os.environ.get("GAME_TRANSLATION_ADDRESSABLES_HELPER", str(
        Path(__file__).resolve().parents[3] / "tools/unity_addressables/bin/UnityAddressables.dll"))).resolve()
    if not helper.is_file():
        raise ValueError("Addressables 需要可选辅助程序；参见 tools/unity_addressables/README.md")
    request = {"path": str(path), "operation": operation, "changes": list(changes)}
    try:
        process = subprocess.run(["dotnet", str(helper)], input=json.dumps(request),
            capture_output=True, text=True, encoding="utf-8", timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Addressables 目录处理超时，未发布游戏副本") from exc
    if process.returncode:
        raise ValueError("Addressables 目录处理失败: " + process.stderr[:3000])
    return json.loads(process.stdout)


def bundle_crc(path):
    """CRC of decompressed UnityFS data blocks, including inter-file padding."""
    from UnityPy.files.BundleFile import BundleFile
    from UnityPy.streams import EndianBinaryReader

    class BlocksOnly(BundleFile):
        def read_files(self, reader, directory):
            self.content_crc = zlib.crc32(reader.bytes)

    raw = Path(path).read_bytes()
    if not raw.startswith(b"UnityFS\0"):
        raise ValueError("Addressables CRC 当前只支持 UnityFS")
    return BlocksOnly(EndianBinaryReader(raw), None).content_crc


def _local_bundle(root, info, internal_id):
    # Resolve explicit Unity runtime variables, never guess by basename.
    data = Path(info["data_dir"]).relative_to(Path(info["game_dir"]))
    text = internal_id.replace("\\", "/")
    prefixes = {
        "{UnityEngine.AddressableAssets.Addressables.RuntimePath}": data / "StreamingAssets/aa",
        "{UnityEngine.Application.streamingAssetsPath}": data / "StreamingAssets",
    }
    for prefix, relative in prefixes.items():
        if text.startswith(prefix + "/"):
            return safe_join(root, (relative / text[len(prefix) + 1:]).as_posix()).relative_to(root).as_posix()
    return None


def plan_catalogs(root, info, bundles, catalogs):
    plans, covered, crc_cache = [], set(), {}
    if any(c["format"] == "unrecognized" for c in catalogs):
        raise ValueError("存在未识别的 catalog，需要先确认加载关系")
    for cat in catalogs:
        if cat["format"] == "hash_record":
            continue
        path = safe_join(root, cat["file"])
        document = catalog_call(path)
        matches = []
        for row in document["bundles"]:
            relative = _local_bundle(root, info, row["internal_id"])
            if relative not in bundles:
                continue
            if (row["provider"] != "UnityEngine.ResourceManagement.ResourceProviders.AssetBundleProvider"
                    or row["use_web_request"]):
                raise ValueError("选中 Bundle 使用自定义 Provider 或 WebRequest 缓存路线，尚未适配")
            if relative not in crc_cache:
                crc_cache[relative] = bundle_crc(safe_join(root, relative))
            if row["crc"] and crc_cache[relative] != row["crc"]:
                raise ValueError(f"原 Bundle CRC 与 catalog 不匹配: {relative}")
            matches.append({"file": relative, "internal_id": row["internal_id"], "crc": row["crc"]})
            covered.add(relative)
        if matches:
            # Validate this exact catalog's writer before copying a large game.
            catalog_call(path, "rewrite")
            hash_path = path.with_suffix(".hash")
            plans.append({"file": cat["file"], "source_hash": file_hash(path), "bundles": matches,
                          "hash_file": hash_path.relative_to(root).as_posix() if hash_path.is_file() else None,
                          "hash_source": file_hash(hash_path) if hash_path.is_file() else None})
    missing = set(bundles) - covered
    if missing:
        raise ValueError("无法确认 Bundle 的本地目录映射: " + ", ".join(sorted(missing)))
    return plans


def finalize_build(output, info, checks):
    """Update catalogs after ALL text and font writes, before output is published."""
    updates, crc_cache = [], {}
    for plan in checks.get("catalog_plan", []):
        path = safe_join(output, plan["file"])
        if file_hash(path) != plan["source_hash"]:
            raise ValueError("构建期间 catalog 被改变，停止收尾")
        changes = []
        for row in plan["bundles"]:
            bundle = safe_join(output, row["file"])
            if row["file"] not in crc_cache:
                crc_cache[row["file"]] = bundle_crc(bundle)
            changes.append({"internal_id": row["internal_id"],
                            "crc": crc_cache[row["file"]] if row["crc"] else 0, "size": bundle.stat().st_size})
        result = catalog_call(path, "rewrite", changes)
        raw = base64.b64decode(result["data"], validate=True)
        updates.append((path, raw))
        if plan["hash_file"]:
            hash_path = safe_join(output, plan["hash_file"])
            if file_hash(hash_path) != plan["hash_source"]:
                raise ValueError("构建期间 catalog hash 被改变")
            # The catalog .hash is a cache version token. This is our version of
            # the modified catalog, NOT a recreation of Unity's SBP build hash.
            updates.append((hash_path, hashlib.md5(raw).hexdigest().encode("ascii")))
    for path, raw in updates:
        write_file(path, raw, binary=True)
    return {**checks, "status": "catalogs_updated" if updates else checks["status"],
            "catalogs_modified": bool(updates), "updated_files": [p.relative_to(output).as_posix() for p, _ in updates],
            "note": "本地直读 Bundle 保留缓存 Hash/路径，更新启用的 CRC 与大小；目录 hash 使用新缓存标识。需实机验证。"
                    if updates else checks["note"]}
