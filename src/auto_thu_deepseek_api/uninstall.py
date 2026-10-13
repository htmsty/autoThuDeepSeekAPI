"""干净卸载：可选还原 DSH / CC Switch，然后清掉程序自己的运行数据。

只用正常的文件操作：不写自删除脚本、不调用 `rd` / `del` / `reg`，免得被安全软件当成恶意行为。
程序自己的 exe 不删（界面上会提示用户手动删）。

只删"程序管理的东西"（见 `paths.MANAGED_ENTRIES`）：用户自己放进 private 的文件一律不动，
目录里还有用户的东西时也不整个删目录。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import backup_ops, paths
from .settings import AppSettings

logger = logging.getLogger(__name__)


def clean_uninstall(settings: AppSettings, *, restore_targets: bool) -> tuple[bool, list[str]]:
    """执行干净卸载。返回 (是否顺利完成, 说明行)。"""

    lines: list[str] = []
    ok = True

    if restore_targets:
        try:
            messages = backup_ops.restore_original(settings)
        except Exception as exc:  # noqa: BLE001 - 卸载流程不能因为一个异常就中断清理
            ok = False
            lines.append(f"还原 DSH / CC Switch 失败：{exc}")
        else:
            lines.extend(messages or ["没有找到可还原的首次备份，跳过还原"])

    root = paths.data_root()
    lines.extend(paths.remove_managed_data(root))

    pointer = paths.private_pointer_file()
    try:
        if pointer.exists():
            pointer.unlink()
            lines.append(f"已删除位置指针 {pointer}")
    except OSError as exc:
        ok = False
        lines.append(f"没删掉位置指针 {pointer}：{exc}")

    # 以前可能退回到 %LOCALAPPDATA%\autoThuDeepSeekAPI\private，顺手也清一份
    fallback = paths.local_app_dir() / "private"
    if fallback != root and fallback.is_dir():
        lines.extend(paths.remove_managed_data(fallback))

    # 位置指针所在的目录空了再删（有别的东西就留着）
    try:
        app_local = paths.local_app_dir()
        if app_local.is_dir() and not any(app_local.iterdir()):
            app_local.rmdir()
            lines.append(f"已删除空目录 {app_local}")
    except OSError:  # pragma: no cover - 删不掉就留着
        pass

    paths.reset_data_root_cache()
    logger.info("干净卸载完成：%d 条操作", len(lines))
    return ok, lines


def program_exe_hint() -> str:
    """提示用户手动删哪个文件（我们不删自己）。"""

    if paths.is_frozen():
        return str(Path(sys.executable).resolve())
    return str(paths.app_dir())
