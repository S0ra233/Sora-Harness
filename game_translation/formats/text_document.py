"""Text byte encodings and content hints, without choosing translation fields."""
import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    bom: bytes = b""

    def encode(self, text):
        return self.bom + text.encode(self.encoding)


def decode_text(value):
    # UnityPy represents binary TextAsset contents as UTF-8 with surrogateescape.
    raw = value.encode("utf-8", "surrogateescape") if isinstance(value, str) else bytes(value)
    for bom, encoding in ((b"\xff\xfe\0\0", "utf-32-le"), (b"\0\0\xfe\xff", "utf-32-be"),
                          (b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"),
                          (b"\xfe\xff", "utf-16-be")):
        if raw.startswith(bom):
            return DecodedText(raw[len(bom):].decode(encoding), encoding, bom)
    # XML's initial '<' bytes identify Unicode encodings even without a BOM.
    for prefix, encoding in ((b"<\0\0\0", "utf-32-le"), (b"\0\0\0<", "utf-32-be"),
                             (b"<\0", "utf-16-le"), (b"\0<", "utf-16-be")):
        if raw.startswith(prefix):
            return DecodedText(raw.decode(encoding), encoding)
    return DecodedText(raw.decode("utf-8"), "utf-8")


def detect_format(text, name=""):
    value = text.lstrip()
    suffix = Path(name).suffix.lower()
    if value.startswith(("{", "[")):
        try:
            json.loads(text)
            return "json", "content"
        except ValueError:
            pass
    if value.startswith("<"):
        from .xml_text import parse
        try:
            parse(text)  # Full document, not just an opening tag.
            return "xml", "content"
        except ValueError:
            if suffix == ".xml" or value.startswith("<?xml"):
                raise
            # Markup in a plain script is not necessarily a complete XML file.
    if suffix in {".csv", ".tsv"}:
        return suffix[1:], "name"
    from .key_value import parse as parse_key_value
    try:
        records = parse_key_value(text)["records"]
        if len(records) >= 2 or (suffix == ".properties" and records):
            return "key-value", "content"
    except ValueError:
        if suffix == ".properties":
            raise
    if suffix == ".txt":
        return "text", "name"
    for delimiter, fmt in (("\t", "tsv"), (",", "csv")):
        try:
            rows = []
            for row in csv.reader(io.StringIO(text), delimiter=delimiter, strict=True):
                if row:
                    rows.append(row)
                if len(rows) >= 100:
                    break
        except csv.Error:
            continue
        widths = [len(row) for row in rows]
        if len(widths) >= 2:
            width = max(set(widths), key=widths.count)
            if fmt == "tsv" and sum(w > 1 for w in widths) / len(widths) >= 0.8:
                return fmt, "content"
            if fmt == "csv" and width > 1 and widths.count(width) / len(widths) >= 0.8:
                return fmt, "content"
    return None, "unknown"


def declared_locale(name):
    from ..localization import normalize_locale
    name = str(name).strip().lower().replace("_", "-")
    prefixed = False
    for prefix in ("text-", "value-", "language-", "lang-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            prefixed = True
            break
    if name == "id" and not prefixed:
        return None  # Usually an identifier; text_id is explicit Indonesian.
    name = normalize_locale(name)
    aliases = {"jp": "ja", "cn": "zh-hans", "zh-tw": "zh-hant", "zh-cn": "zh-hans"}
    name = aliases.get(name, name)
    base = name.split("-", 1)[0]
    return name if base in {"ja", "en", "zh", "ko", "ru", "fr", "de", "es", "it", "pt",
                            "pl", "nl", "tr", "ar", "th", "vi", "id", "uk", "cs", "sv",
                            "fi", "da", "nb", "hu", "ro", "el", "he", "hi"} else None
