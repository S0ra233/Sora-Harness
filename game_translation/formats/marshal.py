# -*- coding: utf-8 -*-
"""VX Ace / VX / XP 的 Ruby Marshal 解析与写回（基于 RPGMTL 的 MC/ME 适配）。

来源：MizaGBF/RPGMTL plugins/rm_marshal.py (MIT)
许可全文：同目录 RPGMTL-LICENSE.txt。
核心：MC/ME 类把 LINK(;/@) 原样保留，原地修改字符串 ME.data 后 dump，
      循环引用/共享对象结构不破坏。
"""
import io
import math
import struct
import re
from dataclasses import dataclass
from typing import Any

BOM = b"\xef\xbb\xbf"

class MC:
    root: Any
    symtable: list
    objtable: list

    def __init__(self):
        self.root = None
        self.symtable = []
        self.objtable = []

    @staticmethod
    def load(binary: bytes) -> "MC":
        with io.BytesIO(binary) as handle:
            if handle.read(1) != b"\x04" or handle.read(1) != b"\x08":
                raise Exception("Unsupported Ruby Marshal version or invalid file")
            mc = MC()
            mc.root = mc._process_token(handle)
            if handle.read(1):
                raise ValueError("Ruby Marshal 数据末尾存在未解析字节")
            return mc

    def dump(self) -> bytes:
        with io.BytesIO() as handle:
            handle.write(b"\x04\x08")
            self.root.dump(handle)
            return handle.getvalue()

    def _token_unimplemented(self, token, handle):
        raise Exception("[RM_Marshal] Token " + str(token) + " isn't implemented")

    def _read_exact(self, handle, size):
        if size < 0:
            raise ValueError("Ruby Marshal 长度不能为负数")
        data = handle.read(size)
        if len(data) != size:
            raise ValueError("Ruby Marshal 数据被截断")
        return data

    def _process_token(self, handle, ivar=False) -> "ME":
        token = handle.read(1)
        if token is None or token == b"":
            raise Exception("[RM_Marshal] Reached EOF")
        if token not in self.TOKEN_TABLE:
            return self._token_unimplemented(token, handle)
        else:
            if not ivar and token not in (b"0", b"T", b"F", b"i", b":", b";", b"@", b"I"):
                index = len(self.objtable)
                self.objtable.append(None)
            else:
                index = None
            me = self.TOKEN_TABLE[token](self, token, handle)
            if ivar:
                me.attributes = self._read_hashtable(b"{", handle)
                me.attributes.silent_token = True
            if not ivar and index is not None:
                self.objtable[index] = me
            return me

    def util_read_fixnum(self, handle) -> int:
        length = struct.unpack("b", handle.read(1))[0]
        if length == 0:
            return 0
        if 4 < length < 128:
            return length - 5
        elif -129 < length < -4:
            return length + 5
        result = 0
        alen = abs(length)
        data = self._read_exact(handle, alen)
        result = int.from_bytes(data, byteorder="little", signed=False)
        if length < 0:
            result -= (1 << (8 * alen))
        return result

    def util_write_fixnum(self, value: int) -> bytes:
        if value == 0:
            return b"\0"
        elif 0 < value < 123:
            return struct.pack("b", value + 5)
        elif -124 < value < 0:
            return struct.pack("b", value - 5)
        else:
            size = int(math.ceil(value.bit_length() / 8.0))
            if size > 4:
                raise Exception(f"[RM_Marshal] {value} is too long for serialization")
            back = value
            factor = 256 ** size
            if value < 0 and value == -factor:
                size -= 1
                value += factor // 256
            elif value < 0:
                value += factor
            sign = int(math.copysign(size, back))
            return struct.pack("b", sign) + value.to_bytes(size, byteorder="little", signed=False)

    def _read_nil(self, token, handle): return ME(self, token, None)
    def _read_true(self, token, handle): return ME(self, token, True)
    def _read_false(self, token, handle): return ME(self, token, False)

    def _read_instancevariable(self, token, handle):
        # I is a wrapper, not a separate object. Register its wrapped object
        # before reading attributes so later @ links keep Ruby's indices.
        me = self._process_token(handle)
        me.attributes = self._read_hashtable(b"{", handle)
        me.attributes.silent_token = True
        return me

    def _read_string(self, token, handle):
        return ME(self, token, self._read_exact(handle, self.util_read_fixnum(handle)))

    def _read_symbol(self, token, handle):
        me = ME(self, token, self._read_exact(handle, self.util_read_fixnum(handle)))
        self.symtable.append(me)
        return me

    def _read_symlink(self, token, handle):
        me = ME(self, token, self.util_read_fixnum(handle))
        if me.data < 0 or me.data >= len(self.symtable):
            raise Exception("[RM_Marshal] Symbol Link isn't pointing to an existing symbol")
        return me

    def _read_fixnum(self, token, handle):
        return ME(self, token, self.util_read_fixnum(handle))

    def _read_array(self, token, handle):
        size = self.util_read_fixnum(handle)
        return ME(self, token, [self._process_token(handle) for _ in range(size)])

    def _read_hashtable(self, token, handle):
        me = ME(self, token, None)
        size = self.util_read_fixnum(handle)
        hashtable = {}
        for _ in range(size):
            original = self._process_token(handle)
            hashtable[original.at().data] = (original, self._process_token(handle))
        me.data = hashtable
        return me

    def _read_float(self, token, handle):
        return ME(self, token, self._read_exact(handle, self.util_read_fixnum(handle)))

    def _read_bignum(self, token, handle):
        sign = handle.read(1)
        size = self.util_read_fixnum(handle)
        b = b"" if size == 0 else self._read_exact(handle, 2 * size)
        return ME(self, token, (sign, b))

    def _read_regex(self, token, handle):
        pattern = self._read_exact(handle, self.util_read_fixnum(handle))
        options = handle.read(1)
        if len(options) != 1:
            raise ValueError("Ruby Regexp 数据被截断")
        return ME(self, token, (pattern, options))

    def _read_usermarshal(self, token, handle):
        return ME(self, token, (self._process_token(handle), self._process_token(handle)))

    def _read_object(self, token, handle):
        symbol = self._process_token(handle)
        table = self._read_hashtable(b"{", handle)
        table.silent_token = True
        return ME(self, token, (symbol, table))

    def _read_link(self, token, handle):
        me = ME(self, token, self.util_read_fixnum(handle))
        if me.data < 0 or me.data >= len(self.objtable):
            raise Exception("[RM_Marshal] Link isn't pointing to an existing element")
        return me

    def _read_userdefined(self, token, handle):
        return ME(self, token, (self._process_token(handle), self._read_exact(handle, self.util_read_fixnum(handle))))

    def _read_classmodule(self, token, handle):
        return ME(self, token, self._read_exact(handle, self.util_read_fixnum(handle)))

    TOKEN_TABLE = {
        b"0": _read_nil, b"T": _read_true, b"F": _read_false, b"I": _read_instancevariable,
        b'"': _read_string, b":": _read_symbol, b";": _read_symlink, b"i": _read_fixnum,
        b"[": _read_array, b"{": _read_hashtable, b"f": _read_float, b"l": _read_bignum,
        b"/": _read_regex, b"U": _read_usermarshal, b"o": _read_object, b"@": _read_link,
        b"u": _read_userdefined, b"m": _read_classmodule, b"c": _read_classmodule,
        b"M": _read_classmodule,
    }


class HashTableIter:
    def __init__(self, d):
        self._dict_ = d
        self._keys = iter(self._dict_)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            key = next(self._keys)
            return key, self._dict_[key][1]
        except StopIteration:
            raise StopIteration


@dataclass(slots=True)
class ME:
    owner: MC
    token: bytes
    data: Any
    attributes: Any = None
    silent_token: bool = False
    _dump_call_: Any = None

    def __post_init__(self):
        self._dump_call_ = ME.DUMP_CALL_TABLE.get(self.token) or ME.dump_unimplemented

    def __repr__(self):
        return self.token.decode() + ":" + repr(self.data)

    def __str__(self):
        return str(self.data)

    def __hash__(self):
        return hash(self.data)

    def __iter__(self):
        match self.token:
            case b"[": return iter(self.data)
            case b"{": return HashTableIter(self.data)
            case b"o": return self.data[1].__iter__()
            case b";" | b"@": return self.at().__iter__()
            case _: raise Exception("This Marshal Element isn't iterable.")

    def __contains__(self, key):
        match self.token:
            case b"[": return key in self.data
            case b"{": return key in self.data
            case b"o": return self.data[1].__contains__(key)
            case b";" | b"@": return self.at().__contains__(key)
            case _: raise Exception("This Marshal Element doesn't support __contains__.")

    def get(self, key):
        match self.token:
            case b"[": return self.data[key]
            case b"{": return self.data.get(key, [None, None])[1]
            case b"o": return self.data[1].get(key)
            case b";" | b"@": return self.at().get(key)

    def at(self):
        match self.token:
            case b";": return self.owner.symtable[self.data]
            case b"@": return self.owner.objtable[self.data]
            case _: return self

    def dump(self, handle):
        if self.attributes is not None:
            handle.write(b"I")
        if not self.silent_token:
            handle.write(self.token)
        self._dump_call_(self, handle)
        if self.attributes is not None:
            self.attributes.dump(handle)

    def dump_none(self, handle): pass
    def dump_binary(self, handle):
        handle.write(self.owner.util_write_fixnum(len(self.data)))
        handle.write(self.data)
    def dump_fixnum(self, handle):
        handle.write(self.owner.util_write_fixnum(self.data))
    def dump_array(self, handle):
        handle.write(self.owner.util_write_fixnum(len(self.data)))
        for e in self.data:
            e.dump(handle)
    def dump_hashtable(self, handle):
        handle.write(self.owner.util_write_fixnum(len(self.data)))
        for k, v in self.data.items():
            v[0].dump(handle)
            v[1].dump(handle)
    def dump_object(self, handle):
        self.data[0].dump(handle)
        self.data[1].dump(handle)
    def dump_long(self, handle):
        handle.write(self.data[0])
        handle.write(self.owner.util_write_fixnum(len(self.data[1]) // 2))
        handle.write(self.data[1])
    def dump_usermarshal(self, handle):
        self.data[0].dump(handle)
        self.data[1].dump(handle)
    def dump_userdefined(self, handle):
        self.data[0].dump(handle)
        handle.write(self.owner.util_write_fixnum(len(self.data[1])))
        handle.write(self.data[1])
    def dump_regex(self, handle):
        handle.write(self.owner.util_write_fixnum(len(self.data[0])))
        handle.write(self.data[0])
        handle.write(self.data[1])
    def dump_unimplemented(self, handle):
        raise Exception("[RM_Marshal] Unknown token type:" + str(self.token))

    DUMP_CALL_TABLE = {
        b"0": dump_none, b"T": dump_none, b"F": dump_none, b'"': dump_binary,
        b":": dump_binary, b";": dump_fixnum, b"i": dump_fixnum, b"[": dump_array,
        b"{": dump_hashtable, b"o": dump_object, b"l": dump_long, b"U": dump_usermarshal,
        b"u": dump_userdefined, b"f": dump_binary, b"/": dump_regex,
        b"m": dump_binary, b"c": dump_binary, b"M": dump_binary,
        b"@": dump_fixnum,
    }

PATH_RE = re.compile(r"\.([^.\[]+)|\[(\d+)\]")

def string_value(me):
    if me is None: return None
    me = me.at()
    if me.token != b'"':
        return None
    try:
        return me.data.decode("utf-8")
    except UnicodeDecodeError:
        return me.data.decode("cp932")

def _me_code(me):
    c = me.get(b"@code")
    return c.at().data if c is not None else None

def _me_class(me):
    return me.data[0].at().data.decode("utf-8") if me.token == b"o" else None

EVENT_CODE_MAP = {
    401: ("dialogue", 0), 102: ("choice_list", 0), 402: ("choice_item", 1),
    403: ("choice_item", 1), 405: ("scroll_line", 0), 108: ("comment", 0),
    408: ("comment", 0), 320: ("name", 1), 324: ("profile", 1),
}

VX_CLASS_FIELDS = {
    "RPG::Actor": [("@name", "str"), ("@nickname", "str"), ("@profile", "str")],
    "RPG::Class": [("@name", "str")],
    "RPG::Skill": [("@name", "str"), ("@message1", "str"), ("@message2", "str"), ("@description", "str")],
    "RPG::Item": [("@name", "str"), ("@description", "str")],
    "RPG::Weapon": [("@name", "str"), ("@description", "str")],
    "RPG::Armor": [("@name", "str"), ("@description", "str")],
    "RPG::Enemy": [("@name", "str"), ("@description", "str")],
    "RPG::Troop": [("@name", "str")],
    "RPG::State": [("@name", "str"), ("@message1", "str"), ("@message2", "str"), ("@message3", "str"), ("@message4", "str")],
}
VX_SYSTEM_SINGLES = ["@game_title", "@currency_unit"]
VX_SYSTEM_LISTS = ["@elements", "@armor_types", "@weapon_types", "@skill_types", "@equip_types"]
VX_TERMS_FIELDS = [("@basic", "strlist"), ("@commands", "strlist"), ("@params", "strlist"), ("@messages", "dictlist")]

def _emit(entries, file_name, path, code, etype, text):
    if text:
        entries.append({"file": file_name, "path": path, "code": code, "type": etype, "text": text})

def _extract_commands(me_list, prefix, file_name, entries):
    for li, cmd in enumerate(me_list.data):
        code = _me_code(cmd)
        if code not in EVENT_CODE_MAP:
            continue
        etype, pidx = EVENT_CODE_MAP[code]
        params = cmd.get(b"@parameters")
        if params is None or params.token != b"[" or pidx >= len(params.data):
            continue
        val = params.data[pidx]
        if code == 102 and val.token == b"[":
            for ci, ch in enumerate(val.data):
                s = string_value(ch)
                _emit(entries, file_name, f"{prefix}[{li}].@parameters[{pidx}][{ci}]", code, "choice", s)
        else:
            s = string_value(val)
            _emit(entries, file_name, f"{prefix}[{li}].@parameters[{pidx}]", code, etype, s)

def _extract_walk_fields(me, path, file_name, entries, depth=0):
    if depth > 6 or me is None: return
    me = me.at()
    cls = _me_class(me)
    if cls in VX_CLASS_FIELDS:
        for ivar, ftype in VX_CLASS_FIELDS[cls]:
            val = me.get(ivar.encode())
            if ftype == "str":
                _emit(entries, file_name, f"{path}.{ivar}", None, "field", string_value(val))
            elif ftype == "strlist" and val is not None and val.token == b"[":
                for i, item in enumerate(val.data):
                    _emit(entries, file_name, f"{path}.{ivar}[{i}]", None, "field", string_value(item))
            elif ftype == "dictlist" and val is not None and val.token == b"{":
                for k, v in val:
                    _emit(entries, file_name, f"{path}.{ivar}.{k.decode('utf-8')}", None, "field", string_value(v))
    if me.token == b"o":
        table = me.data[1]
        for k, v in table:
            key = k.decode("utf-8")
            if key == "@list": continue
            if v.token == b"[":
                for i, item in enumerate(v.data):
                    if item.token == b"o":
                        _extract_walk_fields(item, f"{path}.{key}[{i}]", file_name, entries, depth + 1)
            elif v.token == b"o":
                _extract_walk_fields(v, f"{path}.{key}", file_name, entries, depth + 1)

def extract_vx(file_name, content):
    mc = MC.load(content)
    root = mc.root
    base = file_name.rsplit(".", 1)[0].lower()
    entries = []

    if base.startswith("map"):
        events = root.get(b"@events")
        if events is not None:
            for k, ev in events:
                if ev is None or not hasattr(ev, "get"): continue
                pages = ev.get(b"@pages")
                if pages is None or not hasattr(pages, "data"): continue
                for pi, page in enumerate(pages.data):
                    cmds = page.get(b"@list")
                    if cmds is not None and cmds.token == b"[":
                        _extract_commands(cmds, f"$.@events[{k}].@pages[{pi}].@list", file_name, entries)
        disp = string_value(root.get(b"@display_name"))
        _emit(entries, file_name, "$.@display_name", None, "field", disp)
        return entries

    if base == "system":
        for ivar in VX_SYSTEM_SINGLES:
            _emit(entries, file_name, f"$.{ivar}", None, "field", string_value(root.get(ivar.encode())))
        for ivar in VX_SYSTEM_LISTS:
            val = root.get(ivar.encode())
            if val is not None and val.token == b"[":
                for i, item in enumerate(val.data):
                    _emit(entries, file_name, f"$.{ivar}[{i}]", None, "field", string_value(item))
        terms = root.get(b"@terms")
        if terms is not None:
            for ivar, ftype in VX_TERMS_FIELDS:
                val = terms.get(ivar.encode())
                if val is None: continue
                if ftype == "strlist" and val.token == b"[":
                    for i, item in enumerate(val.data):
                        _emit(entries, file_name, f"$.@terms.{ivar}[{i}]", None, "field", string_value(item))
                elif ftype == "dictlist" and val.token == b"{":
                    for k, v in val:
                        _emit(entries, file_name, f"$.@terms.{ivar}.{k.decode('utf-8')}", None, "field", string_value(v))
        return entries

    if base == "commonevents":
        for cei, ce in enumerate(root.data):
            cmds = ce.get(b"@list")
            if cmds is not None and cmds.token == b"[":
                _extract_commands(cmds, f"$[{cei}].@list", file_name, entries)
        return entries

    if base == "troops":
        for ti, tr in enumerate(root.data):
            _emit(entries, file_name, f"$[{ti}].@name", None, "field", string_value(tr.get(b"@name")))
            pages = tr.get(b"@pages")
            if pages is not None:
                for pi, page in enumerate(pages.data):
                    cmds = page.get(b"@list")
                    if cmds is not None and cmds.token == b"[":
                        _extract_commands(cmds, f"$[{ti}].@pages[{pi}].@list", file_name, entries)
        return entries

    if root.token == b"[":
        for i, item in enumerate(root.data):
            if item.token == b"o":
                _extract_walk_fields(item, f"$[{i}]", file_name, entries)
    else:
        _extract_walk_fields(root, "$", file_name, entries)
    return entries

def resolve_path(root, path):
    node = root
    segs = []
    for m in PATH_RE.finditer(path):
        segs.append(m)
    for m in segs[:-1]:
        node = node.at()
        if m.group(1):
            node = node.get(m.group(1).encode("utf-8"))
        else:
            idx = int(m.group(2))
            node = node.get(idx) if node.token == b"{" else node.data[idx]
    last = segs[-1]
    node = node.at()
    if last.group(1):
        return node.get(last.group(1).encode("utf-8"))
    else:
        idx = int(last.group(2))
        return node.get(idx) if node.token == b"{" else node.data[idx]

def apply_vx(content, replacements):
    mc = MC.load(content)
    root = mc.root
    for path, text in replacements:
        node = resolve_path(root, path)
        if node is None:
            raise ValueError(f"路径无法定位: {path}")
        node = node.at()
        if node.token != b'"':
            raise ValueError(f"路径指向非字符串节点: {path} ({node.token})")
        node.data = text.encode("utf-8")
        if node.attributes is not None:
            encoding_flag = node.attributes.get(b"E")
            if encoding_flag is not None:
                encoding_flag.token, encoding_flag.data = b"T", True
            encoding_name = node.attributes.get(b"encoding")
            if encoding_name is not None and encoding_name.at().token == b'"':
                encoding_name.at().data = b"UTF-8"
    return mc.dump()


# Compatibility names for trusted adapters using earlier format helpers.
_me_str = string_value
_resolve_path = resolve_path
