"""Indexed IL2CPP literal data, deliberately limited to the checked v29 layout.

Literal indexes are shared by their callers. Relocating the data block preserves
indexes; it does not create independent translations for each code occurrence.
"""
import struct


SUPPORTED_VERSIONS = {29}


def parse(raw):
    if len(raw) < 256:
        raise ValueError("metadata 文件头不完整")
    magic, version, table, table_size, data, data_size = struct.unpack_from("<6I", raw)
    if magic != 0xFAB11BAF:
        raise ValueError("不是可识别的 IL2CPP metadata；未尝试解密")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"metadata v{version} 尚未支持；当前仅启用 v29")
    if table_size % 8:
        raise ValueError("字符串索引表长度不是 8 的倍数")
    for start, size in ((table, table_size), (data, data_size)):
        if start < 256 or start > len(raw) or size > len(raw) - start:
            raise ValueError("字符串表或数据区超出 metadata 范围")
    if max(table, data) < min(table + table_size, data + data_size):
        raise ValueError("字符串索引表与数据区重叠")
    strings = []
    for position in range(table, table + table_size, 8):
        length, offset = struct.unpack_from("<II", raw, position)
        if offset > data_size or length > data_size - offset:
            raise ValueError("字符串记录超出数据区范围")
        strings.append(raw[data + offset:data + offset + length])
    return {"version": version, "table_offset": table, "strings": strings}


def literal_index(entry, document):
    prefix = "/string_literals/"
    path = entry["path"]
    if not path.startswith(prefix) or not path[len(prefix):].isdecimal():
        raise ValueError("无效的 metadata 字符串索引")
    index = int(path[len(prefix):])
    if path != prefix + str(index) or index >= len(document["strings"]):
        raise ValueError("metadata 字符串索引不存在")
    if entry["structure"].get("metadata_version") != document["version"]:
        raise ValueError("metadata 版本与提取记录不一致")
    return index


def read(raw, entries):
    document = parse(raw)
    return [document["strings"][literal_index(e, document)].decode("utf-8") for e in entries]


def replace(raw, items):
    document = parse(raw)
    strings, seen = list(document["strings"]), set()
    for entry, text in items:
        index = literal_index(entry, document)
        if index in seen:
            raise ValueError("重复修改同一个 metadata 字符串索引")
        seen.add(index)
        if strings[index].decode("utf-8") != entry["text"]:
            raise ValueError("metadata 字符串原文已改变，拒绝写回")
        strings[index] = text.encode("utf-8")
    # Preserve every original byte outside the literal records and data pointer.
    # Appending avoids moving unrelated metadata sections when translations grow.
    result = bytearray(raw)
    result.extend(bytes((-len(result)) % 4))
    data_offset = len(result)
    offset = 0
    for index, value in enumerate(strings):
        if len(value) > 0xFFFFFFFF or offset + len(value) > 0xFFFFFFFF:
            raise ValueError("字符串数据区超过 metadata 32 位长度范围")
        struct.pack_into("<II", result, document["table_offset"] + index * 8, len(value), offset)
        result.extend(value)
        offset += len(value)
    if data_offset > 0xFFFFFFFF:
        raise ValueError("metadata 文件超出 32 位偏移范围")
    struct.pack_into("<II", result, 16, data_offset, offset)
    return bytes(result)
