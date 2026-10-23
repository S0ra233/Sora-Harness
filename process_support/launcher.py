"""源码与冻结程序共用进程入口；后台程序与GUI分别保留标准输入输出。"""
import os
from pathlib import Path
import sys


_dll_directories = []


def configure_frozen_process() -> None:
    """补充并保留内置DLL目录，不能在Qt导入前清理官方启动环境。"""
    if os.name != "nt" or not getattr(sys, "frozen", False) or _dll_directories:
        return
    root = Path(sys._MEIPASS).resolve()
    for directory in (root, root / "PySide6", root / "shiboken6"):
        if directory.is_dir():
            # 句柄须保持到进程结束；仅登记_internal根不足以覆盖Qt所在子目录。
            _dll_directories.append(os.add_dll_directory(str(directory)))


def worker_command(mode: str, python_arguments, *arguments: str) -> list[str]:
    if not getattr(sys, "frozen", False):
        return [sys.executable, *map(str, python_arguments), *map(str, arguments)]
    executable = Path(sys.executable).with_name("DesktopHarnessWorker.exe")
    if not executable.is_file():
        raise FileNotFoundError(f"后台执行程序缺失，请保留完整安装目录：{executable}")
    return [str(executable), mode, *map(str, arguments)]
