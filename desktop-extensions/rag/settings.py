"""Embedding配置独立于聊天接口；保存本身不联网。"""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from desktop.storage.atomic import write_text_atomic


@dataclass(frozen=True)
class EmbeddingOptions:
    protocol: str = "openai"
    url: str = ""
    model: str = ""
    api_key: str = ""
    dimensions: int | None = None
    timeout_seconds: int = 60
    batch_size: int = 16
    enabled: bool = False

    def validate(self):
        if not isinstance(self.protocol, str) or self.protocol not in {"openai", "ollama", "gemini"}:
            raise ValueError("Embedding协议不受支持。")
        if any(not isinstance(value, str) for value in (self.url, self.model, self.api_key)):
            raise ValueError("接口地址、模型和密钥须为文字。")
        if type(self.enabled) is not bool:
            raise ValueError("Embedding开关须为布尔值。")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 600:
            raise ValueError("请求时限须为1至600秒。")
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 64:
            raise ValueError("批量大小须为1至64。")
        if self.dimensions is not None and (type(self.dimensions) is not int or not 1 <= self.dimensions <= 16384):
            raise ValueError("向量维度须为1至16384或留空。")
        if self.url:
            address = urlsplit(self.url)
            if address.scheme not in {"http", "https"} or not address.hostname or address.username or address.password or address.query or address.fragment:
                raise ValueError("填写不含凭据、查询参数的HTTP接口地址；密钥填写到单独字段。")
        if self.enabled and (not self.url or not self.model.strip()):
            raise ValueError("启用Embedding前请填写接口地址和模型。")
        if self.protocol == "gemini" and self.model and not self.model.removeprefix("models/").replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise ValueError("Gemini模型名称格式不正确。")
        return self

    def fingerprint(self):
        """索引空间不含密钥；不同协议、地址、模型或维度不得混算。"""
        fields = (self.protocol, self.url.rstrip("/"), self.model, self.dimensions, "retrieval-1")
        return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


class Settings:
    def __init__(self, directory: Path):
        self.path = directory / "embedding.json"

    def load(self):
        if not self.path.exists():
            return EmbeddingOptions()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or not isinstance(data.get("embedding"), dict):
            raise ValueError("Embedding设置格式不正确；原文件保留。")
        try:
            return EmbeddingOptions(**data["embedding"]).validate()
        except TypeError as exc:
            raise ValueError("Embedding设置包含未知字段；原文件保留。") from exc

    def save(self, options):
        self.load()
        options.validate()
        write_text_atomic(self.path, json.dumps({"version": 1, "embedding": asdict(options)}, ensure_ascii=False, indent=2))
