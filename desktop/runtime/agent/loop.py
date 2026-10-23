"""模型请求与本地工具之间的单次循环；不处理插件业务或界面布局。"""
import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from time import monotonic

from ...llm.contracts import ModelBackend, TextSink
from ...llm.diagnostics import RequestTraceFactory, call_observed_model
from ...llm.messages import ModelMessage, expand_skill_messages
from ...llm.models import ModelRequest, ModelResponse
from ...llm.usage import normalize_usage
from ...llm.providers.http import ProviderError
from ...llm.attempts import ModelAttemptEvent
from ...llm.reasoning_output import ReasoningBlock, public_reasoning
from ...tools.contracts import ToolContext, ToolExecutionUnknown
from ...tools.registry import ToolRegistry, error_result
from ..runs import exception_diagnostics
from .context import update_runtime_context
from .models import (
    AgentCancelled, AgentCheckpoint, AgentCheckpointSink, AgentEvent, AgentEventSink,
    AgentInputBatch, AgentInputSource, AgentLimits, AgentRequestPolicy, AgentResult, AgentStep, ToolExecution,
)


class AgentLoop:
    def __init__(self, tools: ToolRegistry) -> None:
        self._tools = tools

    async def run(
        self, request: ModelRequest, backend: ModelBackend, context: ToolContext | None,
        limits: AgentLimits, *, on_text: TextSink | None = None,
        on_event: AgentEventSink | None = None, on_checkpoint: AgentCheckpointSink | None = None,
        before_request: AgentRequestPolicy | None = None,
        request_traces: RequestTraceFactory | None = None,
        input_source: AgentInputSource | None = None,
    ) -> AgentResult:
        """一次复制隔离调用方输入；后续历史只追加完整消息与关联工具结果。"""
        result = AgentResult(initial_request=deepcopy(request))
        history = list(result.initial_request.messages)
        seen_ids = {call.call_id for message in history if isinstance(message, ModelMessage) for call in message.tool_calls}
        if context is not None:
            seen_ids.update(context.artifact_calls)
        tool_attempts = 0
        batch = AgentInputBatch()
        try:
            while True:
                # 同步小文件读取后也给事件循环机会接收取消，不直接开始下次请求。
                await asyncio.sleep(0)
                if len(result.steps) >= limits.max_steps:
                    result.stop_reason = "step_limit"
                    return result
                if not batch.input_ids and not batch.program_event_ids and input_source is not None:
                    batch = input_source(False)
                if batch.messages:
                    # 第一步的环境更新原在末尾，按补充→程序资料的检查点顺序发送与恢复。
                    updates = result.initial_request.runtime_context_updates if not result.steps else []
                    incoming = expand_skill_messages(deepcopy(list(batch.messages)))
                    if updates and history[-len(updates):] == updates:
                        history = history[:-len(updates)]
                        history.extend(incoming)
                        history.extend(updates)
                    else:
                        history.extend(incoming)
                # 沿用初始生成参数；追加工具结果时不能漏掉 effort 等后续字段。
                number = len(result.steps) + 1
                current = update_runtime_context(replace(result.initial_request, messages=history,
                                                         runtime_context_updates=[] if result.steps else result.initial_request.runtime_context_updates), limits,
                                                 step=number, tool_calls_used=tool_attempts)
                if before_request is not None:
                    current = await before_request(current, backend)
                history = current.messages
                # 预算或摘要失败发生在请求开始前，不伪造一个已经发出的模型步骤。
                notices = [message for message in batch.messages if message.runtime_context_kind == "subagent_notice"]
                step = AgentStep(number, context_messages=deepcopy([*notices, *current.runtime_context_updates]),
                                 input_ids=list(batch.input_ids), program_event_ids=list(batch.program_event_ids))
                batch = AgentInputBatch()
                result.steps.append(step)
                if on_checkpoint is not None:
                    on_checkpoint(AgentCheckpoint("model_started", number,
                                                  {"context_messages": [asdict(message) for message in step.context_messages],
                                                   "input_ids": step.input_ids, "program_event_ids": step.program_event_ids}))
                self._notify(on_event, AgentEvent("model_started", number, input_ids=tuple(step.input_ids)))
                response = await self._request_step(current, backend, step, on_text, on_event, on_checkpoint, request_traces)
                try:
                    self._accept_response(step, response, seen_ids)
                except (ProviderError, ValueError):
                    # 正文确实已收到，但关联检查失败；保留失败输出，不作为可执行历史。
                    step.partial_text = response.text
                    step.completion_status = "failed"
                    raise
                if on_checkpoint is not None:
                    on_checkpoint(AgentCheckpoint("model_finished", number, asdict(step)))
                self._notify(on_event, AgentEvent("model_finished", number, text=step.completion_status,
                                                 token_usage=step.token_usage, reasoning_blocks=tuple(step.reasoning),
                                                 reasoning_active=step.reasoning_active))
                if step.assistant is None:
                    result.stop_reason = "model_incomplete" if step.completion_status == "incomplete" else "model_unknown"
                    return result
                history.append(step.assistant)
                if not step.assistant.tool_calls:
                    await asyncio.sleep(0)
                    batch = input_source(True) if input_source is not None else AgentInputBatch()
                    if batch.input_ids or batch.program_event_ids:
                        continue
                    result.stop_reason = "completed"
                    return result
                if number == limits.max_steps:
                    result.stop_reason = "step_limit"
                    self._settle_pending(step, "step_limit", "模型步骤已达上限，本批工具未执行。")
                    return result
                if tool_attempts + len(step.tools) > limits.max_tool_calls:
                    result.stop_reason = "tool_limit"
                    self._settle_pending(step, "tool_limit", "本批调用超过剩余工具额度，整批未执行。")
                    return result
                if context is None:
                    raise ProviderError("纯对话没有本地工具执行范围，模型返回的工具调用未执行。")
                await self._execute_tools(step, context, on_event, on_checkpoint)
                tool_attempts += len(step.tools)
                history.extend(execution.result for execution in step.tools)
        except asyncio.CancelledError as exc:
            result.stop_reason = "cancelled"
            if result.steps:
                last = result.steps[-1]
                if last.completion_status == "requesting":
                    last.completion_status = "cancelled"
                self._settle_pending(last, "cancelled", "运行已取消，该调用未继续执行。")
            # 保持 asyncio 的取消语义，入口可以先保存已有事实，再继续传递异常。
            raise AgentCancelled(result) from exc
        except Exception as exc:
            result.stop_reason = "failed"
            result.error = str(exc) if isinstance(exc, (ProviderError, ToolExecutionUnknown)) else f"Agent 运行发生内部错误（{type(exc).__name__}），请检查错误位置。"
            result.diagnostics = exception_diagnostics(exc)
            if result.steps:
                last = result.steps[-1]
                if last.completion_status == "requesting":
                    last.completion_status = "failed"
                self._settle_pending(last, "execution_error", "运行发生内部错误，该调用没有可确认的成功结果。")
            return result

    async def _request_step(
        self, request: ModelRequest, backend: ModelBackend, step: AgentStep,
        on_text: TextSink | None, on_event: AgentEventSink | None,
        on_checkpoint: AgentCheckpointSink | None,
        request_traces: RequestTraceFactory | None,
    ) -> ModelResponse:
        """片段先展示；失败时才拼接为未完成文本，成功以正式响应为准。"""
        fragments = []
        reasoning = {}

        def public_blocks():
            return tuple(ReasoningBlock(identifier, "".join(parts), kind) for identifier, (kind, parts) in reasoning.items())

        def accept_attempt(event: ModelAttemptEvent) -> None:
            if event.phase == "started":
                fragments.clear()
                reasoning.clear()
                step.reasoning = []
                step.reasoning_active = None
            elif event.phase in {"failed", "cancelled"}:
                event = replace(event, reasoning_blocks=public_blocks(), reasoning_active=step.reasoning_active)
            step.attempts.append(event)
            if on_checkpoint is not None:
                on_checkpoint(AgentCheckpoint("model_attempt", step.step, {"attempt": asdict(event)}))
            self._notify(on_event, AgentEvent("model_attempt", step.step, attempt=event))

        def accept_text(text: str) -> None:
            fragments.append(text)
            self._notify(on_event, AgentEvent("text_delta", step.step, text=text))
            on_text(text)

        def accept_reasoning(delta) -> None:
            step.reasoning_active = True
            if delta.text:
                _, parts = reasoning.setdefault(delta.block_id, (delta.kind, []))
                parts.append(delta.text)
            self._notify(on_event, AgentEvent("reasoning_delta", step.step, reasoning=delta))

        try:
            trace = request_traces.for_request(request, purpose="agent", step=step.step) if request_traces else None
            return await call_observed_model(backend, request, on_text=accept_text if on_text is not None else None,
                on_attempt=accept_attempt, on_reasoning=accept_reasoning, trace=trace)
        except BaseException:
            step.partial_text = "".join(fragments)
            raise
        finally:
            # 只在本次请求退出时拼装持久投影；实时界面接收增量通知。
            step.reasoning = list(public_blocks())

    @staticmethod
    def _accept_response(step: AgentStep, response: ModelResponse, seen_ids: set[str]) -> None:
        """完整性和 ID 在执行前核对，不能等下一次请求才发现关联错误。"""
        step.completion_status = response.completion_status
        step.finish_reason, step.usage = response.finish_reason, response.usage
        step.token_usage = response.token_usage or normalize_usage(
            response.usage, response.replay_state.api_style if response.replay_state is not None else None,
        )
        blocks, active = public_reasoning(response.replay_state)
        if blocks:
            step.reasoning = list(blocks)
        if active is not None:
            step.reasoning_active = active
        elif step.token_usage is not None and step.token_usage.reasoning_tokens is not None:
            step.reasoning_active = step.token_usage.reasoning_tokens > 0
        if response.completion_status not in ("completed", "requires_tool"):
            if response.tool_calls:
                raise ProviderError("模型输出未完整结束，不能执行其中的工具调用。")
            step.partial_text = response.text
            return
        if bool(response.tool_calls) != (response.completion_status == "requires_tool"):
            raise ProviderError("模型完成状态与工具调用不一致。")
        ids = [call.call_id for call in response.tool_calls]
        if len(ids) != len(set(ids)) or seen_ids.intersection(ids):
            raise ProviderError("模型重复使用了工具调用 ID，已在执行前停止。")
        seen_ids.update(ids)
        step.assistant = response.as_message()
        step.tools = [ToolExecution(call.call_id) for call in step.assistant.tool_calls]

    async def _execute_tools(
        self, step: AgentStep, context: ToolContext, on_event: AgentEventSink | None,
        on_checkpoint: AgentCheckpointSink | None,
    ) -> None:
        """连续只读调用最多四项并发；修改／控制调用是前后都结算的边界。"""
        pairs = list(zip(step.assistant.tool_calls, step.tools))
        index = 0
        while index < len(pairs):
            call, execution = pairs[index]
            if self._tools.execution_class(call.name) != "read":
                await self._execute_tool(step.step, call, execution, context, on_event, on_checkpoint)
                index += 1
                continue
            end = index
            while end < len(pairs) and end - index < 4 and self._tools.execution_class(pairs[end][0].name) == "read":
                end += 1
            tasks = [asyncio.create_task(self._execute_tool(step.step, call, execution, context, on_event, on_checkpoint))
                     for call, execution in pairs[index:end]]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                # 等同批读取释放连接／线程后再交回运行边界，不遗留后台读取。
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            index = end

    async def _execute_tool(self, number, call, execution, context, on_event, on_checkpoint):
        """检查点和界面按调用ID更新；模型历史仍按原tools数组顺序读取。"""
        await asyncio.sleep(0)

        scheduled_at, entered_at = monotonic(), None
        def entering_handler() -> None:
            nonlocal entered_at
            if on_checkpoint is not None:
                on_checkpoint(AgentCheckpoint("tool_started", number, {"call_id": call.call_id}))
            execution.handler_started = True
            execution.status = "running"
            self._notify(on_event, AgentEvent("tool_started", number, call=call))
            entered_at = monotonic()

        failure = None
        try:
            execution.result = await self._tools.execute(call, context, on_execute=entering_handler)
        except ToolExecutionUnknown as exc:
            execution.result = exc.tool_result
            failure = exc
        except asyncio.CancelledError as exc:
            execution.status = "interrupted" if execution.handler_started else "not_executed"
            execution.result = getattr(exc, "tool_result", None) or error_result(
                call, "cancelled", "运行取消；文件操作的实际状态请看收据。", details=getattr(exc, "tool_details", {}))
            raise
        finally:
            finished_at = monotonic()
            execution.elapsed_seconds = max(0.0, finished_at - scheduled_at)
            execution.handler_seconds = max(0.0, finished_at - entered_at) if entered_at is not None else None
        execution.status = (
            "interrupted" if failure is not None else "succeeded" if not execution.result.is_error
            else "failed" if execution.handler_started else "rejected"
        )
        if on_checkpoint is not None:
            on_checkpoint(AgentCheckpoint("tool_finished", number, asdict(execution)))
        self._notify(on_event, AgentEvent("tool_finished", number, call=call, result=execution.result))
        if failure is not None:
            raise failure
        await asyncio.sleep(0)

    @staticmethod
    def _settle_pending(step: AgentStep, code: str, message: str) -> None:
        """只补齐尚无结果的调用，不改写已经成功或失败的事实。"""
        if step.assistant is None:
            return
        for call, execution in zip(step.assistant.tool_calls, step.tools):
            if execution.result is not None:
                continue
            if execution.handler_started:
                execution.status = "interrupted"
                detail = "执行已进入处理函数，但运行中断，未确认最终结果。"
            else:
                execution.status, detail = "not_executed", message
            execution.result = error_result(call, code, detail)

    @staticmethod
    def _notify(sink: AgentEventSink | None, event: AgentEvent) -> None:
        if sink is not None:
            # 通知接收者不能通过修改调用或结果对象，改变正在执行的历史。
            sink(deepcopy(event))
