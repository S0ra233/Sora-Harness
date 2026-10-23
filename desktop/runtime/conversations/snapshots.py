"""可重建的已结算状态；原JSONL始终是事实来源，损坏或不匹配就完整重放。"""
from dataclasses import asdict
import hashlib
import json
import logging
import os

from .models import Conversation, ConversationSettings
from .revisions import apply_seed, seed_payload
from ...storage.atomic import write_text_atomic


def _stamp(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _digest(source, length):
    source.seek(0)
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            raise ValueError("原日志短于快照边界。")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


class ConversationSnapshots:
    def __init__(self, directory):
        self.directory = directory / ".snapshots"

    def restore(self, path, source):
        """同一文件标记直接恢复；追加过则先校验原前缀，再只解析新增事件。"""
        try:
            envelope = json.loads((self.directory / (path.stem + ".json")).read_text(encoding="utf-8"))
            payload = envelope["payload"]
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if envelope["version"] != 1 or hashlib.sha256(encoded.encode("utf-8")).hexdigest() != envelope["sha256"]:
                return None
            stamp = _stamp(os.fstat(source.fileno()))
            saved, offset = payload["stamp"], payload["offset"]
            if (not isinstance(saved, list) or len(saved) != 5 or type(offset) is not int or offset < 1 or offset != saved[2] or stamp[2] < offset
                    or payload["conversation_id"] != path.stem):
                return None
            if stamp != saved:
                if stamp[2] == offset or _digest(source, offset) != payload["prefix_sha256"]:
                    return None
            conversation = Conversation(path.stem, ConversationSettings.from_dict(payload["settings"]), payload["created_at"])
            apply_seed(conversation, payload["state"])
            seq = payload["last_seq"]
            if type(seq) is not int or seq < 0 or payload["line_count"] != seq + 1:
                return None
            last_event = payload["last_event"]
            if (seq == 0 and last_event is not None or seq > 0
                    and (not isinstance(last_event, dict) or last_event.get("seq") != seq)):
                return None
            conversation.last_seq = seq
            conversation._snapshot_stamp = saved
            conversation._snapshot_seq = seq
            source.seek(offset)
            return conversation, last_event, payload["line_count"]
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError):
            return None

    def save(self, path, conversation, last_event):
        """只缓存完整终态；失败不影响已经持久化的事实，也不删除旧快照。"""
        if conversation.active_turn is not None or conversation.active_compaction is not None:
            return
        try:
            with path.open("rb") as source:
                stamp = _stamp(os.fstat(source.fileno()))
                if (getattr(conversation, "_snapshot_stamp", None) == stamp
                        and getattr(conversation, "_snapshot_seq", None) == conversation.last_seq):
                    return
                prefix = _digest(source, stamp[2])
            payload = {"conversation_id": conversation.conversation_id, "settings": asdict(conversation.settings),
                       "created_at": conversation.created_at, "last_seq": conversation.last_seq,
                       "state": seed_payload(conversation), "last_event": last_event,
                       "stamp": stamp, "offset": stamp[2], "prefix_sha256": prefix,
                       "line_count": conversation.last_seq + 1}
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            checksum = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            envelope = '{"version":1,"sha256":' + json.dumps(checksum) + ',"payload":' + encoded + '}'
            write_text_atomic(self.directory / (path.stem + ".json"), envelope)
            conversation._snapshot_stamp = stamp
            conversation._snapshot_seq = conversation.last_seq
        except (OSError, ValueError, TypeError, RecursionError):
            logging.getLogger(__name__).warning("会话派生快照保存失败：会话=%s；原日志保留。", conversation.conversation_id)
