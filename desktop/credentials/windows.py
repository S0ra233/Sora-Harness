"""当前Windows用户的通用凭据；不把密钥写入项目配置或进程环境。"""
import ctypes
import sys
from ctypes import wintypes
from uuid import UUID


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
    ]


def _target(reference: str) -> str:
    if not isinstance(reference, str) or UUID(reference).hex != reference:
        raise ValueError("凭据引用须为小写UUID，不接受任意系统凭据名称。")
    return "game-translation.desktop/model/" + reference


def _api():
    if sys.platform != "win32":
        raise OSError("当前系统尚未接入密钥存储；可使用高级设置中的环境变量方式。")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(_Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def save(reference: str, key: str) -> None:
    """同一引用更新密钥；本机当前用户重启后可读，不执行删除或跨电脑同步。"""
    target = _target(reference)
    if not isinstance(key, str) or not key.strip():
        raise ValueError("密钥不能为空。")
    blob = key.encode("utf-8")
    if len(blob) > 2560:
        raise ValueError("密钥超过Windows通用凭据的2560字节上限。")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = _Credential(Type=1, TargetName=target, CredentialBlobSize=len(blob),
                             CredentialBlob=buffer, Persist=2, UserName="desktop")
    api = _api()
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise OSError(f"系统密钥保存失败（Windows错误码 {ctypes.get_last_error()}）。")


def read(reference: str) -> str:
    """返回密钥；系统分配的凭据缓冲区无论解码是否成功都释放。"""
    target, api = _target(reference), _api()
    credential = ctypes.POINTER(_Credential)()
    if not api.CredReadW(target, 1, 0, ctypes.byref(credential)):
        raise OSError(f"系统密钥读取失败（Windows错误码 {ctypes.get_last_error()}）；请在设置中重新保存密钥。")
    try:
        value = credential.contents
        try:
            key = ctypes.string_at(value.CredentialBlob, value.CredentialBlobSize).decode("utf-8")
        except UnicodeDecodeError:
            # 解码异常包含原始字节，不能沿异常链进入运行诊断。
            raise ValueError("系统凭据内容无法解码，请重新保存密钥。") from None
        if not key.strip():
            raise ValueError("系统凭据中的密钥为空，请重新保存密钥。")
        return key
    finally:
        api.CredFree(credential)
