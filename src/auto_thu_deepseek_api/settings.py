"""用户设置：读写 private/settings.json。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from . import paths
from .constants import DEFAULT_SYNC_INTERVAL_MINUTES

CCSWITCH_POLICY_PROMPT = "prompt"
CCSWITCH_POLICY_DIRECT = "direct"


@dataclass
class AppSettings:
    """所有可调项。空字符串表示"自动探测"。"""

    # 同步
    sync_interval_minutes: int = DEFAULT_SYNC_INTERVAL_MINUTES
    auto_sync_enabled: bool = False
    sync_on_start: bool = True
    minimize_after_sync: bool = False
    launch_at_startup: bool = False

    # 目标开关（检测到哪个写哪个；可分别关闭）
    enable_dsh: bool = True
    enable_ccswitch: bool = True

    # 自定义路径（留空 = 自动探测）
    dsh_dir: str = ""
    ccswitch_dir: str = ""

    # DSH 细节
    unify_all_profiles: bool = False

    # CC Switch 细节
    ccswitch_running_policy: str = CCSWITCH_POLICY_PROMPT
    suppress_ccswitch_running_warning: bool = False

    # 备份
    backup_keep: int = 10

    # 最近一次成功同步时间（ISO 字符串，仅供界面显示）
    last_sync_at: str = ""
    last_sync_summary: str = ""
    extra: dict = field(default_factory=dict)

    # —— 读写 ——

    @classmethod
    def load(cls) -> "AppSettings":
        path = paths.settings_file()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in raw.items() if k in known}
        settings = cls(**data)
        settings.normalize()
        return settings

    def save(self) -> None:
        self.normalize()
        path = paths.settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def normalize(self) -> None:
        try:
            self.sync_interval_minutes = int(self.sync_interval_minutes)
        except (TypeError, ValueError):
            self.sync_interval_minutes = DEFAULT_SYNC_INTERVAL_MINUTES
        self.sync_interval_minutes = max(1, min(self.sync_interval_minutes, 24 * 60))

        try:
            self.backup_keep = int(self.backup_keep)
        except (TypeError, ValueError):
            self.backup_keep = 10
        self.backup_keep = max(1, min(self.backup_keep, 200))

        if self.ccswitch_running_policy not in (CCSWITCH_POLICY_PROMPT, CCSWITCH_POLICY_DIRECT):
            self.ccswitch_running_policy = CCSWITCH_POLICY_PROMPT

        self.dsh_dir = (self.dsh_dir or "").strip()
        self.ccswitch_dir = (self.ccswitch_dir or "").strip()

