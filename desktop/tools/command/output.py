"""完整命令输出持续落盘，模型窗口只折叠连续重复行并明确标注省略范围。"""
import asyncio
import codecs
import re
from collections import deque
from typing import BinaryIO


MAX_OUTPUT_CHARS = 20000
_HEAD_CHARS = 2000
_TAIL_CHARS = MAX_OUTPUT_CHARS - _HEAD_CHARS - 256
_MAX_LINE_CHARS = 4096


class _OutputWindow:
    """保留首尾定长窗口；内存不随日志长度增长。"""
    def __init__(self) -> None:
        self.head = ""
        self._tail = deque()
        self._tail_chars = 0
        self.characters = 0

    def append(self, text: str) -> None:
        self.characters += len(text)
        self.head += text[:max(0, _HEAD_CHARS - len(self.head))]
        if text:
            self._tail.append(text)
            self._tail_chars += len(text)
            while self._tail_chars > _TAIL_CHARS:
                excess = self._tail_chars - _TAIL_CHARS
                first = self._tail.popleft()
                if len(first) > excess:
                    self._tail.appendleft(first[excess:])
                    self._tail_chars -= excess
                else:
                    self._tail_chars -= len(first)

    def render(self) -> tuple[str, int]:
        tail = "".join(self._tail)
        if self.characters <= _HEAD_CHARS + _TAIL_CHARS:
            rest = self.characters - len(self.head)
            return self.head + (tail[-rest:] if rest else ""), 0
        left = self.head[:self.head.rfind("\n") + 1] if "\n" in self.head else self.head
        right = tail[tail.find("\n") + 1:] if "\n" in tail else tail
        omitted = self.characters - len(left) - len(right)
        return left + f"\n…[省略{omitted}字符，首尾超长行可能为片段；完整输出用read_tool_record]…\n" + right, omitted


class CommandOutput:
    """仅相邻、逐字一致且非空的完整行可折叠；长行直接透传到有界窗口。"""
    def __init__(self, *, collect_compact: bool = True) -> None:
        self.tail = ""
        self.characters = 0
        self.bytes = 0
        self._window = _OutputWindow()
        self._pending = ""
        self._previous = ""
        self._repeated = 0
        self._folded = 0
        self._long_line = False
        self._collect_compact = collect_compact

    async def read(self, stream: asyncio.StreamReader, target: BinaryIO) -> None:
        """先保存原字节再整理显示；UTF-8和换行可以跨管道块。"""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := await stream.read(65536):
            target.write(chunk)
            self.bytes += len(chunk)
            self._append(decoder.decode(chunk))
        self._append(decoder.decode(b"", final=True))
        self._finish()

    def _flush_repeated(self) -> None:
        if self._repeated:
            self._window.append(f"…[另有{self._repeated}行与上一行连续重复]…\n")
            self._folded += self._repeated
            self._repeated = 0

    def _line(self, text: str) -> None:
        if not self._long_line and len(text) <= _MAX_LINE_CHARS and text.strip() and text == self._previous:
            self._repeated += 1
            return
        self._flush_repeated()
        self._window.append(text)
        self._previous = text if not self._long_line and len(text) <= _MAX_LINE_CHARS and text.strip() else ""
        self._long_line = False

    def _append(self, text: str) -> None:
        self.characters += len(text)
        self.tail = (self.tail + text)[-MAX_OUTPUT_CHARS:]
        if not self._collect_compact:
            return
        pending = self._pending + text
        start = 0
        for ending in re.finditer(r"\r\n|\r|\n", pending):
            if ending.group() == "\r" and ending.end() == len(pending):
                break
            self._line(pending[start:ending.end()])
            start = ending.end()
        self._pending = pending[start:]
        if len(self._pending) > _MAX_LINE_CHARS:
            self._flush_repeated()
            self._previous = ""
            self._long_line = True
            keep = 1 if self._pending.endswith("\r") else 0
            self._window.append(self._pending[:-keep] if keep else self._pending)
            self._pending = self._pending[-keep:] if keep else ""

    def _finish(self) -> None:
        if self._pending:
            self._line(self._pending)
            self._pending = ""
        self._flush_repeated()

    def preview(self, *, compact: bool) -> dict:
        """失败保留原末尾诊断；整理成功与否不改变命令真实退出状态。"""
        # 超时也可能取消管道读取；把尚未输出的行和重复次数结算到窗口。
        self._finish()
        output, omitted = self._window.render() if compact else (self.tail, max(0, self.characters - len(self.tail)))
        if compact and self.characters <= MAX_OUTPUT_CHARS and (omitted or len(output) >= self.characters):
            # 小日志完整保留；重复标记比原文长时也不为“压缩”增加输入。
            compact, output, omitted = False, self.tail, 0
        return {"output": output, "truncated": omitted > 0, "compacted": compact and self._folded > 0,
                "output_summary": {"mode": "compact" if compact else "raw", "original_characters": self.characters,
                                   "original_bytes": self.bytes, "rendered_characters": len(output),
                                   "omitted_characters": omitted, "duplicate_lines_folded": self._folded if compact else 0,
                                   "omitted_characters_scope": "compacted_output" if compact else "original_output",
                                   "representation_complete": omitted == 0}}
