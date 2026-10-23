"""安装版GUI入口；启动失败保留本地记录，不修改现有配置。"""
import argparse
import ctypes
from datetime import datetime
from pathlib import Path
import traceback
from uuid import uuid4

from process_support.launcher import configure_frozen_process


def run() -> int:
    try:
        configure_frozen_process()
        from desktop.gui import main
        return main()
    except Exception:
        from desktop.storage.paths import LocalPaths
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--data-dir")
        options, _ = parser.parse_known_args()
        detail = traceback.format_exc()
        try:
            directory = LocalPaths.default(options.data_dir).root / "startup-logs"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.log"
            with path.open("x", encoding="utf-8") as output:
                output.write(detail)
            message = f"启动失败，详细记录已保留：\n{path}"
        except OSError:
            message = "启动失败，且无法写入启动记录。请核对数据目录权限及完整安装文件。"
        ctypes.windll.user32.MessageBoxW(None, message, "模型工作平台", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
