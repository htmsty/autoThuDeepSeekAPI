# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 描述文件。

通过环境变量切换形态：
    ATD_ONEFILE=1   打单文件版
    ATD_CONSOLE=1   保留控制台（排查启动问题用）
    ATD_NAME=...    产物名

要点：**不要把外部工具链的 DLL 打进包里**。这里有两类必须挡掉的东西：
1. Codex 运行库自带的 api-ms-win-*（UCRT/API set）转发 DLL：较旧，缺少
   WaitOnAddress / WakeByAddressAll 等导出；
2. Codex 运行库 native/ 目录（Poppler 等）里的 ICU（icuuc.dll / icudt78.dll）：
   它们会盖住 Qt6Core 需要的系统 ICU，导致 “找不到指定的程序”（WinError 127）。
两类文件都交给操作系统的 System32 提供。
"""

import os
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
# 往上找带 run_dev.py 的那一层 = 仓库根目录（spec 放在 build/ 里）
ROOT = next(path for path in (SPEC_DIR, *SPEC_DIR.parents) if (path / "run_dev.py").exists())

onefile = os.environ.get("ATD_ONEFILE") == "1"
console = os.environ.get("ATD_CONSOLE") == "1"
name = os.environ.get("ATD_NAME", "autoThuDeepSeekAPI")

# 这些名字一律不进包：由 System32 提供（见文件头说明）。
EXCLUDED_DLL_PREFIXES = ("api-ms-win-",)
EXCLUDED_DLL_NAMES = {
    "ucrtbase.dll",
    "ucrtbased.dll",
}
# 这些目录里的任何 DLL 都不进包（外部工具链，容易和 Qt 抢名字）。
EXCLUDED_SOURCE_PARTS = ("\\dependencies\\native\\", "/dependencies/native/")

# 用不到的 Qt 模块：3D、多媒体、设计器一类，纯粹是体积。
# QtWebEngine 依赖的 QtQuick / QtQml / QtPositioning 等一律保留。
EXCLUDED_QT_PREFIXES = (
    "qt3d",
    "qt63d",
    "qtquick3d",
    "qt6quick3d",
    "qtpdf",
    "qt6pdf",
    "qtmultimedia",
    "qt6multimedia",
    "qt6test",
    "qt6designer",
    "qt6help",
    "qt6charts",
    "qt6datavisualization",
    "qt6graphs",
    "qt6sensors",
    "qt6serialport",
    "qt6remoteobjects",
    "qt6scxml",
    "qt6texttospeech",
    "qt6spatialaudio",
    "qt6bluetooth",
    "qt6nfc",
    "qt6location",
    "qt6virtualkeyboard",
    "qt6websockets",
    "qt6networkauth",
)

# DevTools 的调试资源有 70 多 MB，运行时用不到。
EXCLUDED_DATA_SUFFIXES = ("qtwebengine_devtools_resources.debug.pak",)
LOCALIZATION_FILES = {
    "qt_zh_cn.qm",
    "qtbase_zh_cn.qm",
    "qtdeclarative_zh_cn.qm",
    "qtwebengine_zh_cn.qm",
}

block_cipher = None

analysis = Analysis(
    [str(ROOT / "run_dev.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[
        (str(ROOT / "assets"), "assets"),
        # 当前构建运行时的 ssl.py 会从自身目录加载这张“拒绝 CA”证书。
        # PyInstaller 不会自动带上这个纯数据文件，必须放到 _MEIPASS 根目录。
        (str(ROOT / "assets" / "codex-rejected-cas.pem"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
    optimize=0,
)


def _keep(entry) -> bool:
    dest, source = entry[0], entry[1]
    dest_parts = [part.lower() for part in Path(dest).parts]
    name_lower = Path(dest).name.lower()
    if name_lower.endswith(EXCLUDED_DATA_SUFFIXES):
        return False
    if name_lower in EXCLUDED_DLL_NAMES:
        return False
    if name_lower.startswith(EXCLUDED_DLL_PREFIXES):
        return False
    if name_lower.startswith(EXCLUDED_QT_PREFIXES):
        return False
    if "qml" in dest_parts:
        index = dest_parts.index("qml")
        if index + 1 < len(dest_parts) and dest_parts[index + 1].startswith(EXCLUDED_QT_PREFIXES):
            return False
    source_lower = str(source).lower()
    return not any(part.lower() in source_lower for part in EXCLUDED_SOURCE_PARTS)


analysis.binaries = [entry for entry in analysis.binaries if _keep(entry)]
analysis.datas = [
    entry
    for entry in analysis.datas
    if not Path(entry[0]).name.lower().endswith(EXCLUDED_DATA_SUFFIXES)
    and not (
        "translations" in [part.lower() for part in Path(entry[0]).parts]
        and Path(entry[0]).name.lower().endswith(".qm")
        and Path(entry[0]).name.lower() not in LOCALIZATION_FILES
    )
]

# _ssl.pyd 需要 OpenSSL，而解释器把它们放在 Python 安装的 DLLs/ 下；
# 依赖搜索可能先碰到外部工具链里的同名文件，这里显式换成 Python 自带的那份。
BASE_DLLS = Path(sys.base_prefix) / "DLLs"
PINNED_DLLS = ("libssl-3-x64.dll", "libcrypto-3-x64.dll")
for dll_name in PINNED_DLLS:
    candidate = BASE_DLLS / dll_name
    if not candidate.exists():
        continue
    analysis.binaries = [
        entry for entry in analysis.binaries if Path(entry[0]).name.lower() != dll_name.lower()
    ]
    analysis.binaries.append((dll_name, str(candidate), "BINARY"))

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries if onefile else [],
    analysis.datas if onefile else [],
    [],
    name=name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "icon.ico"),
    version=str(ROOT / "build" / "version_info.txt"),
)

if onefile:
    # 单文件版：EXE 里已带上全部内容，这里不再 COLLECT。
    pass
else:
    COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=name,
    )
