"""提问投影只读真实工具结果，用户回答仍走原会话消息日志。"""
from dataclasses import dataclass, replace

from ...llm.messages import ModelMessage, QuestionReply, TextContent
from ...tools.questions import UserQuestion, questions_from_result


@dataclass(frozen=True)
class AskedQuestion:
    run_id: str
    call_id: str
    question: UserQuestion
    answer: str | None = None
    sending: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return self.run_id, self.call_id, self.question.id


def collect_questions(conversation) -> dict[tuple[str, str, str], AskedQuestion]:
    """打开／修订后重建一次；正常流式刷新不反复扫描整份历史。"""
    questions, replies = {}, []
    for turn in conversation.turns:
        if turn.user is not None and turn.user.question_reply is not None:
            replies.append(turn.user.question_reply)
        for step in turn.steps:
            for tool in step.tools:
                for question in questions_from_result(tool.result):
                    item = AskedQuestion(turn.run_id, tool.call_id, question)
                    questions[item.key] = item
    replies.extend(item.message.question_reply for item in conversation.inputs if item.message.question_reply is not None)
    for reply in replies:
        key = reply.run_id, reply.call_id, reply.question_id
        if key in questions:
            questions[key] = replace(questions[key], answer=reply.answer)
    return questions


def reply_message(item: AskedQuestion, answer: str) -> ModelMessage:
    reply = QuestionReply(item.run_id, item.call_id, item.question.id, answer.strip())
    # 正文也带原问题，压缩后迟到的回答仍有明确含义；不改写已返回的工具结果。
    return ModelMessage("user", [TextContent(f"关于问题：{item.question.question}\n\n我的回答：{reply.answer}")],
                        question_reply=reply)


def validate_question_reply(conversation, message: ModelMessage | None) -> None:
    if message is None or message.question_reply is None:
        return
    reply = message.question_reply
    item = collect_questions(conversation).get((reply.run_id, reply.call_id, reply.question_id))
    if item is None:
        raise ValueError("原问题不在当前有效会话中，回答未发送。")
    if item.answer is not None:
        raise ValueError("这个问题已经收到回答，请通过普通对话补充新的意见。")
