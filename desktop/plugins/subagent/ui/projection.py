"""父聊天的子状态行来自管理器；工具回执和有效历史保持原样。"""
import json
from functools import lru_cache

from ....ui.chat.projection import DisplayRow


@lru_cache(maxsize=256)
def _child_id(result):
    try:
        data = json.loads(result)
        payload = data.get("data") if data.get("status") == "ok" else None
        return payload.get("child_id") if isinstance(payload, dict) else None
    except (ValueError, TypeError):
        return None


def decorate(rows, context, access):
    if not context.conversation_id:
        return rows
    records = {item["child_id"]: item for item in access.list(context.conversation_id)}
    seen = set()
    def identifiers(row):
        found = [_child_id(tool.result) for tool in row.tools if tool.result]
        if row.process:
            found.extend(identifier for item in row.process.rows for identifier in identifiers(item))
        return found
    output = []
    for row in rows:
        if row.key.startswith("child-status:"):
            continue
        output.append(row)
        for child_id in identifiers(row):
            if child_id in seen or child_id not in records:
                continue
            seen.add(child_id)
            item = records[child_id]
            phase = {"starting": "开始工作", "running": "正在工作", "cancelling": "正在停止", "completed": "已完成",
                     "failed": "运行失败", "interrupted": "已停止", "timed_out": "已超时", "save_failed": "保存未确认"}.get(item["status"], item["status"])
            output.append(DisplayRow("child-status:" + child_id, "", item["name"] + " " + phase,
                                     page_reference=("subagent", child_id)))
    return tuple(output)
