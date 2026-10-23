"""Embedding专用协议映射；不复用生成模型的messages或effort字段。"""
import asyncio
import math
import os
import re
from urllib.parse import quote

import httpx


class EmbeddingError(ValueError):
    """诊断不包含密钥、完整地址、语料正文或服务端错误回显。"""


def normalized(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 16384:
        raise EmbeddingError("接口返回的向量维度不正确。")
    if any(type(value) not in {int, float} for value in values):
        raise EmbeddingError("接口返回了非有限数值向量。")
    try:
        values = [float(value) for value in values]
    except OverflowError as exc:
        raise EmbeddingError("接口返回了溢出向量。") from exc
    if any(not math.isfinite(value) for value in values):
        raise EmbeddingError("接口返回了非有限数值向量。")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingError("接口返回了无效的零向量或溢出向量。")
    return [value / norm for value in values]


def _secret(value):
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
    if match:
        value = os.environ.get(match[1], "")
        if not value:
            raise EmbeddingError("Embedding密钥引用的环境变量未设置。")
    return value


class EmbeddingClient:
    def __init__(self, options):
        self.options = options.validate()
        if not options.enabled:
            raise EmbeddingError("Embedding尚未启用；关键词检索仍可使用。")
        self._client = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.options.timeout_seconds, follow_redirects=False)
        return self

    async def __aexit__(self, *_):
        await self._client.aclose()

    def _request(self, texts, purpose):
        config = self.options
        url, headers = config.url.rstrip("/"), {}
        key = _secret(config.api_key)
        if config.protocol == "openai":
            url = url if url.endswith("/embeddings") else url + "/embeddings"
            body = {"model": config.model, "input": texts, "encoding_format": "float"}
            if key:
                headers["Authorization"] = "Bearer " + key
            if config.dimensions is not None:
                body["dimensions"] = config.dimensions
        elif config.protocol == "ollama":
            url = url if url.endswith("/api/embed") else url + ("/embed" if url.endswith("/api") else "/api/embed")
            body = {"model": config.model, "input": texts, "truncate": False}
            if key:
                headers["Authorization"] = "Bearer " + key
            if config.dimensions is not None:
                body["dimensions"] = config.dimensions
        else:
            model = "models/" + config.model.removeprefix("models/")
            url += "/" + quote(model, safe="/") + ":batchEmbedContents"
            if key:
                headers["x-goog-api-key"] = key
            requests = []
            for text in texts:
                entry = {"model": model, "content": {"parts": [{"text": text}]},
                         "taskType": "RETRIEVAL_DOCUMENT" if purpose == "document" else "RETRIEVAL_QUERY"}
                if config.dimensions is not None:
                    entry["outputDimensionality"] = config.dimensions
                requests.append(entry)
            body = {"requests": requests}
        return url, headers, body

    async def embed(self, texts, *, purpose):
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("Embedding输入不能为空。")
        url, headers, body = self._request(texts, purpose)
        try:
            # 消费有界响应；失败不自动重试，不把未知服务端回显带入日志。
            async with asyncio.timeout(self.options.timeout_seconds):
                async with self._client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code >= 300:
                        raise EmbeddingError(f"Embedding请求失败（HTTP {response.status_code}）。")
                    payload, size = [], 0
                    async for piece in response.aiter_bytes():
                        size += len(piece)
                        if size > 16 * 1024 * 1024:
                            raise EmbeddingError("Embedding响应超过16MiB；请调小批量。")
                        payload.append(piece)
            import json
            data = json.loads(b"".join(payload))
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise EmbeddingError("Embedding请求超时；没有自动重试。") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Embedding连接失败（{type(exc).__name__}）。") from exc
        except (ValueError, RecursionError) as exc:
            if isinstance(exc, EmbeddingError):
                raise
            raise EmbeddingError("Embedding接口返回了无效JSON。") from exc
        try:
            if self.options.protocol == "openai":
                entries = data["data"]
                if not isinstance(entries, list) or len(entries) != len(texts) or any(type(item["index"]) is not int for item in entries) or {item["index"] for item in entries} != set(range(len(texts))):
                    raise EmbeddingError("Embedding返回的条目数量或顺序索引不正确。")
                values = [entry["embedding"] for entry in sorted(entries, key=lambda item: item["index"])]
                usage = data.get("usage")
            elif self.options.protocol == "ollama":
                values, usage = data["embeddings"], {"input_tokens": data.get("prompt_eval_count")}
            else:
                values = [entry["values"] for entry in data["embeddings"]]
                usage = data.get("usageMetadata")
            if not isinstance(values, list) or len(values) != len(texts):
                raise EmbeddingError("Embedding返回的条目数量不正确。")
            vectors = [normalized(value) for value in values]
            dimensions = len(vectors[0])
            if any(len(vector) != dimensions for vector in vectors) or (self.options.dimensions is not None and dimensions != self.options.dimensions):
                raise EmbeddingError("Embedding返回的维度与配置不一致。")
        except (KeyError, TypeError, IndexError) as exc:
            raise EmbeddingError("Embedding返回结构不符合所选协议。") from exc
        # 不返回服务端自由字段；只留实际数字用量。
        names = {"prompt_tokens", "total_tokens", "input_tokens", "promptTokenCount", "totalTokenCount", "billableTokenCount"}
        usage = {key: value for key, value in (usage or {}).items() if key in names and type(value) is int and value >= 0} if isinstance(usage, dict) else {}
        return vectors, usage
