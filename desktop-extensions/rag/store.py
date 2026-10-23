"""SQLite保存资料版本与有界索引；读取不会在后台重新扫描用户目录。"""
from contextlib import contextmanager
import hashlib
import heapq
import math
import os
from pathlib import Path
import sqlite3
import struct
from time import time
from uuid import uuid4

from .text import CHUNK_VERSION, chunks, terms


class Cancelled(Exception):
    """插件后台任务的协作取消，不借助强行终止线程。"""


def check_cancel(event):
    if event is not None and event.is_set():
        raise Cancelled()


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries(id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id), path TEXT NOT NULL,
 title TEXT NOT NULL, current_revision TEXT);
CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), sha256 TEXT NOT NULL,
 encoding TEXT NOT NULL, source_bytes BLOB NOT NULL, text TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES revisions(id), ordinal INTEGER NOT NULL,
 start INTEGER NOT NULL, end INTEGER NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
 text TEXT NOT NULL, token_count INTEGER NOT NULL, UNIQUE(revision_id, ordinal));
CREATE TABLE IF NOT EXISTS postings(term TEXT NOT NULL, chunk_id TEXT NOT NULL REFERENCES chunks(id), frequency INTEGER NOT NULL,
 PRIMARY KEY(term, chunk_id));
CREATE TABLE IF NOT EXISTS vectors(space TEXT NOT NULL, chunk_id TEXT NOT NULL REFERENCES chunks(id), dimensions INTEGER NOT NULL,
 vector BLOB NOT NULL, PRIMARY KEY(space, chunk_id));
CREATE INDEX IF NOT EXISTS document_library ON documents(library_id);
CREATE INDEX IF NOT EXISTS chunk_revision ON chunks(revision_id);
CREATE INDEX IF NOT EXISTS posting_chunk ON postings(chunk_id);
PRAGMA user_version=1;
"""
_ACTIVE = """ FROM chunks c JOIN documents d ON d.current_revision=c.revision_id
 JOIN libraries l ON l.id=d.library_id WHERE l.enabled=1 """


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "library.sqlite3"

    @contextmanager
    def connection(self, *, write=False, cancel=None):
        """连接归单次操作；数据库事务结束后再关闭，不跨GUI线程共享。"""
        if write:
            self.directory.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5)
        else:
            if not self.path.exists():
                raise ValueError("尚未建立资料库，请在插件设置中创建并导入资料。")
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if write and version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone() is not None:
                    raise ValueError("资料库已有未知表结构，未改写；请保留并检查原数据库。")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA + "\nCOMMIT;")
            elif version != 1:
                raise ValueError("资料库版本不受支持；原数据库保留。")
            if cancel is not None:
                connection.set_progress_handler(lambda: int(cancel.is_set()), 2000)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            check_cancel(cancel)
            yield connection
            check_cancel(cancel)
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            check_cancel(cancel)
            raise ValueError(f"资料库暂不可用（{type(exc).__name__}），可能被占用或损坏；原数据保留。") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def libraries(self, *, enabled_only=False, offset=0, limit=100, space=None, allowed=None):
        if not self.path.exists():
            return {"libraries": [], "total": 0, "next_offset": None}
        with self.connection() as db:
            condition = " WHERE enabled=1" if enabled_only else " WHERE 1=1"
            parameters = ()
            if allowed is not None:
                condition += " AND id IN (" + ",".join("?" for _ in allowed) + ")" if allowed else " AND 0=1"
                parameters = tuple(allowed)
            total = db.execute("SELECT COUNT(*) FROM libraries" + condition, parameters).fetchone()[0]
            rows = db.execute("SELECT * FROM libraries" + condition + " ORDER BY created,id LIMIT ? OFFSET ?", (*parameters, limit, offset))
            result = []
            for row in rows:
                value = dict(row)
                value["enabled"] = bool(value["enabled"])
                value["documents"] = db.execute("SELECT COUNT(*) FROM documents WHERE library_id=? AND current_revision IS NOT NULL", (row["id"],)).fetchone()[0]
                value["chunks"] = db.execute("SELECT COUNT(*) FROM chunks c JOIN documents d ON d.current_revision=c.revision_id WHERE d.library_id=?", (row["id"],)).fetchone()[0]
                if space:
                    value["embedded_chunks"] = db.execute("SELECT COUNT(*) FROM vectors v JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.current_revision=c.revision_id WHERE d.library_id=? AND v.space=?", (row["id"], space)).fetchone()[0]
                result.append(value)
            return {"libraries": result, "total": total, "next_offset": offset + limit if offset + limit < total else None}

    def create_library(self, name):
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("资料库名称须为1至120字。")
        identifier = uuid4().hex
        with self.connection(write=True) as db:
            db.execute("INSERT INTO libraries VALUES(?,?,1,?)", (identifier, name, time()))
        return identifier

    def enable_library(self, identifier, enabled):
        with self.connection(write=True) as db:
            if db.execute("UPDATE libraries SET enabled=? WHERE id=?", (int(enabled), identifier)).rowcount != 1:
                raise ValueError("资料库不存在。")

    def documents(self, library_id, offset=0, limit=100):
        if not self.path.exists():
            return {"documents": [], "next_offset": None}
        with self.connection() as db:
            total = db.execute("SELECT COUNT(*) FROM documents WHERE library_id=?", (library_id,)).fetchone()[0]
            rows = db.execute("SELECT d.id,d.title,d.path,d.current_revision,r.encoding,r.created,r.sha256,length(r.source_bytes) AS bytes FROM documents d JOIN revisions r ON r.id=d.current_revision WHERE d.library_id=? ORDER BY d.title,d.id LIMIT ? OFFSET ?", (library_id, limit, offset))
            return {"documents": [dict(row) for row in rows], "total": total,
                    "next_offset": offset + limit if offset + limit < total else None}

    def import_file(self, library_id, path, encoding="auto", *, cancel=None, converted=None):
        """一份文件一笔事务；更新只切换有效版本，旧字节和片段仍可查。"""
        check_cancel(cancel)
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_file() or path.suffix.lower() not in ({".pdf", ".docx"} if converted is not None else {".txt", ".md", ".markdown"}):
            raise ValueError("首版支持TXT与Markdown文件。")
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            raw = source.read(64 * 1024 * 1024 + 1)
            after = os.fstat(source.fileno())
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError("单份资料超过64MiB，请先按章节拆分；原文件不变。")
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("读取期间文件发生变化，未发布新版本。")
        if converted is not None:
            if hashlib.sha256(raw).hexdigest() != converted["source_sha256"]:
                raise ValueError("转换期间原文件发生变化，未导入。")
            text = converted["text"]
            encoding = "converted:" + converted["converter"]
        elif encoding == "auto":
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        if converted is None and encoding not in {"utf-8-sig", "utf-16", "gb18030", "shift_jis"}:
            raise ValueError("文件编码不受支持。")
        try:
            if converted is None:
                text = raw.decode(encoding, errors="strict")
        except UnicodeError as exc:
            raise ValueError("资料解码失败，请选择正确编码后重新导入。") from exc
        if not text.strip() or "\x00" in text:
            raise ValueError("资料为空或包含二进制空字符，未导入。")
        document_id = digest(library_id + "\0" + os.path.normcase(str(path)))
        content_hash = hashlib.sha256(raw).hexdigest()
        revision_id = digest(document_id + "\0" + content_hash + "\0" + encoding + "\0" + CHUNK_VERSION + ("\0" + digest(text) if converted is not None else ""))
        check_cancel(cancel)
        with self.connection(write=True, cancel=cancel) as db:
            if db.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone() is None:
                raise ValueError("请先创建资料库。")
            previous = db.execute("SELECT current_revision FROM documents WHERE id=?", (document_id,)).fetchone()
            if previous is not None and previous[0] == revision_id:
                return {"status": "unchanged", "title": path.name, "document_id": document_id}
            db.execute("INSERT OR IGNORE INTO documents VALUES(?,?,?,?,NULL)", (document_id, library_id, str(path), path.name))
            if db.execute("SELECT 1 FROM revisions WHERE id=?", (revision_id,)).fetchone() is None:
                db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?)", (revision_id, document_id, content_hash, encoding, raw, text, time()))
                for chunk in chunks(text):
                    check_cancel(cancel)
                    chunk_id = digest(revision_id + ":" + str(chunk.ordinal))
                    words = terms(chunk.text)
                    db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)", (chunk_id, revision_id, chunk.ordinal, chunk.start, chunk.end, chunk.start_line, chunk.end_line, chunk.text, max(1, sum(words.values()))))
                    db.executemany("INSERT INTO postings VALUES(?,?,?)", ((term, chunk_id, count) for term, count in words.items()))
            db.execute("UPDATE documents SET current_revision=? WHERE id=?", (revision_id, document_id))
        return {"status": "imported", "title": path.name, "document_id": document_id, "revision_id": revision_id}

    @staticmethod
    def _scope(db, library_id):
        if library_id is None:
            return "", ()
        if isinstance(library_id, tuple):
            return (" AND l.id IN (" + ",".join("?" for _ in library_id) + ")", library_id) if library_id else (" AND 0=1", ())
        if db.execute("SELECT 1 FROM libraries WHERE id=? AND enabled=1", (library_id,)).fetchone() is None:
            raise ValueError("资料库不存在或已关闭；参数不能开放关闭的资料库。")
        return " AND l.id=?", (library_id,)

    def keyword(self, query, library_id=None, top_k=8, *, cancel=None):
        """BM25在倒排记录上累积，不加载全库原文；超预算明确返回部分排名。"""
        words = list(terms(query, query=True))
        if not words:
            raise ValueError("请输入可检索的文字或数字。")
        if len(words) > 128:
            raise ValueError("关键词过多，请缩短查询。")
        if not self.path.exists():
            return {"results": [], "coverage": {"scan_complete": True, "chunks": 0}}
        with self.connection(cancel=cancel) as db:
            scope, parameters = self._scope(db, library_id)
            count, average = db.execute("SELECT COUNT(*),AVG(c.token_count)" + _ACTIVE + scope, parameters).fetchone()
            if not count:
                return {"results": [], "coverage": {"scan_complete": True, "chunks": 0}}
            placeholders = ",".join("?" for _ in words)
            # 文档频率来自同一只读快照和同一范围，截断不改变IDF的分母。
            frequency = dict(db.execute("SELECT p.term,COUNT(*) FROM postings p JOIN chunks c ON c.id=p.chunk_id JOIN documents d ON d.current_revision=c.revision_id JOIN libraries l ON l.id=d.library_id WHERE l.enabled=1 AND p.term IN (" + placeholders + ")" + scope + " GROUP BY p.term", (*words, *parameters)))
            rows = db.execute("SELECT p.chunk_id,p.term,p.frequency,c.token_count FROM postings p JOIN chunks c ON c.id=p.chunk_id JOIN documents d ON d.current_revision=c.revision_id JOIN libraries l ON l.id=d.library_id WHERE l.enabled=1 AND p.term IN (" + placeholders + ")" + scope + " ORDER BY p.term,p.chunk_id LIMIT 100001", (*words, *parameters))
            scores, complete = {}, True
            for index, row in enumerate(rows):
                if index == 100000:
                    complete = False
                    break
                check_cancel(cancel)
                tf = row["frequency"]
                idf = math.log1p((count - frequency[row["term"]] + .5) / (frequency[row["term"]] + .5))
                score = idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * row["token_count"] / average))
                scores[row["chunk_id"]] = scores.get(row["chunk_id"], 0) + score
            selected = heapq.nlargest(top_k, scores, key=lambda key: (scores[key], key))
            return {"results": self._results(db, [(key, scores[key]) for key in selected]),
                    "coverage": {"scan_complete": complete, "chunks": count, "scored_chunks": len(scores),
                                 "posting_limit": 100000}, "score_basis": "BM25; not_probability"}

    @staticmethod
    def _results(db, ranked):
        results = []
        remaining = 12000
        for identifier, score in ranked:
            row = db.execute("SELECT c.*,d.title,d.path,l.id AS library_id,l.name AS library_name,r.sha256,r.encoding FROM chunks c JOIN revisions r ON r.id=c.revision_id JOIN documents d ON d.id=r.document_id JOIN libraries l ON l.id=d.library_id WHERE c.id=?", (identifier,)).fetchone()
            full = row["text"]
            text = full[:min(900, remaining)]
            remaining -= len(text)
            results.append({"chunk_id": identifier, "library_id": row["library_id"], "library": row["library_name"],
                            "title": row["title"], "source": row["path"], "revision_id": row["revision_id"],
                            "sha256": row["sha256"], "chunk_sha256": digest(full), "start_line": row["start_line"], "end_line": row["end_line"],
                            "start_char": row["start"], "end_char": row["end"], "text": text,
                            "excerpt_truncated": len(text) != len(full), "score": round(score, 6)})
            results[-1]["location_basis"] = "converted_markdown_lines; not_pdf_page_numbers" if row["encoding"].startswith("converted:") else "decoded_source_lines"
            if remaining <= 0:
                break
        return results

    def read(self, chunk_id, offset=0, length=6000, *, cancel=None, allowed=None):
        """通过稳定片段ID续读该版本原文，不接受任意路径。"""
        with self.connection(cancel=cancel) as db:
            row = db.execute("SELECT c.revision_id,c.start,length(r.text) AS source_length,r.encoding,d.title,d.path,l.id AS library_id,l.enabled,d.current_revision FROM chunks c JOIN revisions r ON r.id=c.revision_id JOIN documents d ON d.id=r.document_id JOIN libraries l ON l.id=d.library_id WHERE c.id=?", (chunk_id,)).fetchone()
            if row is None or not row["enabled"] or allowed is not None and row["library_id"] not in allowed:
                raise ValueError("片段不存在或所属资料库已关闭。")
            source_length = row["source_length"]
            start = row["start"] + offset
            if not 0 <= start <= source_length:
                raise ValueError("原文偏移越界。")
            end = min(source_length, start + length)
            text = db.execute("SELECT substr(text,?,?) FROM revisions WHERE id=?", (start + 1, end - start, row["revision_id"])).fetchone()[0]
            return {"chunk_id": chunk_id, "revision_id": row["revision_id"], "title": row["title"], "source": row["path"],
                    "is_current_revision": row["current_revision"] == row["revision_id"], "text": text,
                    "start_char": start, "end_char": end, "offset_basis": "decoded_source_characters_from_chunk_start",
                    "location_basis": "converted_markdown_lines; not_pdf_page_numbers" if row["encoding"].startswith("converted:") else "decoded_source_lines",
                    "next_offset": end - row["start"] if end < source_length else None, "eof": end == source_length}

    def pending_vectors(self, space, library_id, limit, *, cancel=None):
        with self.connection(cancel=cancel) as db:
            self._scope(db, library_id)
            rows = db.execute("SELECT c.id,c.text FROM chunks c JOIN documents d ON d.current_revision=c.revision_id JOIN libraries l ON l.id=d.library_id LEFT JOIN vectors v ON v.chunk_id=c.id AND v.space=? WHERE l.enabled=1 AND l.id=? AND v.chunk_id IS NULL ORDER BY c.id LIMIT ?", (space, library_id, limit))
            return [(row["id"], row["text"]) for row in rows]

    def save_vectors(self, space, rows, vectors, *, cancel=None):
        with self.connection(write=True, cancel=cancel) as db:
            dimension = len(vectors[0])
            previous = db.execute("SELECT dimensions FROM vectors WHERE space=? LIMIT 1", (space,)).fetchone()
            if previous is not None and previous[0] != dimension:
                raise ValueError("同一模型返回了不同维度，请检查接口；旧索引保留。")
            for (identifier, _text), vector in zip(rows, vectors, strict=True):
                check_cancel(cancel)
                if len(vector) != dimension:
                    raise ValueError("向量批次维度不一致。")
                blob = struct.pack("<" + "f" * dimension, *vector)
                db.execute("INSERT OR IGNORE INTO vectors VALUES(?,?,?,?)", (space, identifier, dimension, blob))

    def embedding_ready(self, space, library_id=None, *, cancel=None):
        """先核对索引与库权限，避免无索引或关闭的库触发付费查询。"""
        with self.connection(cancel=cancel) as db:
            scope, parameters = self._scope(db, library_id)
            row = db.execute("SELECT v.dimensions FROM vectors v JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.current_revision=c.revision_id JOIN libraries l ON l.id=d.library_id WHERE l.enabled=1 AND v.space=?" + scope + " LIMIT 1", (space, *parameters)).fetchone()
            if row is None:
                raise ValueError("当前模型尚无向量索引，请先在插件设置建立；关键词检索仍可用。")
            return row[0]

    def semantic(self, vector, space, library_id=None, top_k=8, *, cancel=None):
        with self.connection(cancel=cancel) as db:
            scope, parameters = self._scope(db, library_id)
            total = db.execute("SELECT COUNT(*)" + _ACTIVE + scope, parameters).fetchone()[0]
            available = db.execute("SELECT COUNT(*) FROM vectors v JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.current_revision=c.revision_id JOIN libraries l ON l.id=d.library_id WHERE l.enabled=1 AND v.space=?" + scope, (space, *parameters)).fetchone()[0]
            if not available:
                raise ValueError("当前模型尚无向量索引，请在插件设置中建立；关键词检索仍可用。")
            rows = db.execute("SELECT c.id,v.dimensions,v.vector FROM vectors v JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.current_revision=c.revision_id JOIN libraries l ON l.id=d.library_id WHERE l.enabled=1 AND v.space=?" + scope + " ORDER BY c.id LIMIT 20001", (space, *parameters))
            ranked, scanned = [], 0
            for row in rows:
                if scanned == 20000:
                    break
                check_cancel(cancel)
                if row["dimensions"] != len(vector):
                    raise ValueError("查询向量与索引维度不一致。")
                values = struct.unpack("<" + "f" * len(vector), row["vector"])
                score = sum(left * right for left, right in zip(values, vector))
                item = (score, row["id"])
                if len(ranked) < top_k:
                    heapq.heappush(ranked, item)
                elif item > ranked[0]:
                    heapq.heapreplace(ranked, item)
                scanned += 1
            selected = [(key, score) for score, key in sorted(ranked, reverse=True)]
            return {"results": self._results(db, selected), "score_basis": "cosine; not_probability",
                    "coverage": {"scan_complete": scanned == available, "index_complete": available == total,
                                 "chunks": total, "embedded_chunks": available, "scanned_vectors": scanned,
                                 "vector_scan_limit": 20000}}
