"""目标的公共类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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


class SkipTarget(Exception):
    """目标未安装 / 未启用，跳过的理由。"""

