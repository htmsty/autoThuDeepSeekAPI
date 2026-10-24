"""Windows DPAPI helpers for local secrets and backup payloads."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

ENCRYPTED_PREFIX = b"ATD-DPAPI-1\0"
_ENTROPY = b"autoThuDeepSeekAPI/local-secrets/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    value = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return value, buffer


def _require_windows() -> None:
    if os.name != "nt":
        raise OSError("Windows DPAPI 只能在 Windows 上使用")


def _protect(data: bytes) -> bytes:
    _require_windows()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    output_blob = _DataBlob()
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "autoThuDeepSeekAPI",
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    # Keep the buffers alive until the call has returned.
    _ = (input_buffer, entropy_buffer)
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    _require_windows()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    output_blob = _DataBlob()
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    description = wintypes.LPWSTR()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    _ = (input_buffer, entropy_buffer)
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData 失败")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(output_blob.pbData)


def protect_bytes(data: bytes) -> bytes:
    """Encrypt bytes for the current Windows user."""

    return ENCRYPTED_PREFIX + _protect(data)


def unprotect_bytes(data: bytes, *, require_encrypted: bool = False) -> bytes:
    """Decrypt one of our DPAPI payloads.

    Legacy backups deliberately remain readable until they are migrated. Callers
    that must not accept plaintext can set ``require_encrypted=True``.
    """

    if data.startswith(ENCRYPTED_PREFIX):
        return _unprotect(data[len(ENCRYPTED_PREFIX) :])
    if require_encrypted:
        raise ValueError("数据没有使用 DPAPI 加密")
    return data


def is_encrypted(data: bytes) -> bool:
    return data.startswith(ENCRYPTED_PREFIX)


def protect_text(text: str) -> bytes:
    return protect_bytes(text.encode("utf-8"))


def unprotect_text(data: bytes, *, require_encrypted: bool = False) -> str:
    return unprotect_bytes(data, require_encrypted=require_encrypted).decode("utf-8")
