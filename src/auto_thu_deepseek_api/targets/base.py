"""目标的公共类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..paths import write_text_atomic


@dataclass
class RenderedFile:
    """干跑（dry-run）时"本来要写出去"的内容。

    预检（preflight）与实际写盘共用同一套渲染代码，靠它拿到将要写入的文本再校验，
    因此"预检通过的内容"就是"真正会写入的内容"。
    """

    path: Path
    text: str
    label: str
    kind: str = "text"  # yaml / json / sqlite-json


def emit(target, path: Path, text: str, label: str, kind: str = "text") -> None:
    """记录一份将要写出的内容；干跑模式下到此为止。"""

    target.rendered.append(RenderedFile(path=Path(path), text=text, label=label, kind=kind))


def write_with_rollback(path: Path, text: str, backup, label: str) -> tuple[bool, str]:
    """先备份、再原子写；万一写坏了就把刚备份的那份放回去。

    返回 (是否成功, 说明)。BS 是 BackupManager，这里不写死类型注解以免和 backup 模块绕环。
    """

    backup_path: Path | None = None
    try:
        backup_path = backup.backup_file(path, label)
    except OSError as exc:
        return False, f"备份失败，未写入：{exc}"

    try:
        write_text_atomic(path, text)
    except OSError as exc:
        if backup_path and Path(backup_path).exists():
            try:
                path.write_text(Path(backup_path).read_text(encoding="utf-8"), encoding="utf-8")
                return False, f"写入失败，已回滚到备份：{exc}"
            except OSError as restore_exc:
                return False, f"写入失败且回滚失败：{exc}；回滚错误：{restore_exc}"
        return False, f"写入失败：{exc}"
    return True, ""


@dataclass
class TargetStatus:
    key: str
    label: str
    detected: bool
    base_dir: Path | None = None
    detail: str = ""


@dataclass
class ApplyResult:
    key: str
    label: str
    ok: bool = True
    changed: bool = False
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.messages.append(message)

    def fail(self, message: str) -> None:
        self.errors.append(message)
        self.ok = False

