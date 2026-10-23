"""模型试运行与 Agent 入口：从项目根目录执行 python -m desktop.cli。"""
import argparse
import asyncio
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from .agent_cli import run_agent_cli, print_request_record
from .bootstrap import create_runtime
from .tools.catalog import default_tool_names
from .credentials import resolve_api_key
from .attachments.store import ImageStore
from .llm.messages import ImageContent, user_message
from .conversation_cli import run_compaction_cli, run_conversation_cli
from .llm.models import API_STYLES, ModelConfig, ModelRequest, ModelResponse
from .llm.diagnostics import RequestTraceContext
from .storage.request_records import RequestRecordStore
from .llm.reasoning import EFFORT_LEVELS, REASONING_ADAPTERS, ReasoningProfile
from .llm.providers.http import ProviderError
from .prompts import load_prompt_config
from .runtime.registry import RegistryError
from .llm.validation import parse_max_output_tokens, parse_temperature, resolve_model_config, validate_prompt
from .runtime.agent.models import AgentLimits, AgentResult
from .runtime.conversations import CompactionRecord, ConversationSettings, ConversationStore
from .runtime.context.budget import load_context_policy
from .runtime.application import Runtime
from .tools.command.shells import ShellConfig
from .tools.filesystem.search_config import SEARCH_TOOLS, SearchConfig
from .settings import load_model_catalog
from .storage.paths import LocalPaths


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Desktop 模型试运行与 Agent 循环")
    parser.add_argument("--endpoint", help="直接连接的完整 API URL；Gemini 使用含 {model} 的 :generateContent 地址")
    parser.add_argument("--api-style", choices=API_STYLES, help="直接连接的接口协议；默认 Chat Completions")
    parser.add_argument("--model", help="直接连接的实际模型名称；须与 --endpoint 同时提供")
    parser.add_argument("--provider", help="直接连接的 adapters.json 提供商ID；默认 compatible")
    parser.add_argument("--model-entry", help="使用已保存模型条目的稳定 ID，不是 API 模型名称")
    parser.add_argument("--settings-file", type=Path, help="用户模型目录；省略条目 ID 时使用默认条目")
    parser.add_argument("--image-input", choices=("unknown", "text", "image"), help="直接连接的输入能力；默认 unknown，只有 image 接收图片")
    parser.add_argument("--reasoning-adapter", choices=REASONING_ADAPTERS, help="直接连接的已确认推理方式，与 --effort-map 配合")
    parser.add_argument("--effort-map", help='直接连接的档位映射 JSON，例如 {"high":"high","max":"max"}')
    parser.add_argument("--tool-efforts", help='直接连接的工具可用档位 JSON，例如 ["low","high"]；留空不加额外限制')
    parser.add_argument("--effort", choices=EFFORT_LEVELS, help="本次推理档位；未设置时不发送。新会话保存，继续会话沿用原值")
    parser.add_argument("--image", action="append", type=Path, default=[], help="用户明确选择的本地图片，可重复；不与 request-file／compact 混用")
    parser.add_argument("--attachment-dir", type=Path, help="持久附件目录；继续会话时须使用原目录，默认 desktop/local/attachments")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt", help="本次用户消息")
    source.add_argument("--request-file", help="统一 ModelRequest JSON 文件；trial 仅检查协议，agent 使用本地工具声明")
    source.add_argument("--compact", action="store_true", help="仅手动压缩既有会话，不发送新任务；用于 conversation 模式")
    parser.add_argument("--system", default="", help="可选的系统提示词")
    parser.add_argument("--temperature", type=float, help="仅在模型支持时设置；默认不发送")
    parser.add_argument("--max-output-tokens", type=int, help="可选正整数；Messages 留空时使用 4096")
    parser.add_argument("--timeout", type=float, help="直接连接的连续读取等待上限，默认 180 秒，不是总生成时间")
    parser.add_argument("--connect-timeout", type=float, help="直接连接的建立连接等待上限，默认 30 秒")
    parser.add_argument("--max-attempts", type=int, help="直接连接的最多联网次数，含首次；默认 5")
    parser.add_argument("--action", default="chat", help="插件动作：chat 或 summarize")
    parser.add_argument("--key-env", help="直接连接的密钥环境变量名称；默认 MODEL_API_KEY")
    parser.add_argument("--mode", choices=("trial", "agent", "conversation"), default="trial", help="trial 单次调用；agent 单轮工具循环；conversation 连续会话")
    parser.add_argument("--workspace", type=Path, help="Agent 或新建会话的工作目录；继续会话沿用记录")
    parser.add_argument("--tool", action="append", dest="tool_names", help="Agent 可用工具，可重复指定；默认三个已登记的只读文件工具")
    parser.add_argument("--shell", choices=("powershell", "git_bash"), help="应用选择命令解释器；开放 run_command 时默认为 powershell")
    parser.add_argument("--shell-path", type=Path, help="已有 Shell 的可执行文件路径，不下载或安装")
    parser.add_argument("--command-timeout", type=float, help="整条命令运行上限（秒），默认 120；与模型网络等待分开")
    parser.add_argument("--search-backend", choices=("auto", "python", "rg"), help="搜索后端；新任务默认 auto，在开始前确定，不下载工具")
    parser.add_argument("--rg-path", type=Path, help="已有 rg 可执行文件路径，与 PowerShell／Git Bash 选择独立")
    parser.add_argument("--search-timeout", type=float, help="rg 整次搜索时限（秒），默认 30；不改变 Shell 或模型超时")
    parser.add_argument("--prompt-config", type=Path, help="提示词配置 JSON：version/system/context/application；同名覆盖，空字符串关闭")
    parser.add_argument("--prompt-mode", choices=("dialogue", "work"), help="纯对话不注入工作规则和文件工具；新建默认为 work，继续沿用快照")
    parser.add_argument("--max-steps", type=int, help="Agent 模型步骤上限，重试另计，默认 8")
    parser.add_argument("--max-tool-calls", type=int, help="Agent 工具调用尝试上限，默认 16")
    parser.add_argument("--record-dir", type=Path, help="Agent 记录目录，默认 desktop/local/agent-runs")
    parser.add_argument("--record-requests", action="store_true", help="按需保留本次实际API请求全文（含图片编码）；不保存鉴权头")
    parser.add_argument("--conversation", dest="conversation_id", help="conversation 模式继续此 UUID 会话；省略则新建")
    parser.add_argument("--conversation-dir", type=Path, help="会话目录，默认 desktop/local/conversations")
    parser.add_argument("--context-window", type=int, help="新会话的模型窗口；显式设置后启用请求前自动压缩，token 数为粗估")
    parser.add_argument("--context-config", type=Path, help="新会话的 ContextPolicy JSON：窗口、预留、保留比例和摘要指令")
    parser.add_argument("--stream", action="store_true", help="Agent 模式逐段输出文本；工具仍等待完整响应后执行")
    return parser


def _read_model_config(args: argparse.Namespace) -> ModelConfig:
    """目录选择与直接连接分开，避免半份旧配置与半份覆盖参数混成一个模型。"""
    if args.model_entry is not None or args.settings_file is not None:
        direct = (
            args.endpoint, args.model, args.api_style, args.provider, args.image_input,
            args.key_env, args.timeout, args.connect_timeout, args.max_attempts,
            args.reasoning_adapter, args.effort_map, args.tool_efforts,
        )
        if any(value is not None for value in direct):
            raise ValueError("--model-entry／--settings-file 不与直接连接或能力参数混用；生成参数 --effort 可单独选择。")
        path = args.settings_file if args.settings_file is not None else LocalPaths.default().settings
        entry = load_model_catalog(path).selected(args.model_entry)
        if entry is None:
            raise ValueError("模型目录为空，请先保存模型配置。")
        return entry.config
    if args.endpoint is None or args.model is None:
        raise ValueError("请选择已保存模型，或同时提供 --endpoint 与 --model。")
    profile = None
    if args.reasoning_adapter is None:
        if args.effort_map is not None or args.tool_efforts is not None:
            raise ValueError("档位映射需要 --reasoning-adapter。")
    else:
        try:
            mapping = json.loads(args.effort_map) if args.effort_map is not None else {}
            tools = json.loads(args.tool_efforts) if args.tool_efforts is not None else None
        except ValueError as exc:
            raise ValueError("--effort-map 须为 JSON 对象，--tool-efforts 须为 JSON 数组。") from exc
        profile = ReasoningProfile(args.reasoning_adapter, mapping, tools)
    return ModelConfig(
        endpoint_url=args.endpoint, model=args.model,
        api_style=args.api_style or "chat_completions",
        provider_kind=args.provider if args.provider is not None else "compatible",
        key_env=args.key_env if args.key_env is not None else "MODEL_API_KEY",
        timeout_seconds=args.timeout if args.timeout is not None else 180.0,
        connect_timeout_seconds=args.connect_timeout if args.connect_timeout is not None else 30.0,
        max_attempts=args.max_attempts if args.max_attempts is not None else 5,
        image_input={"unknown": None, "text": False, "image": True}[args.image_input or "unknown"],
        reasoning=profile,
    )


async def _run(args: argparse.Namespace, api_key: str, config: ModelConfig | None = None) -> ModelResponse | AgentResult | CompactionRecord:
    """将命令行输入转为配置和业务输入，再调用平台入口。"""
    config = _resolve_run_config(args, _read_model_config(args)) if config is None else config
    attachments = ImageStore(args.attachment_dir) if args.attachment_dir is not None else ImageStore()
    runtime = create_runtime(config, api_key, attachments=attachments)
    if args.mode == "trial" and (
        args.workspace is not None or args.tool_names is not None or args.max_steps is not None
        or args.max_tool_calls is not None or args.record_dir is not None or args.stream
        or args.prompt_config is not None or args.conversation_id is not None or args.conversation_dir is not None
        or args.prompt_mode is not None
        or args.shell is not None or args.shell_path is not None or args.command_timeout is not None
        or _has_search_options(args)
    ):
        raise ValueError("工作目录、工具、提示词配置、运行上限、记录目录和流式选项用于 agent／conversation 模式。")
    if args.mode != "conversation" and (
        args.conversation_id is not None or args.conversation_dir is not None or args.compact
        or args.context_window is not None or args.context_config is not None
    ):
        raise ValueError("会话选项仅用于 --mode conversation。")
    if args.mode == "conversation":
        images = await _import_images(args, config, attachments)
        return await _run_conversation(args, runtime, config, images)
    payload = _read_payload(args)
    images = await _import_images(args, config, attachments)
    if images:
        payload["images"] = [asdict(part) for part in images]
    if args.mode == "agent":
        prompt_mode = args.prompt_mode or "work"
        if args.action != "chat" or (prompt_mode == "work" and args.workspace is None):
            raise ValueError("工作模式需要 --workspace，且不与试运行的 --action 混用。")
        if prompt_mode == "dialogue" and (args.workspace is not None or args.tool_names is not None or _has_shell_options(args) or _has_search_options(args)):
            raise ValueError("纯对话模式不接收工作目录或文件工具。")
        request = _agent_request(args, payload)
        defaults = AgentLimits()
        tool_names = [] if prompt_mode == "dialogue" else args.tool_names if args.tool_names is not None else list(default_tool_names("cli_work"))
        return await run_agent_cli(
            runtime, config, request, workspace=args.workspace,
            tool_names=tool_names,
            limits=AgentLimits(
                args.max_steps if args.max_steps is not None else defaults.max_steps,
                args.max_tool_calls if args.max_tool_calls is not None else defaults.max_tool_calls,
            ),
            record_dir=args.record_dir if args.record_dir is not None else LocalPaths.default().agent_runs,
            stream=args.stream,
            prompt_config=load_prompt_config(args.prompt_config),
            prompt_mode=prompt_mode,
            shell_config=_shell_config(args),
            search_config=_search_config(args, tool_names),
            record_requests=args.record_requests,
        )
    action = "inspect_request" if args.request_file is not None else args.action
    async with RequestRecordStore(LocalPaths.default().request_records).recording(args.record_requests,
        RequestTraceContext(uuid4().hex, purpose="plugin", plugin_id="model_trial", action=action),
        api_style=config.api_style, model=config.model, prompt_source={"origin": "cli_trial"},
        on_notice=print_request_record) as traces:
        return await runtime.run_action("model_trial", action, payload, request_traces=traces)


def _resolve_run_config(args: argparse.Namespace, config: ModelConfig) -> ModelConfig:
    """新运行读取当前目录；继续会话先取原规则，不用新目录重解释历史。"""
    if args.mode == "conversation" and args.conversation_id is not None:
        directory = args.conversation_dir if args.conversation_dir is not None else LocalPaths.default().conversations
        saved = ConversationStore(directory).load_settings(args.conversation_id).config
        if (saved.api_style, saved.endpoint_url, saved.model, saved.provider_kind) != (
            config.api_style, config.endpoint_url, config.model, config.provider_kind,
        ):
            raise ValueError("继续会话须使用原协议、地址、模型和提供商；切换连接请新建会话。")
        from_catalog = args.model_entry is not None or args.settings_file is not None
        if not from_catalog and config.image_input != saved.image_input:
            raise ValueError("直接连接继续会话须使用原图片能力。")
        if args.reasoning_adapter is not None or args.effort_map is not None or args.tool_efforts is not None:
            raise ValueError("继续会话沿用原推理能力，不能重新指定能力参数。")
        config = replace(
            config, provider_rules=saved.provider_rules, reasoning=saved.reasoning,
            context_window=saved.context_window, image_input=saved.image_input,
        )
    return resolve_model_config(config)


async def _run_conversation(
    args: argparse.Namespace, runtime: Runtime, config: ModelConfig, images: list[ImageContent],
) -> AgentResult | CompactionRecord:
    """创建时保存参数；继续时沿用记录，不静默读取已变化的默认提示词。"""
    if args.request_file is not None or args.action != "chat":
        raise ValueError("连续会话接收 --prompt；历史由会话模块管理，不与 --request-file 或 --action 混用。")
    if not args.compact:
        user_message(args.prompt, images)
    prompt_mode = args.prompt_mode or "work"
    tools = [] if prompt_mode == "dialogue" else args.tool_names if args.tool_names is not None else list(default_tool_names("cli_work"))
    defaults = AgentLimits()
    overrides = {
        key: value for key, value in (("max_steps", args.max_steps), ("max_tool_calls", args.max_tool_calls))
        if value is not None
    }
    settings = None
    if args.compact and args.conversation_id is None:
        raise ValueError("--compact 需要指定既有会话的 --conversation 编号。")
    if args.conversation_id is None:
        if prompt_mode == "work" and args.workspace is None:
            raise ValueError("新建会话需要 --workspace。")
        if prompt_mode == "dialogue" and (args.workspace is not None or args.tool_names is not None or _has_shell_options(args) or _has_search_options(args)):
            raise ValueError("纯对话会话不接收工作目录或文件工具。")
        settings = ConversationSettings(
            config, str(args.workspace.expanduser().resolve(strict=True)) if args.workspace is not None else None, tools,
            load_prompt_config(args.prompt_config), system=args.system,
            temperature=args.temperature, max_output_tokens=args.max_output_tokens,
            reasoning_effort=args.effort,
            limits=AgentLimits(**{**asdict(defaults), **overrides}),
            context_policy=load_context_policy(args.context_config, context_window=args.context_window),
            prompt_mode=prompt_mode,
            shell_config=_shell_config(args),
            search_config=_search_config(args, tools),
        )
    elif (
        args.workspace is not None or args.system or args.prompt_config is not None
        or args.temperature is not None or args.max_output_tokens is not None or args.effort is not None
        or args.context_window is not None or args.context_config is not None
        or args.prompt_mode is not None
        or _has_shell_options(args)
        or _has_search_options(args)
    ):
        raise ValueError("继续会话沿用原模式、目录、提示词、Shell、搜索后端、生成参数和上下文配置；修改这些设置请新建会话。")
    conversation_dir = args.conversation_dir if args.conversation_dir is not None else LocalPaths.default().conversations
    if args.compact:
        if overrides or args.stream:
            raise ValueError("手动压缩不执行 Agent 步骤，不与运行上限或 --stream 混用。")
        return await run_compaction_cli(
            runtime, config, args.conversation_id, tool_names=tools, conversation_dir=conversation_dir,
            record_requests=args.record_requests,
        )
    return await run_conversation_cli(
        runtime, config, args.prompt, conversation_id=args.conversation_id, settings=settings,
        tool_names=tools, limit_overrides=overrides,
        conversation_dir=conversation_dir,
        record_dir=args.record_dir if args.record_dir is not None else LocalPaths.default().agent_runs,
        stream=args.stream,
        images=images,
        record_requests=args.record_requests,
    )


async def _import_images(args: argparse.Namespace, config: ModelConfig, store: ImageStore) -> list[ImageContent]:
    """只接收命令行明确选中的本地文件，不从模型消息推导文件权限。"""
    if args.image and (args.request_file is not None or args.compact):
        raise ValueError("--image 只用于本次 --prompt；请求文件使用已存在的附件引用。")
    if args.image and config.image_input is not True:
        raise ValueError("选择图片前请确认模型条目的视觉能力；直接连接使用 --image-input image。")
    return [info.as_content() for info in await store.import_files(args.image)] if args.image else []


def _has_shell_options(args: argparse.Namespace) -> bool:
    return args.shell is not None or args.shell_path is not None or args.command_timeout is not None


def _has_search_options(args: argparse.Namespace) -> bool:
    return args.search_backend is not None or args.rg_path is not None or args.search_timeout is not None


def _search_config(args: argparse.Namespace, tool_names: list[str]) -> SearchConfig | None:
    """只读搜索不要求开放 Shell；模型不能通过工具参数选择后端。"""
    if not SEARCH_TOOLS.intersection(tool_names):
        if _has_search_options(args):
            raise ValueError("搜索配置需要开放 find_files 或 search_text。")
        return None
    return SearchConfig(
        backend=args.search_backend or "auto",
        executable=str(args.rg_path.expanduser().resolve(strict=True)) if args.rg_path is not None else None,
        timeout_seconds=args.search_timeout if args.search_timeout is not None else 30.0,
    )


def _shell_config(args: argparse.Namespace) -> ShellConfig | None:
    """--tool 是应用授权入口；Shell 参数本身不能自动开放执行能力。"""
    if "run_command" not in (args.tool_names or []):
        if _has_shell_options(args):
            raise ValueError("Shell 配置需要显式指定 --tool run_command。")
        return None
    return ShellConfig(
        kind=args.shell or "powershell",
        executable=str(args.shell_path.expanduser().resolve(strict=True)) if args.shell_path is not None else None,
        timeout_seconds=args.command_timeout if args.command_timeout is not None else 120.0,
    )


def _read_payload(args: argparse.Namespace) -> dict[str, Any]:
    """读取文件或提示词输入，保持两种输入来源互斥。"""
    if args.request_file is not None:
        if args.system or args.temperature is not None or args.max_output_tokens is not None or args.effort is not None or args.action != "chat":
            raise ValueError("--request-file 的消息与参数由文件提供，不与提示词参数或 --action 混用。")
        try:
            payload = json.loads(Path(args.request_file).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("无法读取请求文件，请提供有效的 UTF-8 JSON 文件。") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求文件顶层必须是 JSON 对象。")
        return payload
    if not args.image:
        validate_prompt(args.prompt)
    return {
        "prompt": args.prompt, "system": args.system, "temperature": parse_temperature(args.temperature),
        "max_output_tokens": parse_max_output_tokens(args.max_output_tokens),
        "reasoning_effort": args.effort,
    }


def _agent_request(args: argparse.Namespace, payload: dict[str, Any]) -> ModelRequest:
    """Agent 使用统一消息对象；试运行动作仍接收原来的业务输入。"""
    if args.request_file is not None:
        try:
            return ModelRequest(**payload)
        except TypeError as exc:
            raise ValueError("请求文件字段应为 messages、tools、temperature、max_output_tokens、reasoning_effort。") from exc
    messages = [{"role": "system", "content": args.system}] if args.system else []
    messages.append(user_message(args.prompt, [ImageContent(part["attachment_id"]) for part in payload.get("images", [])]))
    return ModelRequest(messages, payload["temperature"], payload["max_output_tokens"], reasoning_effort=payload["reasoning_effort"])


def main() -> int:
    args = _parser().parse_args()
    try:
        config = _resolve_run_config(args, _read_model_config(args))
    except (OSError, ValueError) as exc:
        print(f"配置失败：{exc}", file=sys.stderr)
        return 1
    try:
        api_key = resolve_api_key(config)
    except (OSError, ValueError) as exc:
        print(f"密钥读取失败：{exc}", file=sys.stderr)
        return 1

    try:
        result = asyncio.run(_run(args, api_key, config))
    except (ProviderError, RegistryError, OSError, ValueError) as exc:
        print(f"调用失败：{exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("已中断本次调用", file=sys.stderr)
        return 130

    if isinstance(result, AgentResult):
        if not args.stream:
            print(result.text)
        else:
            print()
        print(json.dumps({
            "text": result.text, "stop_reason": result.stop_reason,
            "model_steps": len(result.steps), "tool_attempts": result.tool_attempts,
            "http_attempts": result.http_attempts,
            "error": result.error,
            "usage_summary": result.usage_summary,
            "compaction_usage_summary": result.compaction_usage_summary,
            "total_usage_summary": result.total_usage_summary,
            "request_tokens_estimate": result.request_tokens_estimate,
        }, ensure_ascii=False))
        if result.stop_reason != "completed":
            print(f"Agent 未完成任务：{result.stop_reason}。", file=sys.stderr)
        return 0 if result.stop_reason == "completed" else 1
    if isinstance(result, CompactionRecord):
        print(json.dumps(asdict(result), ensure_ascii=False))
        return 0 if result.status == "succeeded" else 1
    print(result.text)
    details = {
        "finish_reason": result.finish_reason, "completion_status": result.completion_status,
        "usage": result.usage, "tool_calls": [asdict(call) for call in result.tool_calls],
    }
    if args.request_file is not None and result.completion_status in ("completed", "requires_tool"):
        details["assistant_message"] = asdict(result.as_message())
    print(json.dumps(details, ensure_ascii=False))
    if result.completion_status == "incomplete":
        print("提示：已返回文本，但生成未完成；请查看结束原因", file=sys.stderr)
    elif result.completion_status == "unknown":
        print("提示：服务未提供可确认的完成状态，请人工检查文本", file=sys.stderr)
    elif result.completion_status == "requires_tool":
        print("提示：模型请求工具结果；本入口未执行工具。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
