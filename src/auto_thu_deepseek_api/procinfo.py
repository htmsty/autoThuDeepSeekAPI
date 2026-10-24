"""查一下有哪些进程在跑。

只用来判断"某个程序到底装没装"这类问题：装了但还没生成配置文件时，进程名是一份有用的线索。
查一次缓存一次，避免启动时反复开进程。非 Windows 上直接返回空集合。
"""

from __future__ import annotations

import csv
import ctypes
import io
import logging
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE: set[str] | None = None


def _windows_process_names() -> set[str]:
    """Read process names with the native Toolhelp snapshot API."""

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle or not snapshot:
        raise OSError("CreateToolhelp32Snapshot failed")

    names: set[str] = set()
    entry = ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(ProcessEntry32W)
    try:
        has_entry = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while has_entry:
            name = str(entry.szExeFile).strip().lower()
            if name:
                names.add(name)
            has_entry = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    return names


def _tasklist_process_names() -> set[str]:
    """Fallback for older or restricted Windows environments."""

    completed = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    names: set[str] = set()
    for row in csv.reader(io.StringIO(completed.stdout or "")):
        if row and row[0].strip():
            names.add(Path(row[0].strip()).name.lower())
    return names


def running_process_names(*, refresh: bool = False) -> set[str]:
    """当前在跑的进程名（全小写，含 .exe）。查不到就返回空集合。"""

    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE

    names: set[str] = set()
    if sys.platform == "win32":
        try:
            names = _windows_process_names()
        except Exception as exc:  # noqa: BLE001 - 原生 API 失败时退回 tasklist
            logger.info("原生进程查询失败，回退 tasklist：%s", exc)
            try:
                names = _tasklist_process_names()
            except (OSError, subprocess.SubprocessError) as fallback_exc:
                logger.info("查询进程列表失败（按「没在跑」处理）：%s", fallback_exc)

    _CACHE = names
    return names


def any_running(*keywords: str, refresh: bool = False) -> str | None:
    """进程名里只要包含任一关键词就算命中，返回命中的进程名。"""

    lowered = [word.lower() for word in keywords if word]
    for name in running_process_names(refresh=refresh):
        if any(word in name for word in lowered):
            return name
    return None
