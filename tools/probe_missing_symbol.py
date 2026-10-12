"""定位“找不到指定的程序”到底是哪个导出缺失。

用法：python tools/probe_missing_symbol.py <_internal 目录> <要检查的 dll>
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import pefile


def prepare_dirs(internal: Path) -> None:
    os.add_dll_directory(str(internal))
    for sub in ("PySide6", "shiboken6"):
        candidate = internal / sub
        if candidate.is_dir():
            os.add_dll_directory(str(candidate))


def load_module(internal: Path, name: str):
    for candidate in (internal / "PySide6" / name, internal / "shiboken6" / name, internal / name):
        if candidate.exists():
            try:
                return ctypes.WinDLL(str(candidate))
            except OSError as exc:
                print(f"  ! 直接加载 {candidate.name} 失败：{exc}")
    try:
        return ctypes.WinDLL(name)
    except OSError as exc:
        print(f"  ! 按名字加载 {name} 失败：{exc}")
        return None


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    internal = Path(sys.argv[1]).resolve()
    target = Path(sys.argv[2])
    if not target.is_absolute():
        target = (internal / "PySide6" / sys.argv[2]).resolve()
    prepare_dirs(internal)

    pe = pefile.PE(str(target))
    print(f"检查 {target.name} 的导入符号")
    missing_total = 0
    for entry in pe.DIRECTORY_ENTRY_IMPORT:
        dll_name = entry.dll.decode("utf-8", "replace")
        module = load_module(internal, dll_name)
        if module is None:
            print(f"  ! 依赖 {dll_name} 加载失败")
            continue
        missing = []
        for imp in entry.imports:
            if imp.name is None:
                continue
            if ctypes.cast(module._handle, ctypes.c_void_p) and not hasattr(module, imp.name.decode("utf-8", "replace")):
                missing.append(imp.name.decode("utf-8", "replace"))
        if missing:
            missing_total += len(missing)
            print(f"  ! {dll_name}: 缺 {len(missing)} 个符号，例如 {missing[:3]}")
    pe.close()
    print(f"合计缺失 {missing_total} 个符号")
    return 1 if missing_total else 0


if __name__ == "__main__":
    raise SystemExit(main())

