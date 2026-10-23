"""试运行插件的回复分段显示；与主应用布局和模型有效历史分开。"""
from collections.abc import Sequence

from ....llm.attempts import ModelAttemptEvent


def format_replies(attempts: Sequence[ModelAttemptEvent], current_text: str) -> str:
    """中断回复保留原片段；最后一次完整文本不与旧片段拼成一条回答。"""
    sections = []
    for event in attempts:
        if event.phase in ("failed", "cancelled"):
            status = "已取消" if event.phase == "cancelled" else event.error or "请求中断"
            sections.append(f"回复 {event.attempt}（{status}，未完成）\n{event.partial_text or '未收到文本。'}")
    if attempts and attempts[-1].phase not in ("failed", "cancelled"):
        sections.append(f"回复 {attempts[-1].attempt}\n{current_text}")
    elif not attempts:
        sections.append(current_text)
    return "\n\n".join(sections)
