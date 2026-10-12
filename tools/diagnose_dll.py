"""排查打包后 DLL 加载失败：逐个检查导入表里的符号是否真的存在。

用法：python tools/diagnose_dll.py dist/autoThuDeepSeekAPI/_internal
"""

from __future__ import annotations

import sys
from pathlib import Path

import pefile


def collect_modules(root: Path) -> dict[str, Path]:
    modules: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.suffix.lower() in {".dll", ".pyd", ".exe"} and path.is_file():
            modules.setdefault(path.name.lower(), path)
    return modules


def exports_of(path: Path) -> set[str]:
    try:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]]
        )
    except pefile.PEFormatError:
        return set()
    names: set[str] = set()
    for entry in getattr(pe, "DIRECTORY_ENTRY_EXPORT", []).symbols:
        if entry.name:
            names.add(entry.name.decode("utf-8", "replace"))
        else:
            names.add(f"#{entry.ordinal}")
    pe.close()
    return names


def imports_of(path: Path) -> list[tuple[str, str]]:
    try:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
    except pefile.PEFormatError:
        return []
    result: list[tuple[str, str]] = []
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        dll = entry.dll.decode("utf-8", "replace")
        for imp in entry.imports:
            name = imp.name.decode("utf-8", "replace") if imp.name else f"#{imp.ordinal}"
            result.append((dll, name))
    pe.close()
    return result


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1])
    modules = collect_modules(root)
    print(f"扫描 {root}：{len(modules)} 个模块")

    print("\n=== 关键 DLL 导出表 ===")
    for name in ("qt6core.dll", "qt6gui.dll", "qt6widgets.dll", "qt6webenginecore.dll"):
        path = modules.get(name)
        if path is None:
            print(f"  {name}: 不在包内")
            continue
        exports = exports_of(path)
        marker = "qVersion" if name == "qt6core.dll" else ""
        has = f"（含 {marker}）" if marker and marker in exports else ""
        print(f"  {name}: {path.stat().st_size / 1e6:.1f} MB，导出 {len(exports)} 个{has}")
        if name == "qt6core.dll" and marker and marker not in exports:
            for line in exports_of(path):
                pass
            print("    ! 这个 Qt6Core.dll 连 qVersion 都没有，说明它被替换或损坏了")

    export_cache: dict[str, set[str] | None] = {}
    problems = 0
    for target in ("qtcore.pyd", "qt6core.dll"):
        path = modules.get(target)
        if path is None:
            print(f"  ! 找不到 {target}")
            continue
        print(f"\n=== {path.name} ===")
        for dll, symbol in imports_of(path):
            key = dll.lower()
            if key not in export_cache:
                dependency = modules.get(key)
                export_cache[key] = exports_of(dependency) if dependency else None
            exports = export_cache[key]
            if exports is None:
                continue  # 系统 DLL：交给操作系统
            if symbol not in exports:
                print(f"  ! {path.name} 需要 {dll}!{symbol}，但随包 DLL 里没有这个导出")
                problems += 1

    print(f"\n发现 {problems} 处缺失导出")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

