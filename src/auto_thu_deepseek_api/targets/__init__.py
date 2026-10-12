"""写入目标：DSH 与 CC Switch。"""

from __future__ import annotations

from .base import ApplyResult, TargetStatus
from .ccswitch import CcSwitchTarget
from .dsh import DshTarget

__all__ = ["ApplyResult", "TargetStatus", "CcSwitchTarget", "DshTarget"]

