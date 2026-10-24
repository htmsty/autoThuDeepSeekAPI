"""手动备份与一键还原：给界面用的小封装。

- 手动备份：把当前 DSH / CC Switch 的关键文件各留一份（带 `manual-` 标签）。
- 一键还原：把每个源文件恢复成**最早**的那份备份（也就是我们第一次动它之前的状态），
  还原前会先把当前状态再备份一份，所以这一步本身也可回退。
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import paths
from .backup import BackupManager, is_original_label
from .change_journal import ChangeJournal, restore_all
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


def original_backed_up_sources(settings: AppSettings) -> set[str]:
    """已经留过"原始备份"（程序第一次动它之前）的源文件集合。"""

    manager = BackupManager(paths.backups_dir(), keep=settings.backup_keep)
    return {entry.source for entry in manager.entries() if entry.is_original or is_original_label(entry.label)}


def auto_backup_on_first_run(settings: AppSettings) -> int:
    """首次运行自动备份：给每个还没留过原始备份的文件各留一份。

    原始备份带 `original` 标签，不会被清理逻辑删掉（见 BackupManager.prune），
    所以在配置里始终能一键还原到"程序第一次动它之前"的状态。
    已经留过的文件跳过，重复调用不会重复记账。
    """

    manager = BackupManager(paths.backups_dir(), keep=settings.backup_keep)
    # 复用 original_backed_up_sources()，别在这里再抄一遍判定
    already = original_backed_up_sources(settings)
    # 同一次操作的文件共用一个快照目录，方便人工翻看"首次备份那一批"
    snapshot_label = "original-first-run"
    pending = [
        (path, is_sqlite)
        for path, _label, is_sqlite in _target_files(settings)
        if str(path) not in already
    ]
    if not pending:
        return 0

    manager.begin_snapshot(snapshot_label)
    done = 0
    for path, is_sqlite in pending:
        try:
            target = (
                manager.backup_sqlite(path, snapshot_label)
                if is_sqlite
                else manager.backup_file(path, snapshot_label)
            )
        except OSError as exc:
            logger.warning("首次运行备份失败：%s（%s）", path, exc)
            continue
        if target:
            done += 1
    if done:
        logger.info(
            "首次运行已自动备份 %d 份原始配置（保留不清理，可在设置里一键还原）", done
        )
    return done


def has_field_records() -> bool:
    return bool(ChangeJournal().records())


def restore_original(
    settings: AppSettings, *, allow_legacy_full_restore: bool = False
) -> list[str]:
    """Prefer field-level undo; legacy full-file restore is explicitly opt-in."""

    journal = ChangeJournal()
    records = journal.records()
    if records:
        messages = restore_all(journal)
        journal_sources = {
            str(record.get("path") or "")
            for record in records
            if isinstance(record, dict)
        }
        manager = BackupManager(paths.backups_dir(), keep=settings.backup_keep)
        legacy_only = sorted(
            source
            for source in manager.original_entries()
            if source not in journal_sources
        )
        if legacy_only:
            messages.append(
                "以下文件只有旧版整文件备份，本次未自动覆盖："
                + "、".join(Path(source).name for source in legacy_only)
            )
        messages.append("字段级还原完成；用户后来修改过的冲突字段均已保留。")
    elif allow_legacy_full_restore:
        manager = BackupManager(paths.backups_dir(), keep=settings.backup_keep)
        messages = manager.restore()
        messages.insert(0, "没有字段级变更日志，已执行旧版整文件还原。")
    else:
        messages = [
            "只有旧版整文件备份，没有字段级变更日志；为避免覆盖用户后来修改，本次未还原。"
        ]
    logger.info("一键还原：%d 条结果", len(messages))
    return messages


def backup_dir() -> Path:
    return paths.backups_dir()
