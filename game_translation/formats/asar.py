"""Standard Electron ASAR index and streaming replacement, using only stdlib.

Format reference: electron/asar src/disk.ts and src/integrity.ts. Implemented
here without bundling upstream code; offsets are relative to the payload.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile

from ..storage import safe_join, write_file


def read_index(path):
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as handle:
        prefix = handle.read(16)
        if len(prefix) != 16:
            raise ValueError("ASAR 索引头不完整")
        size_payload, header_size, payload_size, string_size = struct.unpack("<4I", prefix)
        if (size_payload != 4 or header_size < 8 or header_size > size - 8 or
                payload_size != header_size - 4 or string_size > payload_size - 4):
            raise ValueError("ASAR Pickle 长度与归档边界不一致")
        raw = handle.read(string_size)
    header = json.loads(raw.decode("utf-8"))
    if not isinstance(header, dict) or not isinstance(header.get("files"), dict):
        raise ValueError("ASAR 索引没有标准 files 目录")
    return header, 8 + header_size


def _nodes(header, prefix="", unpacked=False):
    if not isinstance(header.get("files"), dict):
        raise ValueError("ASAR 目录 files 不是对象")
    for name, node in header["files"].items():
        if not name or name in {".", ".."} or any(c in name for c in "/\\:\0"):
            raise ValueError(f"ASAR 索引路径无效: {name!r}")
        if not isinstance(node, dict):
            raise ValueError("ASAR 索引节点不是对象")
        path = prefix + name
        external = unpacked or bool(node.get("unpacked"))
        if "files" in node:
            yield from _nodes(node, path + "/", external)
        else:
            yield path, node, external


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.header, self.data_offset = read_index(path)
        self.nodes = {name: (node, external) for name, node, external in _nodes(self.header)}
        archive_size = self.path.stat().st_size
        for name, (node, external) in self.nodes.items():
            if "link" in node:
                if not isinstance(node["link"], str):
                    raise ValueError(f"ASAR 链接路径无效: {name}")
                continue
            size = node.get("size")
            if type(size) is not int or size < 0:
                raise ValueError(f"ASAR 文件长度无效: {name}")
            if not external:
                raw_offset = node.get("offset")
                if not isinstance(raw_offset, str) or not raw_offset.isdecimal():
                    raise ValueError(f"ASAR 文件偏移无效: {name}")
                offset = int(raw_offset)
                if offset < 0 or self.data_offset + offset + size > archive_size:
                    raise ValueError(f"ASAR 文件越过归档边界: {name}")

    def resolve(self, name):
        seen = set()
        while "link" in self.nodes[name][0]:
            if name in seen:
                raise ValueError("ASAR 符号链接循环")
            seen.add(name)
            name = self.nodes[name][0]["link"].replace("\\", "/")
        return name

    def external_path(self, name):
        return safe_join(Path(str(self.path) + ".unpacked"), name)

    def read(self, name):
        name = self.resolve(name)
        node, external = self.nodes[name]
        if external:
            value = self.external_path(name).read_bytes()
        else:
            with self.path.open("rb") as handle:
                handle.seek(self.data_offset + int(node.get("offset", "0")))
                value = handle.read(node["size"])
        if len(value) != node["size"]:
            raise ValueError(f"ASAR 文件内容长度不符: {name}")
        return value

    def replace(self, replacements):
        resolved = {}
        for name, value in replacements.items():
            target = self.resolve(name)
            if target in resolved and resolved[target] != value:
                raise ValueError(f"ASAR 链接指向同一文件但译文不同: {target}")
            resolved[target] = value
        header = copy.deepcopy(self.header)
        records, offset = [], 0
        for name, node, external in _nodes(header):
            if "link" in node:
                continue
            if name in resolved:
                node["size"] = len(resolved[name])
                if "integrity" in node:
                    node["integrity"] = _integrity(resolved[name], node["integrity"])
            if external:
                continue
            node["offset"] = str(offset)
            offset += node["size"]
            records.append((name, node["size"]))
        self._publish(header, records, resolved)

    def _publish(self, header, records, replacements):
        # Failed writes retain the complete/partial temporary archive. Core only
        # calls this on its staging copy; the original game is never published.
        with tempfile.NamedTemporaryFile("wb", dir=self.path.parent,
                prefix="." + self.path.name + ".", delete=False) as output:
            temporary = Path(output.name)
            output.write(_encode_header(header))
            with self.path.open("rb") as source:
                for name, size in records:
                    if name in replacements:
                        output.write(replacements[name])
                    else:
                        source.seek(self.data_offset + int(self.nodes[name][0].get("offset", "0")))
                        _copy_bytes(source, output, size)
            output.flush()
            os.fsync(output.fileno())
        for name, value in replacements.items():
            if self.nodes[name][1]:
                write_file(self.external_path(name), value, binary=True)
        os.replace(temporary, self.path)


def _encode_header(header):
    value = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = struct.pack("<I", len(value)) + value
    payload += bytes((-len(payload)) % 4)
    pickle = struct.pack("<I", len(payload)) + payload
    return struct.pack("<II", 4, len(pickle)) + pickle


def _copy_bytes(source, output, size):
    while size:
        chunk = source.read(min(size, 1024 * 1024))
        if not chunk:
            raise ValueError("ASAR 复制时资源提前结束")
        output.write(chunk)
        size -= len(chunk)


def _integrity(value, previous):
    if previous.get("algorithm") != "SHA256":
        raise ValueError("ASAR 文件校验算法尚未支持")
    block_size = previous.get("blockSize", 4 * 1024 * 1024)
    if type(block_size) is not int or block_size <= 0:
        raise ValueError("ASAR 校验块大小无效")
    blocks = [hashlib.sha256(value[i:i + block_size]).hexdigest()
              for i in range(0, len(value), block_size)]
    return {"algorithm": "SHA256", "hash": hashlib.sha256(value).hexdigest(),
            "blockSize": block_size, "blocks": blocks or [hashlib.sha256(b"").hexdigest()]}


def electron_fuses(executable):
    """Read the documented v1 fuse wire; never execute or patch the EXE."""
    marker = b"dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX"
    tail = b""
    with Path(executable).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            data = tail + chunk
            index = data.find(marker)
            if index >= 0 and len(data) >= index + len(marker) + 2:
                wire = data[index + len(marker):]
                version, size = wire[:2]
                if len(wire) < 2 + size:
                    data += handle.read(2 + size - len(wire))
                    wire = data[index + len(marker):]
                if version != 1 or size <= 4 or len(wire) < 2 + size:
                    return {"status": "unknown", "reason": "不支持的 Electron fuse wire"}
                state = wire[2 + 4]
                if state not in (ord("0"), ord("1")):
                    return {"status": "unknown", "reason": "无法确认 ASAR 完整性开关"}
                return {"status": "read", "embedded_asar_integrity": state == ord("1"),
                        "only_load_asar": size > 5 and wire[2 + 5] == ord("1")}
            tail = data[-(len(marker) + 2):]
    return {"status": "not_found", "reason": "未发现标准 fuse wire；未确认自定义加载校验"}
