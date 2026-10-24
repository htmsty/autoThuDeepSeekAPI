"""Windows Credential Manager storage for optional automatic sign-in."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

CREDENTIAL_TARGET = "autoThuDeepSeekAPI/THU-MadModel"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


@dataclass(frozen=True)
class LoginCredential:
    username: str
    password: str


def _advapi32():
    if os.name != "nt":
        raise OSError("Windows Credential Manager 只能在 Windows 上使用")
    return ctypes.windll.advapi32


def _last_error() -> int:
    return int(ctypes.windll.kernel32.GetLastError())


def save_login(username: str, password: str, *, target: str = CREDENTIAL_TARGET) -> None:
    """Store the optional THU login in the current user's credential vault."""

    username = username.strip()
    if not username or not password:
        raise ValueError("用户名和密码都不能为空")
    blob = password.encode("utf-16-le")
    blob_buffer = ctypes.create_string_buffer(blob, max(1, len(blob)))
    credential = _Credential()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.Comment = "Auto THU DeepSeek API optional login"
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(
        blob_buffer, ctypes.POINTER(ctypes.c_ubyte)
    )
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = username

    advapi32 = _advapi32()
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    if not advapi32.CredWriteW(ctypes.byref(credential), 0):
        raise OSError(_last_error(), "CredWriteW 失败")


def load_login(*, target: str = CREDENTIAL_TARGET) -> LoginCredential | None:
    advapi32 = _advapi32()
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None

    pointer = ctypes.POINTER(_Credential)()
    if not advapi32.CredReadW(
        target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
    ):
        error = _last_error()
        if error == _ERROR_NOT_FOUND:
            return None
        raise OSError(error, "CredReadW 失败")
    try:
        credential = pointer.contents
        raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        password = raw.decode("utf-16-le")
        username = credential.UserName or ""
        return LoginCredential(username=username, password=password)
    finally:
        advapi32.CredFree(pointer)


def delete_login(*, target: str = CREDENTIAL_TARGET) -> bool:
    advapi32 = _advapi32()
    advapi32.CredDeleteW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    if advapi32.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
        return True
    if _last_error() == _ERROR_NOT_FOUND:
        return False
    raise OSError(_last_error(), "CredDeleteW 失败")
