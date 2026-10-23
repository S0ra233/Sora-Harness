"""Utage book objects; byte layout remains in formats.utage."""
from ...formats import utage
from .unity_objects import asset_identity


def load_book(obj):
    """Resolve class identity before parsing a stripped 64-bit LE object."""
    if obj.type.name != "MonoBehaviour" or obj.reader.endian != "<":
        return None
    base = obj.read(check_read=False)
    if not getattr(base, "m_Name", "").endswith(".book"):
        return None
    script = base.m_Script.read()
    if (script.m_Namespace, script.m_ClassName) != ("Utage", "AdvImportBook"):
        return None
    book = utage.parse(obj.get_raw_data())
    if book["name"] != base.m_Name:
        raise ValueError("Utage 对象名称与基础头不一致")
    return book


def extract(obj, book, file, entries, warnings):
    items, issues = utage.extract(book)
    identity = asset_identity(obj, {"m_Name": book["name"]})
    for entry in items:
        entry.update(file=file, asset=identity,
                     scene=file + "::" + str(obj.path_id) + ":" + entry["scene"])
        entry["candidate_context"] = {"command": entry["structure"]["command"],
            "arguments": [value[:160] for value in entry["structure"]["arguments"][:8]]}
    entries.extend(items)
    warnings.extend(issues)


def _checked_book(obj, entries):
    book = load_book(obj)
    if book is None or any(asset_identity(obj, {"m_Name": book["name"]}) != e["asset"] for e in entries):
        raise ValueError("Utage 资产身份改变")
    return book


def read(obj, entries, path, reader=None):
    book = _checked_book(obj, entries)
    values = []
    for entry in entries:
        sheet, row, column = map(int, entry["path"].strip("/").split("/"))
        cells = next(r for r in book["sheets"][sheet]["rows"] if r["index"] == row)["cells"]
        values.append(cells[column]["text"])
    return values


def write(obj, items, path, reader=None):
    _checked_book(obj, [entry for entry, _ in items])
    obj.set_raw_data(utage.replace(obj.get_raw_data(), items))
    return len(items)
