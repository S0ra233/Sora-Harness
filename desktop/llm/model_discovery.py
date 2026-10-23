"""按现有协议读取模型目录；不发生成请求，不修改本地配置。"""
import asyncio
from dataclasses import dataclass
import json
from urllib.parse import urlsplit, urlunsplit

import httpx

from .rules import API_STYLES


@dataclass(frozen=True)
class ModelListSource:
    api_url: str
    api_style: str
    url_mode: str = "base"
    credential_ref: str | None = None
    key_env: str = "MODEL_API_KEY"


@dataclass(frozen=True)
class RemoteModel:
    model: str
    label: str


def models_url(source: ModelListSource) -> str:
    """从明确的基础地址或已知生成路径定位同一接口的目录。"""
    if source.api_style not in API_STYLES or source.url_mode not in {"base", "endpoint"}:
        raise ValueError("请选择已支持的 API 协议和地址类型。")
    parsed = urlsplit(source.api_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("请填写有效的 HTTP／HTTPS API 地址，地址中不能包含账号密码。")
    path = parsed.path.rstrip("/")
    if source.api_style == "gemini":
        if "/models/" in path and path.endswith((":generateContent", ":streamGenerateContent")):
            path = path.rsplit("/models/", 1)[0]
        elif source.url_mode == "endpoint":
            raise ValueError("无法从此完整地址确定模型目录，请使用 API 基础地址。")
        if not path.endswith(("/v1", "/v1beta")):
            path += "/v1beta"
    else:
        suffix = {"chat_completions": "/chat/completions", "responses": "/responses",
                  "anthropic_messages": "/messages"}[source.api_style]
        if path.endswith(suffix):
            path = path[:-len(suffix)]
        elif source.url_mode == "endpoint":
            raise ValueError("无法从此完整地址确定模型目录，请使用 API 基础地址。")
        if source.api_style == "anthropic_messages" and not path.endswith("/v1"):
            path += "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/models", parsed.query, ""))


async def _fetch_models(source: ModelListSource, key: str) -> tuple[RemoteModel, ...]:
    """一次点击使用一个可取消客户端；分页出错时不发布不完整目录。"""
    url = models_url(source)
    headers = {"Accept": "application/json"}
    gemini = source.api_style == "gemini"
    anthropic = source.api_style == "anthropic_messages"
    if anthropic:
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif gemini:
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = "Bearer " + key
    params = {"pageSize": "1000"} if gemini else {"limit": "1000"} if anthropic else {}
    models: dict[str, RemoteModel] = {}
    seen_pages: set[str] = set()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), follow_redirects=False) as client:
            while True:
                target = httpx.URL(url).copy_merge_params(params)
                async with client.stream("GET", target, headers=headers) as response:
                    if not response.is_success:
                        raise ValueError(f"模型目录请求失败（HTTP {response.status_code}），请检查地址、密钥或接口是否提供模型列表。")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2 * 1024 * 1024:
                            raise ValueError("模型目录响应过大；请手动添加需要的模型。")
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError):
                    raise ValueError("模型目录没有返回有效 JSON。") from None
                entries = data.get("models" if gemini else "data") if isinstance(data, dict) else None
                if not isinstance(entries, list):
                    raise ValueError("模型目录字段格式不符合所选协议；可继续手动添加模型。")
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError("模型目录包含无效条目。")
                    if gemini:
                        methods = entry.get("supportedGenerationMethods", [])
                        if not isinstance(methods, list):
                            raise ValueError("模型目录中的生成方法字段无效。")
                        if "generateContent" not in methods:
                            continue
                    name = entry.get("name" if gemini else "id")
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError("模型目录包含缺少 ID 的条目。")
                    name = name.strip().removeprefix("models/") if gemini else name.strip()
                    if not name:
                        raise ValueError("模型目录包含空的模型 ID。")
                    label = entry.get("displayName" if gemini else "display_name") or name
                    models.setdefault(name, RemoteModel(name, label if isinstance(label, str) else name))
                if len(models) > 10000:
                    raise ValueError("模型目录超过读取范围；请手动添加需要的模型。")
                token = data.get("nextPageToken") if gemini else data.get("last_id") if anthropic and data.get("has_more") else None
                if anthropic and data.get("has_more") and not token:
                    raise ValueError("模型目录缺少下一页标记。")
                if token is None or token == "":
                    break
                if not isinstance(token, str) or token in seen_pages:
                    raise ValueError("模型目录分页返回无效或重复的标记。")
                seen_pages.add(token)
                if len(seen_pages) >= 100:
                    raise ValueError("模型目录超过读取范围；请手动添加需要的模型。")
                params["pageToken" if gemini else "after_id"] = token
    except httpx.HTTPError:
        # 不展示URL、响应正文或底层异常，避免接口回显密钥。
        raise ValueError("模型目录连接失败或超时，请检查接口后重试。") from None
    return tuple(sorted(models.values(), key=lambda model: model.model.casefold()))


async def discover_models(source: ModelListSource, key: str) -> tuple[RemoteModel, ...]:
    """限制完整目录操作时长，兼容项目既有Python版本；取消仍原样传播。"""
    try:
        return await asyncio.wait_for(_fetch_models(source, key), timeout=60)
    except asyncio.TimeoutError:
        raise ValueError("读取完整模型目录超时，请稍后重试或手动填写。") from None
