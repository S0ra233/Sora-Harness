"""Shared Unity container loading and exact object identities."""
import io
from pathlib import Path
from .unity_types import ComponentReader
from ...formats.tables import walk_strings


def load_asset(path):
    """Keep asset names stable without retaining Windows file locks."""
    import UnityPy
    stream = io.BytesIO(Path(path).read_bytes())
    stream.name = str(path)
    env = UnityPy.load(stream)
    env.path = str(Path(path).parent)
    return env


def asset_identity(obj, tree):
    return {"path_id": obj.path_id, "file": str(obj.assets_file.name),
            "name": tree.get("m_Name", ""), "kind": obj.type.name}


def read_tree(obj, path, reader=None):
    try:
        return obj.read_typetree(), None
    except ValueError:
        reader = reader or ComponentReader.for_asset(path)
        return reader.read(obj, reader.identify(obj))


def save_tree(obj, tree, node):
    obj.save_typetree(tree, **({"nodes": node} if node is not None else {}))


def object_index(env):
    objects = {}
    for obj in env.objects:
        key = (str(obj.assets_file.name), obj.path_id)
        objects.setdefault(key, []).append(obj)
    return objects


def find_object(objects, key):
    matches = objects.get(key, [])
    if len(matches) != 1:
        raise ValueError(f"资产定位缺失或不唯一: {key[0]} / {key[1]}")
    return matches[0]


def candidate_context(parent):
    if not isinstance(parent, dict):
        return {}
    return {k: v[:160] if isinstance(v, str) else v
            for k, v in list(parent.items())[:12]
            if isinstance(v, (str, int, float, bool)) or v is None}


def json_candidates(node, parts=()):
    for path, value in walk_strings(node, parts, include_internal=True):
        if value.strip():
            yield path, value
