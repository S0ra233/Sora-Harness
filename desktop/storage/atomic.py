"""小型状态文件的持久替换；失败候选保留，不删除用户记录。"""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
                            suffix=".tmp", delete=False) as output:
        candidate = Path(output.name)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    candidate.replace(path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
