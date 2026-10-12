"""手动备份与一键还原：给界面用的小封装。

- 手动备份：把当前 DSH / CC Switch 的关键文件各留一份（带 `manual-` 标签）。
- 一键还原：把每个源文件恢复成**最早**的那份备份（也就是我们第一次动它之前的状态），
  还原前会先把当前状态再备份一份，所以这一步本身也可回退。
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import paths
from .backup import BackupEntry, BackupManager
from .settings import AppSettings

logger = logging.getLogger(__name__)


def _target_files(settings: AppSettings) -> list[tuple[Path, str, bool]]:
    """[(文件, 备份标签, 是否 SQLite)]，只包含开着的目标里真实存在的文件。"""

    items: list[tuple[Path, str, bool]] = []

    if settings.enable_dsh:
        dsh = paths.resolve_dsh_paths(settings.dsh_dir)
        if dsh.credentials_file:
            items.append((dsh.credentials_file, "manual-dsh-credentials", False))
        if dsh.profiles_dir and dsh.profiles_dir.is_dir():
            for profile in sorted(dsh.profiles_dir.glob("*/cordis.patch.yml")):
                items.append((profile, f"manual-dsh-profile-{profile.parent.name}", False))

    if settings.enable_ccswitch:
        cc = paths.resolve_ccswitch_paths(settings.ccswitch_dir)
        if cc.db_file:
            items.append((cc.db_file, "manual-ccswitch-db", True))
        if cc.settings_file:
            items.append((cc.settings_file, "manual-ccswitch-settings", False))
        if cc.live_state_file:
            items.append((cc.live_state_file, "manual-ccswitch-live-state", False))

    return [(path, label, is_sqlite) for path, label, is_sqlite in items if path and path.exists()]


def manual_backup(settings: AppSettings) -> tuple[int, list[str]]:
    """手动备份一次。返回 (成功份数, 说明行)。"""

    manager = BackupManager(paths.backups_dir(), keep=settings.backup_keep)
    lines: list[str] = []
    done = 0
    for path, label, is_sqlite in _target_files(settings):
        try:
            target = manager.backup_sqlite(path, label) if is_sqlite else manager.backup_file(path, label)
        except OSError as exc:
            lines.append(f"✗ {path} 备份失败：{exc}")
            continue
        if target:
            done += 1
            lines.append(f"✓ {path.name} → {target.parent.name}")
    if not lines:
        lines.append("没有找到可备份的文件（检查 DSH / CC Switch 目录与开关）")
    logger.info("手动备份完成：%d 份", done)
    return done, lines


def pending_restore(settings: AppSettings) -> dict[str, BackupEntry]:
    """一提到"会还原哪些文件"就用它，界面可以先给用户看清楚。"""

    manager = BackupManager(paths.backups_dir(), keep=settings.backup_keep)
    return manager.original_entries()


def restore_original(settings: AppSettings) -> list[str]:
    """一键还原到每个文件最早的那份备份。"""

    manager = BackupManager(paths.backups_dir(), keep=settings.backup_keep)
    messages = manager.restore()
    logger.info("一键还原：%d 条结果", len(messages))
    return messages


def backup_dir() -> Path:
    return paths.backups_dir()
