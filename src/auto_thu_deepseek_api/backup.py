"""备份与还原：写入前留底，可一键回到首次修改前的状态。"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

INDEX_NAME = "index.json"


@dataclass
class BackupEntry:
    label: str
    source: str
    backup: str
    created: str
    sha256: str
    size: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


class BackupManager:
    """把文件副本放进 private/backups/<时间戳>-<标签>/，并在 index.json 里记账。"""

    def __init__(self, root: Path, keep: int = 10) -> None:
        self.root = Path(root)
        self.keep = max(1, int(keep))
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / INDEX_NAME

    # —— 索引 ——

    def _load_index(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("备份索引损坏，忽略：%s", self.index_path)
            return []
        return raw if isinstance(raw, list) else []

    def _save_index(self, entries: list[dict]) -> None:
        self.index_path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def entries(self) -> list[BackupEntry]:
        result: list[BackupEntry] = []
        for item in self._load_index():
            try:
                result.append(BackupEntry(**item))
            except TypeError:
                continue
        return result

    def _record(self, entry: BackupEntry) -> None:
        entries = self._load_index()
        entries.append(asdict(entry))
        self._save_index(entries)

    # —— 备份 ——

    def _new_snapshot_dir(self, label: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        safe_label = "".join(ch for ch in label if ch.isalnum() or ch in "-_") or "backup"
        base = f"{stamp}-{safe_label}"
        path = self.root / base
        counter = 1
        # 毫秒级时间戳也可能撞（同一毫秒里连做几次备份），撞了就加序号，
        # 否则两条记账会指向同一个文件，清理逻辑就数不清份数了。
        while path.exists():
            counter += 1
            path = self.root / f"{base}-{counter}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def backup_file(self, source: Path, label: str) -> Path | None:
        """普通文件备份；文件不存在返回 None。"""

        source = Path(source)
        if not source.exists():
            return None
        snapshot = self._new_snapshot_dir(label)
        target = snapshot / source.name
        shutil.copy2(source, target)
        self._record(
            BackupEntry(
                label=label,
                source=str(source),
                backup=str(target),
                created=datetime.now().isoformat(timespec="milliseconds"),
                sha256=_sha256(target),
                size=target.stat().st_size,
            )
        )
        logger.info("已备份 %s → %s", source, target)
        self.prune()
        return target

    def backup_sqlite(self, source: Path, label: str) -> Path | None:
        """SQLite 一致性备份（含 WAL 里未落盘的内容）。"""

        source = Path(source)
        if not source.exists():
            return None
        snapshot = self._new_snapshot_dir(label)
        target = snapshot / source.name
        try:
            src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(target)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except sqlite3.Error as exc:
            logger.warning("SQLite 备份接口失败（%s），改用文件复制", exc)
            for suffix in ("", "-wal", "-shm"):
                part = Path(str(source) + suffix)
                if part.exists():
                    shutil.copy2(part, snapshot / part.name)
        self._record(
            BackupEntry(
                label=label,
                source=str(source),
                backup=str(target),
                created=datetime.now().isoformat(timespec="milliseconds"),
                sha256=_sha256(target),
                size=target.stat().st_size if target.exists() else 0,
            )
        )
        logger.info("已备份数据库 %s → %s", source, target)
        self.prune()
        return target

    # —— 清理 ——

    @staticmethod
    def _sort_key(item: dict) -> tuple[str, str]:
        """快照目录名带毫秒时间戳，用它排序最稳（created 只有秒级）。"""

        backup_path = Path(item.get("backup", ""))
        return (backup_path.parent.name, item.get("created", ""))

    def prune(self) -> None:
        """每个源文件只保留最近 keep 份，避免无限增长。"""

        entries = self._load_index()
        by_source: dict[str, list[dict]] = {}
        for item in entries:
            by_source.setdefault(item.get("source", ""), []).append(item)

        keep_paths: set[str] = set()
        for items in by_source.values():
            items.sort(key=self._sort_key)
            keep_paths.update(item.get("backup", "") for item in items[-self.keep :])

        removed_dirs: set[str] = set()
        for item in entries:
            backup_path = item.get("backup", "")
            if backup_path and backup_path not in keep_paths:
                removed_dirs.add(str(Path(backup_path).parent))

        self._save_index([item for item in entries if item.get("backup", "") in keep_paths])

        for directory in removed_dirs:
            path = Path(directory)
            try:
                if path.is_dir() and path.parent == self.root:
                    shutil.rmtree(path)
            except OSError as exc:  # pragma: no cover
                logger.warning("清理旧备份失败：%s (%s)", path, exc)

    # —— 还原 ——

    def original_entries(self) -> dict[str, BackupEntry]:
        """每个源文件最早的一份备份 = 首次修改前的状态。"""

        result: dict[str, BackupEntry] = {}
        for entry in sorted(self.entries(), key=lambda item: Path(item.backup).parent.name):
            result.setdefault(entry.source, entry)
        return result

    def latest_entries(self) -> dict[str, BackupEntry]:
        result: dict[str, BackupEntry] = {}
        for entry in sorted(self.entries(), key=lambda item: Path(item.backup).parent.name):
            result[entry.source] = entry
        return result

    def restore(self, entries: dict[str, BackupEntry] | None = None, label: str = "pre-restore") -> list[str]:
        """把备份复制回原位。还原前先把当前状态也备份一份。"""

        targets = entries if entries is not None else self.original_entries()
        messages: list[str] = []
        for source_str, entry in targets.items():
            source = Path(source_str)
            backup = Path(entry.backup)
            if not backup.exists():
                messages.append(f"跳过 {source}：备份文件已不存在")
                continue
            if source.exists():
                try:
                    self.backup_file(source, label)
                except OSError as exc:
                    messages.append(f"跳过 {source}：还原前备份失败（{exc}）")
                    continue
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, source)
            except OSError as exc:
                messages.append(f"还原失败 {source}：{exc}")
                continue
            messages.append(f"已还原 {source}")
            logger.info("已还原 %s（来自 %s）", source, backup)
        return messages

