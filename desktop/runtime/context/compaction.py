"""通用摘要与请求视图替换；只覆盖已结束轮次，不执行历史工具。"""
from __future__ import annotations

import asyncio
import json
import math
from dataclasses import asdict, replace
from time import monotonic
from uuid import uuid4
from typing import TYPE_CHECKING

from ...llm.contracts import ModelBackend
from ...llm.diagnostics import RequestTraceFactory, call_observed_model
from ...llm.messages import ImageContent, Message, ModelMessage, TextContent, ToolResult
from ...llm.models import ModelConfig, ModelRequest
from ...llm.model_catalog import ModelTarget
from ...llm.history_projection import project_request
from ...llm.reasoning import resolve_effort
from ...llm.providers.http import ProviderError
from ...llm.usage import normalize_usage
from ...llm.attempts import ModelAttemptEvent
from ...prompts.assembly import build_prompt_prefix, runtime_environment, environment_update
from ..agent.models import AgentEvent, AgentEventSink
from .budget import effective_window, estimate_message_tokens, estimate_request_tokens, RequestTokenEstimator

if TYPE_CHECKING:
    from ..conversations.models import CompactionRecord
    from ..conversations.store import ConversationJournal


# 旧日志保留自有指令；本次的选区和长度约束在请求末尾统一补充。
DEFAULT_SUMMARY_INSTRUCTIONS = "为后续继续对话生成简洁摘要，保留必要事实、最新约定和未完成事项；区分事实与推测。"


class CompactionError(ProviderError):
    """预算或摘要失败是可解释的运行错误，不自动重发请求。"""


def _visible_transcript(messages: list[Message]) -> str:
    """摘要输入是待分析资料，不是原生续接请求；不拆写 thinking 签名。"""
    transcript = []
    for message in messages:
        if isinstance(message, ToolResult):
            record = asdict(message)
            try:
                record["content"] = json.loads(message.content)
            except (ValueError, RecursionError):
                pass
            transcript.append(record)
        else:
            transcript.append({
                "role": message.role, "text": message.text,
                "tool_calls": [asdict(call) for call in message.tool_calls],
                "images": [asdict(part) for part in message.content if isinstance(part, ImageContent)],
            })
    return json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))


class ConversationCompactor:
    """一轮运行的预算处理器；循环中的摘要与正常请求共用已有模型连接。"""

    def __init__(self, journal: ConversationJournal, api_style: str, on_event: AgentEventSink | None = None,
                 *, request_traces: RequestTraceFactory | None = None, config: ModelConfig | None = None,
                 model_target: ModelTarget | None = None, prompt_config=None) -> None:
        self.journal, self.api_style, self.on_event = journal, api_style, on_event
        self.request_traces = request_traces
        settings = journal.conversation.settings
        self.config = config if config is not None else settings.config
        active = journal.conversation.active_turn
        if model_target is None and active is not None and active.options is not None:
            model_target = active.options.model_target
        self.model_target = model_target or ModelTarget(self.config)
        if self.model_target.config != self.config:
            raise ValueError("摘要目标与模型后端不一致。")
        self.policy = replace(settings.context_policy, context_window=effective_window(
            self.config.context_window, settings.context_policy.context_window))
        # 与请求装配共用配置和构造函数，前置对话／固定参考资料不算真实历史。
        system_messages = [ModelMessage("system", settings.system)] if settings.system else []
        self.history_start = len(build_prompt_prefix(
            prompt_config or (active.options.prompt_config if active is not None and active.options is not None and active.options.prompt_config is not None
                              else journal.conversation.current_prompt),
            prompt_mode=settings.prompt_mode, system_messages=system_messages,
        ))
        # 会话先保存初始环境再创建压缩器；当前未结束轮次永远不进入可压缩范围。
        self.turns = tuple(turn for turn in journal.conversation.turns if turn.finished_at is not None)
        self.estimates: list[int] = []
        self._estimate = RequestTokenEstimator()
        self.input_estimate_scale = 1.0

    def _input_scale(self, request: ModelRequest) -> float:
        """同模型近期实测只校准压缩目标；窗口检查仍使用原保守估算。"""
        if any(isinstance(part, ImageContent) for message in request.messages if isinstance(message, ModelMessage)
               for part in message.content):
            return 1.0
        ratios = []
        for turn in reversed(self.turns):
            source = (turn.options.model_target.config if turn.options is not None and turn.options.model_target is not None
                      else self.journal.conversation.settings.config)
            if source != self.config or len(turn.request_tokens_estimate) != len(turn.steps):
                continue
            for index in range(len(turn.steps) - 1, -1, -1):
                step, estimate = turn.steps[index], turn.request_tokens_estimate[index]
                usage = step.token_usage
                if (step.completion_status not in {"completed", "requires_tool"} or usage is None
                        or type(usage.input_tokens) is not int or usage.input_tokens <= 0
                        or type(estimate) is not int or estimate <= 0):
                    continue
                ratios.append(usage.input_tokens / estimate)
                if len(ratios) == 3:
                    return max(ratios) * 1.2
        return max(ratios) * 1.2 if ratios else 1.0

    def _replacement_request(self, request: ModelRequest, start: int, end: int, summary: str) -> ModelRequest:
        """选区预算和最终提交共用替换构造，保留当前有效环境及完整尾部。"""
        from ..conversations.history import summary_message

        candidate = replace(request, messages=[*request.messages[:start], summary_message(summary), *request.messages[end:]])
        baseline = next((message for message in request.messages if isinstance(message, ModelMessage)
                         and message.runtime_context_kind == "baseline"
                         and message.runtime_context_id == request.runtime_context_id), None)
        if baseline is not None:
            update = environment_update(baseline, runtime_environment(baseline, request.messages), candidate.messages)
            if update is not None:
                candidate = replace(candidate, messages=[*candidate.messages, update],
                                    runtime_context_updates=[*candidate.runtime_context_updates, update])
        candidate, _ = project_request(candidate, self.config, legacy_changed=True)
        return candidate

    async def before_request(self, request: ModelRequest, backend: ModelBackend) -> ModelRequest:
        """每步测量实际准备请求；没有窗口时不自动触发，也不使用累计消耗。"""
        request, _ = project_request(request, self.config, legacy_changed=self.journal.conversation.latest_compaction is not None)
        estimate = self._estimate(request)
        threshold = self.policy.threshold(request.max_output_tokens)
        if threshold is not None:
            if threshold <= 0:
                raise CompactionError("输出预留超过可用窗口，请调整上下文预算。")
            if estimate >= threshold:
                request = await self.compact(request, backend, trigger="budget")
                estimate = self._estimate(request)
        self.estimates.append(estimate)
        return request

    def _select_prefix(self, request: ModelRequest) -> tuple[int, int, int] | None:
        # 会话包导出 Service，而 Service 使用本模块；执行时取投影函数避免初始化循环。
        from ..conversations.history import turn_messages

        previous = self.journal.conversation.latest_compaction
        covered = previous.covered_turns if previous else 0
        if len(self.turns) <= covered:
            return None
        start = self.history_start
        if (start < len(request.messages) and isinstance(request.messages[start], ModelMessage)
                and request.messages[start].runtime_context_kind == "baseline"
                and request.messages[start].runtime_context_id == request.runtime_context_id):
            start += 1
        boundaries = [start + (1 if previous else 0)]
        # Provider投影保持消息数量；直接测量已准备的发送视图，不逐轮重复转换原生块。
        for turn in self.turns[covered:]:
            boundaries.append(boundaries[-1] + len(turn_messages(turn, self.journal.conversation.inputs)))
        # 先计入system、tools、环境及当前Run，不能把一万当成额外历史额度。
        fixed = self._replacement_request(request, start, boundaries[-1], "")
        fixed_tokens = estimate_request_tokens(fixed)
        limit = self.policy.compacted_input_budget(request.max_output_tokens)
        retained, target = 0, len(self.turns)
        # 每次至少推进一个已结束轮次；只保留预算内的最近完整轮次。
        block_count = len(boundaries) - 1
        keep_count = min(self.policy.retain_turns, block_count - 1)
        for index in range(block_count - 1, block_count - keep_count - 1, -1):
            size = sum(estimate_message_tokens(message)
                       for message in request.messages[boundaries[index]:boundaries[index + 1]])
            expected = math.ceil((fixed_tokens + retained + size) * self.input_estimate_scale)
            if expected + self.policy.summary_soft_limit > limit:
                break
            retained += size
            target -= 1
        active = self.journal.conversation.active_turn
        if active is not None and active.resumed_from_run_id:
            # 暂停目标和它的续接链作为保护尾部，旧工具批次不能被摘要拆走。
            protected = {active.resumed_from_run_id}
            for index in range(len(self.turns) - 1, -1, -1):
                turn = self.turns[index]
                if turn.run_id in protected and index >= covered:
                    target = min(target, index)
                    if turn.resumed_from_run_id:
                        protected.add(turn.resumed_from_run_id)
            if target <= covered:
                return None
        # 暂停续接链不可拆；不能因保护尾部而悄悄超过本次压缩目标。
        tail_tokens = sum(estimate_message_tokens(message)
                          for message in request.messages[boundaries[target - covered]:boundaries[-1]])
        expected = math.ceil((fixed_tokens + tail_tokens) * self.input_estimate_scale)
        if expected + self.policy.summary_soft_limit > limit:
            raise CompactionError(
                f"固定前缀、当前任务或暂停续接链约占{expected}输入token，无法再容纳约"
                f"{self.policy.summary_target_tokens}token摘要并达到{limit}的总输入目标；"
                "未请求摘要、原历史保留，请精简预设／资料或完成当前任务后再压缩。")
        return start, boundaries[target - covered], target

    def _summary_request(self, original: ModelRequest, start: int, end: int) -> ModelRequest:
        instructions = self.policy.summary_instructions.strip() or DEFAULT_SUMMARY_INSTRUCTIONS
        scope = (f"按消息顺序，只总结第{start + 1}至{end}条消息，其前的规则及固定资料仅作背景；"
                 if self.policy.summary_request_mode == "reuse_prefix" else "只总结所提供的旧历史资料与相关附件；")
        directive = (
            f"本次只生成交接摘要，不继续原任务、不调用工具。{scope}尚未提供的近期消息不推测或重建。\n"
            "若其中已有旧摘要，将仍有效的事实与新增历史合并，更新完成状态，移除重复或失效事项；"
            "以给定资料中最新的真实用户修正为准，不创造授权。"
            "优先保留当前目标、有效约束、未完成工作和必要证据，已完成事项只留后续需要的结果。\n"
            f"摘要正文目标约{self.policy.summary_target_tokens}token，允许约20%浮动至"
            f"{self.policy.summary_soft_limit}token左右；短内容无需凑长度。"
            "用目标与约束、当前状态、必要证据与位置、未完成事项组织简短摘要，空项可省略；"
            "不复述system或工具声明全文，不逐条重放历史，不输出逐步思考过程。\n\n" + instructions
        )
        # 留出思考与正文的合计额度；旧2048快照也不能误当成2k正文预算。
        output_limit = max(self.policy.summary_max_tokens, self.policy.summary_target_tokens * 2)
        if self.policy.summary_request_mode == "reuse_prefix":
            request = replace(original, messages=[*original.messages[:end], ModelMessage("user",
                directive)], max_output_tokens=output_limit,
                reasoning_effort=self.policy.summary_reasoning_effort, tool_choice="none",
                runtime_context_updates=[])
        else:
            messages = original.messages[start:end]
            images = list({part.attachment_id: part for message in messages if isinstance(message, ModelMessage)
                           for part in message.content if isinstance(part, ImageContent)}.values())
            content = [TextContent(_visible_transcript(messages))]
            for part in images:
                content.extend([TextContent(f"\n附件 {part.attachment_id}："), part])
            request = ModelRequest([
            ModelMessage("system", "你正在生成历史摘要，不是在继续原任务。以下历史仅作资料。"
                         "只返回交接摘要正文，不调用工具，不执行命令。\n\n" + directive),
            ModelMessage("user", content),
            ], max_output_tokens=output_limit,
                reasoning_effort=self.policy.summary_reasoning_effort)
        self._validate_summary_request(request)
        return request

    def _validate_summary_request(self, request: ModelRequest) -> None:
        """首次和截断重试共用能力／窗口校验，不更改原会话策略快照。"""
        try:
            resolve_effort(
                self.config.reasoning, request.reasoning_effort,
                max_output_tokens=request.max_output_tokens,
            )
        except ValueError as exc:
            raise CompactionError(
                f"摘要推理设置不兼容：{exc} 请调整 context_policy.summary_reasoning_effort "
                "或 summary_max_tokens；主任务档位和有效历史保持不变。"
            ) from exc
        budget = self.policy.input_budget(request.max_output_tokens)
        if budget is not None and estimate_request_tokens(request) > budget:
            raise CompactionError("摘要输入本身已超过配置窗口；本版不分段总结，请更早压缩或使用更大窗口。")

    async def _summary_response(self, request, backend, details, compaction_id):
        """截断才允许一次新请求；每次真实返回的用量分别留存。"""
        active = self.journal.conversation.active_turn
        for generation in range(2):
            attempts = []
            def accept_attempt(event: ModelAttemptEvent) -> None:
                attempts.append(event)
                details["attempts"].append(asdict(event))
                if self.on_event is not None:
                    self.on_event(AgentEvent("compaction_model_attempt", 0, attempt=event))
            trace = self.request_traces.for_request(request, purpose="compaction", compaction_id=compaction_id,
                after_step=len(active.steps) if active else 0, owner_run_id=active.run_id if active else None) if self.request_traces else None
            response = await call_observed_model(backend, request, on_attempt=accept_attempt, trace=trace)
            usage = response.token_usage or normalize_usage(response.usage, self.api_style, self.config.provider_kind)
            received = next((event for event in reversed(attempts) if event.phase == "received"), None)
            details["summary_requests"].append({"request_id": received.request_id if received else None,
                "max_output_tokens": request.max_output_tokens, "token_usage": asdict(usage) if usage else None,
                "duration_seconds": received.duration_seconds if received else None,
                "completion_status": response.completion_status, "finish_reason": response.finish_reason})
            details.update(summary=response.text, usage=response.usage, token_usage=asdict(usage) if usage else None,
                           completion_status=response.completion_status, finish_reason=response.finish_reason)
            truncated = response.completion_status == "incomplete" and (response.finish_reason or "").casefold() in {
                "length", "max_tokens", "max_output_tokens"}
            if not truncated or response.tool_calls or generation or request.max_output_tokens >= 8192:
                return response
            retry = replace(request, max_output_tokens=8192)
            try:
                self._validate_summary_request(retry)
            except CompactionError as exc:
                raise CompactionError("摘要达到输出上限，无法在当前窗口或模型设置下提高至8192；原历史保留。 " + str(exc)) from exc
            request = retry
        return response

    async def compact(self, request: ModelRequest, backend: ModelBackend, *, trigger: str) -> ModelRequest:
        """生成后先校验，再持久提交替换；失败不改变已生效的摘要。"""
        request, _ = project_request(request, self.config, legacy_changed=self.journal.conversation.latest_compaction is not None)
        before = estimate_request_tokens(request)
        self.input_estimate_scale = self._input_scale(request)
        region = self._select_prefix(request)
        if region is None:
            budget = self.policy.input_budget(request.max_output_tokens)
            if trigger == "budget" and budget is not None and before <= budget:
                # 触发线是软阈值；没有可压缩旧轮次但尚在预算内时继续，不反复生成摘要。
                return request
            raise CompactionError("没有可压缩的已结束旧轮次；超过预算时请减少当前输入或调整窗口，不能靠重复摘要解决。")
        start, end, target = region
        source = request.messages[start:end]
        budget = self.policy.input_budget(request.max_output_tokens)
        protected = before - sum(estimate_message_tokens(message) for message in source)
        if budget is not None and protected >= budget:
            raise CompactionError("当前 Run 与固定上下文已用尽预算，摘要旧轮次也无法容纳；未请求摘要，请减少输入或调整窗口。")
        summary_request = self._summary_request(request, start, end)
        compaction_id = uuid4().hex
        self.journal.append("compaction_started", compaction_id, {
            "covered_turns": target, "through_run_id": self.turns[target - 1].run_id,
            "trigger": trigger, "before_tokens_estimate": before,
            "owner_run_id": self.journal.conversation.active_turn.run_id if self.journal.conversation.active_turn else None,
            "after_step": len(self.journal.conversation.active_turn.steps) if self.journal.conversation.active_turn else 0,
            "model_target": asdict(self.model_target),
            "input_target_tokens": self.policy.compacted_input_budget(request.max_output_tokens),
            "input_estimate_scale": self.input_estimate_scale,
            "retained_turns": len(self.turns) - target,
        })
        started = monotonic()
        details = {
            "status": "failed", "summary": "", "after_tokens_estimate": None,
            "summary_input_estimate": estimate_request_tokens(summary_request),
            "duration_seconds": None, "usage": None, "token_usage": None, "error": None,
            "completion_status": None, "finish_reason": None,
            "attempts": [], "summary_requests": [],
        }

        try:
            self._notify("compaction_started", compaction_id)
            # 声明可复用，但调用被禁止；这里从未创建工具执行循环或执行上下文。
            response = await self._summary_response(summary_request, backend, details, compaction_id)
            if response.tool_calls:
                raise CompactionError("摘要请求返回了工具调用，未执行工具，也未替换原历史。")
            if response.completion_status != "completed":
                reason = response.finish_reason or "unknown"
                raise CompactionError(f"摘要未完整结束（{reason}）；已返回文字未作为摘要采用，原历史保留。")
            if not response.text.strip():
                raise CompactionError("接口完整结束但没有返回摘要正文；原有效历史保持不变。")
            summary = response.text.strip()
            candidate = self._replacement_request(request, start, end, summary)
            after = estimate_request_tokens(candidate)
            details.update(summary=summary, after_tokens_estimate=after)
            if after >= before:
                raise CompactionError("摘要没有缩小请求，未提交历史替换；请调整摘要指令或输出上限。")
            if budget is not None and after > budget:
                raise CompactionError("压缩旧轮次后当前任务仍超过预算，未提交替换；请减少输入或调整预算。")
            estimated_input = math.ceil(after * self.input_estimate_scale)
            target_input = self.policy.compacted_input_budget(request.max_output_tokens)
            if estimated_input > target_input:
                raise CompactionError(
                    f"摘要完整，但压缩后的总输入估计为{estimated_input}token，超过{target_input}目标；"
                    "未裁短摘要、未丢弃未总结的原文，也未自动再次请求，原有效历史保留。")
            details["status"] = "succeeded"
        except (Exception, asyncio.CancelledError) as exc:
            details["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            details["error"] = str(exc) if isinstance(exc, ProviderError) else f"摘要请求停止（{type(exc).__name__}）。"
            self._finish(compaction_id, details, started)
            raise
        self._finish(compaction_id, details, started)
        return candidate

    def _finish(self, compaction_id: str, details: dict, started: float) -> None:
        details["duration_seconds"] = round(monotonic() - started, 6)
        self.journal.append("compaction_finished", compaction_id, details)
        self._notify("compaction_finished", compaction_id)

    def _notify(self, kind: str, compaction_id: str) -> None:
        if self.on_event is not None:
            record = self.journal.conversation.compactions[-1]
            self.on_event(AgentEvent(kind, 0, text=compaction_id, compaction=record.display_status()))


def recover_compaction(journal: ConversationJournal) -> None:
    """进程退出遗留的摘要不重做；只写中断终态，继续沿用上次成功视图。"""
    record: CompactionRecord | None = journal.conversation.active_compaction
    if record is not None:
        journal.append("compaction_finished", record.compaction_id, {
            "status": "interrupted", "summary": "", "error": "上次摘要没有持久终态，未采用其结果，也未重新请求模型。",
        })
