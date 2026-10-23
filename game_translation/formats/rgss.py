"""RGSS v1/v3 archive reader."""
from pathlib import Path
import struct
from ..storage import safe_join, separate_directories, write_file

def _read(handle, size):
    if size < 0:
        raise ValueError("负数归档长度")
    data = handle.read(size)
    if len(data) != size:
        raise ValueError("RGSS 归档被截断")
    return data


def _u32(handle):
    return struct.unpack("<I", _read(handle, 4))[0]


def _next(key):
    return (key * 7 + 3) & 0xFFFFFFFF


def decrypt_payload(blob, key):
    result = bytearray()
    for start in range(0, len(blob), 4):
        chunk = blob[start:start + 4]
        mask = key.to_bytes(4, "little")
        result.extend(value ^ mask[index] for index, value in enumerate(chunk))
        key = _next(key)
    return bytes(result)


def parse_rgss_header(path):
    """v3 has an index; v1 (XP/VX) stores headers inline with each payload."""
    total = Path(path).stat().st_size
    with open(path, "rb") as handle:
        header = _read(handle, 8)
        if header[:7] != b"RGSSAD\x00":
            raise ValueError("不是 RGSSAD 归档")
        version = header[7]
        entries = []
        if version == 3:
            key = (_u32(handle) * 9 + 3) & 0xFFFFFFFF
            while True:
                offset = _u32(handle) ^ key
                if offset == 0:
                    break
                size, payload_key, length = (_u32(handle) ^ key for _ in range(3))
                if length > 65536 or offset > total or size > total - offset:
                    raise ValueError("RGSS 索引长度/位置无效")
                mask = key.to_bytes(4, "little")
                name = bytes(value ^ mask[i % 4] for i, value in enumerate(_read(handle, length)))
                entries.append((name.decode("utf-8").replace("\\", "/"), offset, size, payload_key))
            index_end = handle.tell()
            if any(offset < index_end for _, offset, _, _ in entries):
                raise ValueError("RGSS 数据与索引区域重叠")
        elif version == 1:
            key = 0xDEADCAFE
            while handle.tell() < total:
                length = _u32(handle) ^ key
                key = _next(key)
                if length > 65536:
                    raise ValueError("RGSS 文件名长度无效")
                name = bytearray()
                for value in _read(handle, length):
                    name.append(value ^ (key & 0xFF))
                    key = _next(key)
                size = _u32(handle) ^ key
                key = _next(key)
                offset = handle.tell()
                if size > total - offset:
                    raise ValueError("RGSS 文件数据长度无效")
                entries.append((name.decode("utf-8").replace("\\", "/"), offset, size, key))
                handle.seek(size, 1)
        else:
            raise ValueError(f"尚无 RGSS 头版本 {version} 的解析器；请在工作目录中补适配")
    return version, entries


def unpack_rgss(path, outdir):
    path, output = Path(path).resolve(), Path(outdir).resolve()
    separate_directories(path.parent, output)
    version, entries = parse_rgss_header(path)
    # Validate every destination before creating any files.
    targets = [safe_join(output, name) for name, _, _, _ in entries]
    if len(targets) != len(set(targets)):
        raise ValueError("RGSS 归档包含重复输出路径")
    with open(path, "rb") as handle:
        for target, (_, offset, size, key) in zip(targets, entries):
            handle.seek(offset)
            write_file(target, decrypt_payload(_read(handle, size), key), binary=True)
    return str(output)


def unpack_game_dir(game_dir, outdir=None):
    game = Path(game_dir).resolve()
    archives = sorted(p for p in game.iterdir() if p.suffix.lower() in (".rgss3a", ".rgss2a", ".rgssad"))
    if not archives:
        return None, None
    if len(archives) != 1:
        raise ValueError("存在多个 RGSS 归档，需要确认主归档")
    output = Path(outdir) if outdir else game.with_name(game.name + "_unpacked")
    unpack_rgss(archives[0], output)
    for sub in ("Data", "data", ""):
        data = output / sub
        if data.is_dir() and any(p.suffix.lower() in (".rvdata2", ".rvdata", ".rxdata") for p in data.iterdir()):
            return str(output.resolve()), str(data.resolve())
    raise ValueError("归档已解包，但没有找到 RPG 数据目录")
