"""模型通过任务ID管理子工作，调用受理与子任务终态分别反馈。"""
from ...llm.messages import ToolSchema
from ...tools.contracts import ToolDefinition, ToolDisplay, ToolError
from ...runtime.subagents import SUBAGENT_TOOLS


def create_tools():
    text = {"type": "string"}
    specs = (
        ("subagent", ("启动一个拥有独立上下文历史的后台子智能体（Subagent）来并行处理明确的拆分任务（如独立模块分析、分 Scene 翻译/审校或专项背景调查）。调用时须注意：\n1. **自包含委托**：子智能体不会自动继承主"
            "聊天的对话历史或已加载的 Skill 正文。请在 `task` 与 `context` 中完整写明：具体目标、负责的范围边界（禁止自行扩权）、输出格式/结果文件路径、完成标准，以及你已经查好的必要背景片段或术语"
            "规则。\n2. **共享工作区协调**：子智能体与主任务共享工作目录租约——只读工具可与主任务并发运行，而文件修改与命令执行会由平台写锁自动串行化。为避免逻辑冲突，不要让多个并发子任务直接修改同一个目标文件，优先"
            "让子任务生成各自独立的结果文件，再由主任务统一审核汇总。\n3. **异步受理语义**：调用成功返回 `child_id` 和 `run_id` 仅表示后台已受理启动（`starting`/`running`）。"
            "启动后你可以继续执行主线其他工作，或调用 `wait_subagents` / `get_subagent_result` 跟进结果。"),
         {"profile_id": text, "task": text, "context": text}, ("profile_id", "task")),
        ("send_subagent", ("向已创建的子智能体（`child_id`）发送补充说明或继续指令。若该子任务正在运行，补充内容会在其当前模型步骤与关联工具批次结算后安全注入；若该子任务已结束、超时或暂停且仍满足原目录与工具范围，则会沿用其原有"
            "的独立历史启动新一轮继续运行。重新委托或修正范围时，请明确重申当前有效的目标与包/文件版本。save_failed、cancelling或父目标已停止时不可继续；投递未确认先核对子日志，不要重复发送。"),
         {"child_id": text, "message": text, "context": text}, ("child_id", "message")),
        ("list_subagents", ("列出当前主会话下所有已创建的子智能体及其真实运行状态（`starting`、`running`、`cancelling`、`completed`、`failed`、`timed_out`、`interrupte"
            "d`、`save_failed`）、所属角色、运行时长与最新运行 ID。用于全局盘点子任务进度。"), {}, ()),
        ("get_subagent_result", ("读取指定子智能体（`child_id` 及可选 `run_id`）的真实终态、执行摘要、资源用量与有界最终回复正文（超长正文可将 next_offset 传入 offset 分页续读，每次最多12,000字符）"
            "。注意：本工具调用成功仅表示“成功取回了子任务记录”，请务必检查返回内容中的子任务真实终态（是否 `completed`）、目标覆盖是否完整，并在采纳其结论或提交译文前核对工作区内的实际结果文件。"),
         {"child_id": text, "run_id": text, "offset": {"type": "integer", "minimum": 0}}, ("child_id",)),
        ("wait_subagents", ("等待指定的1～8个子智能体（`child_ids`）中任一任务达到终态，单次等待时长默认 30 秒、最高 60 秒。注意：如果达到 `timeout_seconds` 返回 `wait_expired`（状态仍"
            "为 `running`），仅代表本次等待窗口结束，**不代表子任务失败或已停止**——你可以根据情况再次调用 `wait_subagents` 或先处理其他工作。timeout_seconds=0可立即查看当前"
            "状态。"),
         {"child_ids": {"type": "array", "items": text, "minItems": 1, "maxItems": 8},
          "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 60}}, ("child_ids",)),
        ("cancel_subagent", ("请求停止指定的子智能体（`child_id`）运行并保存进度，由平台回收后台执行器与派生子进程树；请求受理不代表回收已确认，需观察真实终态。适用于发现拆方向有误、目标已变更或子任务卡死需要人工中止的场景。取消后"
            "先核对已落盘的局部结果文件状态。"), {"child_id": text}, ("child_id",)),
    )
    for name, description, properties, required in specs:
        async def execute(arguments, context, tool_name=name, fields=frozenset(properties), required_fields=required):
            if arguments.keys() - fields or any(field not in arguments for field in required_fields):
                raise ToolError("invalid_arguments", "子Agent工具参数缺失或包含未知字段。")
            if context.subagents is None:
                raise ToolError("subagent_unavailable", "本轮未绑定子Agent能力。")
            return await context.subagents.dispatch(tool_name, arguments)
        yield ToolDefinition(ToolSchema(name, description, {"type": "object", "properties": properties,
                             "required": list(required), "additionalProperties": False}), execute,
                             ToolDisplay(category="subagent", icon="plugin", verb="委托", summary="管理了{n}个子任务",
                                         heading="子Agent", subject_fields=("task", "child_id")),
                             timeout_seconds=None, execution_class="control")
