"""仅隐藏系统标题栏左侧图标与文字；保留原生拖动、缩放和窗口按钮。"""
import ctypes
import sys
from ctypes import wintypes


def white_rounded_caption(window_id: int) -> tuple[int, ...]:
    """独立辅助窗口使用原生白色标题栏及圆角，保留系统拖动、缩放与按钮。"""
    if sys.platform != "win32":
        return ()
    api = ctypes.WinDLL("dwmapi", use_last_error=True)
    setter = api.DwmSetWindowAttribute
    setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    setter.restype = ctypes.c_long
    failed = []
    # Windows 11公开DWM属性；旧系统拒绝时沿原生外观继续，不阻断页面。
    for attribute, value in ((20, 0), (33, 2), (34, 0xFFFFFFFE), (35, 0xFFFFFF)):
        data = wintypes.DWORD(value)
        if setter(window_id, attribute, ctypes.byref(data), ctypes.sizeof(data)) < 0:
            failed.append(attribute)
    return tuple(failed)


def hide_caption_label(window_id: int) -> None:
    if sys.platform != "win32":
        return
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    api.GetWindowLongW.restype = wintypes.LONG
    api.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    api.SetWindowLongW.restype = wintypes.LONG
    api.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    api.SetWindowTextW.restype = wintypes.BOOL
    api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
    api.SetWindowPos.restype = wintypes.BOOL
    # Qt的Windows实现也使用此扩展样式为保留关闭按钮的无图标标题栏提供边框。
    ctypes.set_last_error(0)
    style = api.GetWindowLongW(window_id, -20)
    if style == 0 and ctypes.get_last_error():
        raise OSError(f"标题栏样式读取失败（Windows错误码 {ctypes.get_last_error()}）。")
    ctypes.set_last_error(0)
    previous = api.SetWindowLongW(window_id, -20, style | 0x00000001)
    if previous == 0 and ctypes.get_last_error():
        raise OSError(f"标题栏样式更新失败（Windows错误码 {ctypes.get_last_error()}）。")
    if not api.SetWindowTextW(window_id, ""):
        raise OSError(f"标题文字隐藏失败（Windows错误码 {ctypes.get_last_error()}）。")
    # 主窗口已设置透明图标；不再发空WM_SETICON，否则系统可能回退到窗口类图标。
    if not api.SetWindowPos(window_id, None, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020):
        raise OSError(f"标题栏刷新失败（Windows错误码 {ctypes.get_last_error()}）。")
