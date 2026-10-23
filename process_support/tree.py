"""管理命令的进程树；这是生命周期控制，不是文件或网络沙箱。"""
import ctypes
import os
import signal
from ctypes import wintypes


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class ProcessTree:
    """Windows 的 Job 关闭时结束成员；其他系统使用独立进程组。"""

    def __init__(self) -> None:
        self.pid: int | None = None
        self._job = None
        if os.name != "nt":
            return
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self._api, name)
            function.argtypes, function.restype = arguments, result
        self._job = self._api.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE。
        if not self._api.SetInformationJobObject(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, pid: int) -> None:
        """Shell 此时仍等待 stdin 信号；归入 Job 失败就不运行用户命令。"""
        self.pid = pid
        if os.name != "nt":
            return
        process = self._api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE。
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._api.AssignProcessToJobObject(self._job, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._api.CloseHandle(process)

    def close(self) -> None:
        """正常结束也回收留在组内的子进程，不支持后台常驻命令。"""
        if os.name == "nt":
            if self._job:
                if not self._api.CloseHandle(self._job):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._job = None
        elif self.pid is not None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.pid = None
