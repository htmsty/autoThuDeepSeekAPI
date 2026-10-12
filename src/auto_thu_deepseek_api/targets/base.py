"""目标的公共类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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

