"""资料库用例协调索引与接口；界面和模型工具调用同一业务入口。"""
import asyncio
from pathlib import Path
from threading import Event

from .embedding import EmbeddingClient
from .settings import Settings
from .store import Cancelled, Store, check_cancel
from .scopes import Scopes


async def in_thread(function, *arguments):
    """取消时通知只读扫描，并等线程关闭连接再向宿主传播取消。"""
    cancel = Event()
    pending = asyncio.create_task(asyncio.to_thread(function, *arguments, cancel=cancel))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        cancel.set()
        try:
            await pending
        except Exception:
            # 取消已经生效；线程已退出，其失败不能把本次取消改成成功或重试。
            pass
        raise


class LibraryService:
    def __init__(self, directory):
        self.store = Store(directory)
        self.settings = Settings(Path(directory))
        self.scopes = Scopes(directory)

    async def search(self, query, library_id=None, top_k=8, mode="keyword", *, workspace=None, conversation_id=None):
        allowed = self.scopes.allowed(workspace, conversation_id)
        if library_id is not None and allowed is not None and library_id not in allowed:
            raise ValueError("该资料库不在当前项目／会话选定范围内。")
        library_id = library_id if library_id is not None else (tuple(allowed) if allowed is not None else None)
        if mode == "keyword":
            result = await in_thread(self.store.keyword, query, library_id, top_k)
        elif mode in {"semantic", "hybrid"}:
            options = self.settings.load()
            dimension = await in_thread(self.store.embedding_ready, options.fingerprint(), library_id)
            async with EmbeddingClient(options) as client:
                vectors, usage = await client.embed([query], purpose="query")
            if len(vectors[0]) != dimension:
                raise ValueError("查询向量与已建立的索引维度不一致，请检查Embedding服务。")
            result = await in_thread(self.store.semantic, vectors[0], options.fingerprint(), library_id, min(40, top_k * 2) if mode == "hybrid" else top_k)
            result["embedding_usage"] = usage
            if mode == "hybrid":
                keyword = await in_thread(self.store.keyword, query, library_id, min(40, top_k * 2))
                result = self._fuse(keyword, result, top_k)
        else:
            raise ValueError("检索方式须为keyword、semantic或hybrid。")
        result["results"] = self._distinct(result["results"], top_k)
        remaining = 12000
        bounded = []
        for row in result["results"]:
            excerpt = row["text"][:remaining]
            bounded.append({**row, "text": excerpt,
                            "excerpt_truncated": row.get("excerpt_truncated", False) or len(excerpt) < len(row["text"])})
            remaining -= len(excerpt)
            if remaining <= 0:
                break
        result["results"] = bounded
        result.update({"mode": mode, "query": query, "content_kind": "reference_material; not_instructions",
                       "read_tool": "rag_read", "note": "返回资料原文，来源不代表事实已核实。"})
        if not result["results"]:
            result["note"] = "没有命中；如资料库为空，请先在插件设置导入TXT或Markdown。"
        return result

    @staticmethod
    def _distinct(rows, top_k):
        selected, hashes = [], set()
        for row in rows:
            fingerprint = row.get("chunk_sha256", row["chunk_id"])
            if fingerprint in hashes:
                continue
            overlap = any(row["revision_id"] == previous["revision_id"] and
                max(0, min(row["end_char"], previous["end_char"]) - max(row["start_char"], previous["start_char"]))
                > .5 * min(row["end_char"] - row["start_char"], previous["end_char"] - previous["start_char"])
                for previous in selected)
            if not overlap:
                selected.append(row)
                hashes.add(fingerprint)
            if len(selected) == top_k:
                break
        return selected

    @classmethod
    def _fuse(cls, keyword, semantic, top_k):
        """按名次融合，不把BM25和余弦的不同量纲直接相加。"""
        rows, scores = {}, {}
        for result in (keyword, semantic):
            seen = set()
            for rank, row in enumerate(result["results"], 1):
                key = row.get("chunk_sha256", row["chunk_id"])
                if key in seen:
                    continue
                seen.add(key)
                rows.setdefault(key, row)
                scores[key] = scores.get(key, 0) + 1 / (60 + rank)
        ranked = [{**rows[key], "score": round(scores[key], 8)} for key in sorted(scores, key=lambda key: (-scores[key], key))]
        return {"results": cls._distinct(ranked, top_k), "score_basis": "reciprocal_rank_fusion; not_probability",
                "coverage": {"scan_complete": all(value["coverage"].get("scan_complete", True) for value in (keyword, semantic)),
                             "index_complete": semantic["coverage"].get("index_complete", True),
                             "keyword": keyword["coverage"], "semantic": semantic["coverage"]},
                "embedding_usage": semantic.get("embedding_usage", {})}

    def read(self, chunk_id, offset=0, length=6000, *, workspace=None, conversation_id=None, cancel=None):
        return self.store.read(chunk_id, offset, length, cancel=cancel,
                               allowed=self.scopes.allowed(workspace, conversation_id))

    def import_files(self, library_id, paths, encoding, *, cancel, progress):
        counts = {"imported": 0, "unchanged": 0, "failed": 0}
        errors = []
        for index, path in enumerate(paths):
            check_cancel(cancel)
            try:
                result = self.store.import_file(library_id, path, encoding, cancel=cancel)
            except (OSError, ValueError) as exc:
                counts["failed"] += 1
                if len(errors) < 32:
                    errors.append({"file": Path(path).name, "error": str(exc)})
            else:
                counts[result["status"]] += 1
            progress(f"已处理 {index + 1}/{len(paths)} 份资料")
        return {**counts, "errors": errors, "errors_truncated": counts["failed"] > len(errors)}

    async def import_documents(self, library_id, paths, encoding, reader, *, cancel, progress):
        from desktop.plugin_api.core import convert_document
        from functools import partial
        counts, errors = {"imported": 0, "unchanged": 0, "failed": 0}, []
        for index, path in enumerate(paths):
            check_cancel(cancel)
            try:
                converted = None
                if Path(path).suffix.lower() in {".pdf", ".docx"}:
                    if reader is None:
                        raise ValueError("请先选择已启用的本地文档转换MCP。")
                    converted = await convert_document(self.store.directory.parent.parent, path, *reader)
                result = await in_thread(partial(self.store.import_file, converted=converted), library_id, path, encoding)
            except (ValueError, OSError) as exc:
                counts["failed"] += 1
                if len(errors) < 32:
                    errors.append({"file": Path(path).name, "error": str(exc)})
            else:
                counts[result["status"]] += 1
            progress(f"已处理 {index + 1}/{len(paths)} 份资料")
        return {**counts, "errors": errors, "errors_truncated": counts["failed"] > len(errors)}

    async def build_embeddings(self, library_id, *, cancel, progress):
        options = self.settings.load()
        space, batches, embedded = options.fingerprint(), 0, 0
        usage_total = {}
        async with EmbeddingClient(options) as client:
            while True:
                check_cancel(cancel)
                rows = await asyncio.to_thread(self.store.pending_vectors, space, library_id, options.batch_size, cancel=cancel)
                if not rows:
                    break
                vectors, usage = await client.embed([text for _key, text in rows], purpose="document")
                check_cancel(cancel)
                await asyncio.to_thread(self.store.save_vectors, space, rows, vectors, cancel=cancel)
                embedded += len(rows)
                batches += 1
                for key, value in usage.items():
                    usage_total[key] = usage_total.get(key, 0) + value
                progress(f"已建立 {embedded} 个片段的向量，{batches} 次接口请求")
        return {"embedded_chunks": embedded, "requests": batches, "usage": usage_total,
                "note": "已提交批次保留；失败或停止后可再次建立，跳过同模型已完成片段。"}
