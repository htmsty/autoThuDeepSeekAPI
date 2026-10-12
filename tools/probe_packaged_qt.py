"""在普通 Python 里加载“打包目录里的 QtCore”，定位 DLL 搜索路径问题。

用法：
    python tools/probe_packaged_qt.py dist/autoThuDeepSeekAPI/_internal        # 只加 _internal
    python tools/probe_packaged_qt.py dist/autoThuDeepSeekAPI/_internal full   # 额外加 PySide6 / shiboken6
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    internal = Path(sys.argv[1]).resolve()
    add_subdirs = len(sys.argv) > 2 and sys.argv[2] == "full"

    sys.path.insert(0, str(internal))
    os.add_dll_directory(str(internal))
    if add_subdirs:
        for sub in ("PySide6", "shiboken6", "PySide6/plugins", "PySide6/plugins/platforms"):
            candidate = internal / sub
            if candidate.is_dir():
                os.add_dll_directory(str(candidate))
                print(f"已加入 DLL 目录：{candidate}")

    try:
        from PySide6 import QtCore  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"导入 QtCore 失败：{type(exc).__name__}: {exc}")
        _stage_load(internal)
        return 1

    print(f"QtCore 模块：{QtCore.__file__}")
    print(f"Qt 版本：{QtCore.qVersion()}")
    return 0


def _stage_load(internal: Path) -> None:
    """逐个加载依赖，找出真正加载不起来的那个 DLL。"""

    import ctypes  # noqa: PLC0415

    candidates = [
        internal / "PySide6" / "MSVCP140.dll",
        internal / "VCRUNTIME140.dll",
        internal / "shiboken6" / "shiboken6.abi3.dll",
        internal / "PySide6" / "pyside6.abi3.dll",
        internal / "PySide6" / "Qt6Core.dll",
        internal / "PySide6" / "QtCore.pyd",
    ]
    print("\n逐个加载：")
    for path in candidates:
        if not path.exists():
            print(f"  - {path.name}: 文件不存在")
            continue
        try:
            ctypes.WinDLL(str(path))
        except OSError as exc:
            print(f"  ! {path.name}: {exc}")
        else:
            print(f"  + {path.name}: OK")


if __name__ == "__main__":
    raise SystemExit(main())

