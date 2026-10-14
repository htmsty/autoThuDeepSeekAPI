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

from . import paths

logger = logging.getLogger(__name__)

INDEX_NAME = "index.json"
SOURCE_MARKER = "source.txt"


@dataclass
class BackupEntry:
    label: str
    source: str
    backup: str
    created: str
    sha256: str
    size: int
    # 是否是"原始"备份（程序第一次动这个文件之前的状态）。老索引里没有这个字段，
    # 默认 False，靠快照目录名里的 original 标记也能识别。
    is_original: bool = False


def is_original_label(label: str) -> bool:
    """快照标签是否属于"首份/原始备份"（目录名会带 -original 标记）。"""

    return "original" in (label or "")


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
        self.index_path = self.root / INDEX_NAME
        self._session_snapshot: Path | None = None
        self._session_label = ""
        # 只有 begin_snapshot() 显式开的会话才会被复用；普通的一次备份各占一个目录
        self._session_explicit = False

    # —— 索引 ——

    def _load_index(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("备份索引损坏，稍后从快照目录重建：%s", self.index_path)
            return []
        return raw if isinstance(raw, list) else []

    def _rebuild_index(self) -> list[dict]:
        """索引丢了就从快照目录自愈。

        每个快照目录里都写了 `source.txt` 记录它来自哪个文件（也方便人工翻备份），
        没有这个标记的老快照无法判断该还原到哪，只能先跳过。
        """

        if not self.root.is_dir():
            return []
        rebuilt: list[dict] = []
        for snapshot in sorted(self.root.iterdir()):
            if not snapshot.is_dir():
                continue
            marker = snapshot / SOURCE_MARKER
            try:
                source = marker.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not source:
                continue
            for payload in snapshot.iterdir():
                if not payload.is_file() or payload.name == SOURCE_MARKER:
                    continue
                created = datetime.fromtimestamp(payload.stat().st_mtime).isoformat(timespec="milliseconds")
                rebuilt.append(
                    asdict(
                        BackupEntry(
                            label=snapshot.name,
                            source=source,
                            backup=str(payload),
                            created=created,
                            sha256=_sha256(payload),
                            size=payload.stat().st_size,
                        )
                    )
                )
        if rebuilt:
            logger.warning("已从快照目录重建备份索引，共 %d 条", len(rebuilt))
            try:
                self._save_index(rebuilt)
            except OSError as exc:  # pragma: no cover
                logger.warning("重建后的索引写不进去：%s", exc)
        return rebuilt

    def _save_index(self, entries: list[dict]) -> None:
        # 原子写：索引被写坏会让"一键还原"找不到任何备份
        paths.write_text_atomic(self.index_path, json.dumps(entries, ensure_ascii=False, indent=2) + "\n")

    def entries(self) -> list[BackupEntry]:
        result: list[BackupEntry] = []
        raw = self._load_index()
        if not raw:
            # 索引丢了/读不出来时，从快照目录自愈（只在读取路径做，写备份时不做）
            raw = self._rebuild_index()
        for item in raw:
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

    @staticmethod
    def _snapshot_target(snapshot: Path, base_name: str) -> Path:
        """同一快照目录里给文件挑一个不重名的落点。

        多个 profile 都叫 `cordis.patch.yml`，如果直接按原文件名放进去会互相覆盖，
        「一键还原」就会把 A 的备份还原到 B。这里改成 `名字-2.yml`、`名字-3.yml`。
        """

        target = snapshot / base_name
        if not target.exists():
            return target
        stem = Path(base_name).stem
        suffix = Path(base_name).suffix
        counter = 2
        while True:
            candidate = snapshot / f"{stem}-{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def _new_snapshot_dir(self, label: str, *, reuse: bool = True) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if (
            reuse
            and self._session_explicit
            and self._session_snapshot is not None
            and self._session_label == label
        ):
            # 会话目录可能已经被清理掉（例如连续同名备份触发 prune），确认还在才复用
            if self._session_snapshot.is_dir():
                return self._session_snapshot
            self._session_snapshot = None
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
        self._session_snapshot = path
        self._session_label = label
        self._session_explicit = False
        return path

    def begin_snapshot(self, label: str) -> None:
        """显式开一个快照目录：接下来同一标签的多次备份都放进它（同一批文件）。

        只有显式开过的会话才会复用目录；普通的一次备份（例如每次同步前备份凭据）
        各占一个目录，否则旧备份会堆在同一个目录里永远清不掉。
        """

        self._new_snapshot_dir(label, reuse=False)
        self._session_explicit = True

    def backup_file(self, source: Path, label: str) -> Path | None:
        """普通文件备份；文件不存在返回 None。"""

        source = Path(source)
        if not source.exists():
            return None
        snapshot = self._new_snapshot_dir(label)
        target = self._snapshot_target(snapshot, source.name)
        shutil.copy2(source, target)
        self._write_source_marker(snapshot, source)
        self._record(
            BackupEntry(
                label=label,
                source=str(source),
                backup=str(target),
                created=datetime.now().isoformat(timespec="milliseconds"),
                sha256=_sha256(target),
                size=target.stat().st_size,
                is_original=is_original_label(label),
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
        target = self._snapshot_target(snapshot, source.name)
        self._write_source_marker(snapshot, source)
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
                is_original=is_original_label(label),
            )
        )
        logger.info("已备份数据库 %s → %s", source, target)
        self.prune()
        return target

    @staticmethod
    def _write_source_marker(snapshot: Path, source: Path) -> None:
        try:
            (snapshot / SOURCE_MARKER).write_text(str(source), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 只是方便自愈，写不进去不算失败
            logger.warning("写备份来源标记失败：%s", exc)

    # —— 清理 ——

    @staticmethod
    def _sort_key(item: dict) -> tuple[str, str]:
        """快照目录名带毫秒时间戳，用它排序最稳（created 只有秒级）。"""

        backup_path = Path(item.get("backup", ""))
        return (backup_path.parent.name, item.get("created", ""))

    @staticmethod
    def _is_original(item: dict) -> bool:
        """首份（原始）备份：快照目录名带 original 标记。

        这类备份是"程序第一次动这个文件之前"的状态，永远不参与清理，
        保证「一键还原」随时能回到最初。
        """

        if item.get("is_original"):
            return True
        return Path(item.get("backup", "")).parent.name.endswith("-original")

    @classmethod
    def _oldest(cls, items: list[dict]) -> dict | None:
        """一个源文件里时间最早的那条（= 程序第一次动它之前的状态）。"""

        return min(items, key=cls._sort_key) if items else None

    def prune(self) -> None:
        """每个源文件保留最早的那份备份 + 最近 `keep-1` 份，避免无限增长。

        "最早的那份"就是"程序第一次动这个文件之前"的状态，无论它是否带 original 标记，
        都不参与清理；这样「一键还原」在任何时候都能回到最初。
        """

        entries = self._load_index()
        by_source: dict[str, list[dict]] = {}
        for item in entries:
            by_source.setdefault(item.get("source", ""), []).append(item)

        keep_paths: set[str] = set()
        for items in by_source.values():
            originals = [item for item in items if self._is_original(item)]
            rest = [item for item in items if not self._is_original(item)]
            rest.sort(key=self._sort_key)
            keep_paths.update(item.get("backup", "") for item in originals)
            already = len(originals)
            # 没有 original 标记的老索引：把时间最早的一条当作受保护的原始备份
            if not originals:
                oldest = self._oldest(items)
                if oldest is not None:
                    keep_paths.add(oldest.get("backup", ""))
                    rest = [item for item in rest if item is not oldest]
                    already = 1
            keep_paths.update(item.get("backup", "") for item in rest[-max(0, self.keep - already) :])

        removed_dirs: set[str] = set()
        for item in entries:
            backup_path = item.get("backup", "")
            if backup_path and backup_path not in keep_paths:
                removed_dirs.add(str(Path(backup_path).parent))

        self._save_index([item for item in entries if item.get("backup", "") in keep_paths])

        for directory in removed_dirs:
            path = Path(directory)
            # 正在用的显式会话快照不能删：同一次操作可能还要往它里面写下一份文件
            if self._session_explicit and self._session_snapshot is not None and path == self._session_snapshot:
                continue
            if not (path.is_dir() and path.parent == self.root):
                continue
            try:
                # 清理旧备份也走回收站（和"程序里的删除都进回收站"保持一致）
                ok, detail = paths.delete_path(path)
            except OSError as exc:  # pragma: no cover
                ok, detail = False, str(exc)
            if ok:
                logger.info("已清理旧备份 %s（%s）", path, detail)
            else:
                logger.warning("清理旧备份失败：%s（%s）", path, detail)

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

