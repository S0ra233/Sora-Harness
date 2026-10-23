"""仅把真实工具来源投影为页面入口；不修改工具回执或模型历史。"""
from dataclasses import replace
from functools import lru_cache
import json
import re
from desktop.plugin_api.ui import DisplayRow


@lru_cache(maxsize=128)
def _references(result):
    try:
        value = json.loads(result)
        data = value.get("data") if value.get("status") == "ok" else None
        if not isinstance(data, dict):
            return ()
        rows = data.get("results", []) if "results" in data else [data]
        return tuple((row["chunk_id"], str(row.get("title", "资料原文"))) for row in rows[:8]
                     if isinstance(row, dict) and isinstance(row.get("chunk_id"), str)
                     and re.fullmatch(r"[0-9a-f]{64}", row["chunk_id"]))
    except (ValueError, TypeError, AttributeError):
        return ()


def decorate(rows, context):
    output = []
    for row in rows:
        if row.key.startswith("rag-reference:"):
            continue
        if row.process:
            row = replace(row, process=replace(row.process, rows=decorate(row.process.rows, context)))
        output.append(row)
        seen = set()
        for tool in row.tools:
            if tool.name not in {"rag_search", "rag_read"} or tool.status != "succeeded" or not tool.result:
                continue
            for identifier, title in _references(tool.result):
                if identifier not in seen:
                    seen.add(identifier)
                    output.append(DisplayRow(f"rag-reference:{row.key}:{identifier}", "", "查看来源 · " + title,
                                             page_reference=("rag", identifier), regeneratable=False, rewindable=False))
    return tuple(output)
