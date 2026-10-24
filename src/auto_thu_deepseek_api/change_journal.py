"""Encrypted, field-level change journal used by safe restore.

Every successful sync records only the fields this application changed. Restore
walks the journal backwards and refuses to overwrite a field that the user (or
another application) changed after our write.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from . import crypto, paths
from .backup import BackupManager

logger = logging.getLogger(__name__)


class ChangeJournal:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else paths.data_root() / "changes"

    @classmethod
    def for_backup(cls, backup: BackupManager) -> "ChangeJournal":
        return cls(backup.root.parent / "changes")

    def record(self, kind: str, **payload: object) -> Path | None:
        record = {
            "version": 1,
            "kind": kind,
            "created": datetime.now().isoformat(timespec="milliseconds"),
            **payload,
        }
        try:
            raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            encrypted = crypto.protect_bytes(raw)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self.root / f"{stamp}-{kind}-{uuid.uuid4().hex[:8]}.json.dpapi"
            paths.write_bytes_atomic(path, encrypted)
            return path
        except (OSError, TypeError, ValueError) as exc:
            # The target write itself already succeeded. Failing the whole sync
            # here would be misleading, but safe restore must clearly report that
            # this transaction has no field-level undo record.
            logger.error("写入字段级变更日志失败：%s", exc)
            return None

    def records(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        result: list[dict] = []
        for path in sorted(self.root.glob("*.json.dpapi")):
            try:
                raw = crypto.unprotect_bytes(path.read_bytes(), require_encrypted=True)
                payload = json.loads(raw.decode("utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                logger.warning("读取变更日志失败：%s（%s）", path, exc)
                continue
            if isinstance(payload, dict) and payload.get("version") == 1:
                result.append(payload)
        return result

    @staticmethod
    def read(path: Path) -> dict | None:
        try:
            raw = crypto.unprotect_bytes(Path(path).read_bytes(), require_encrypted=True)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            logger.warning("读取变更日志失败：%s（%s）", path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def clear(self) -> None:
        if not self.root.exists():
            return
        for path in self.root.iterdir():
            if path.is_file() and path.name.endswith(".json.dpapi"):
                ok, detail = paths.delete_path(path)
                if not ok:
                    logger.warning("清理变更日志失败：%s（%s）", path, detail)


def restore_all(journal: ChangeJournal | None = None) -> list[str]:
    """Undo journaled fields newest-first, stopping at the first conflict per field."""

    from .targets.ccswitch_restore import restore_journal_record as restore_ccswitch
    from .targets.dsh_restore import restore_journal_record as restore_dsh

    journal = journal or ChangeJournal()
    messages: list[str] = []
    for record in reversed(journal.records()):
        kind = str(record.get("kind") or "")
        try:
            if kind in {"dsh_credentials", "dsh_profile"}:
                messages.extend(restore_dsh(record))
            elif kind in {"ccswitch_card", "ccswitch_routing"}:
                messages.extend(restore_ccswitch(record))
            else:
                messages.append(f"跳过未知变更记录：{kind}")
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the rest
            messages.append(f"变更记录 {kind} 还原失败：{exc}")
    return messages
