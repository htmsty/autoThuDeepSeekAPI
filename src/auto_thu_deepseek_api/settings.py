"""用户设置：读写 private/settings.json。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

from . import paths
from .constants import DEFAULT_SYNC_INTERVAL_MINUTES

CCSWITCH_POLICY_PROMPT = "prompt"
CCSWITCH_POLICY_DIRECT = "direct"

# 设置文件版本。
#   v2：默认勾选「统一所有 profile 到 madmodel」。
#   v3：默认开启自动同步、间隔 180 分钟，并默认不弹「CC Switch 正在运行」提醒。
SETTINGS_VERSION = 3

# 「恢复默认设置」时**不**动的字段：它们不是"设置项"，而是运行状态或系统状态。
RESET_KEEP_FIELDS = (
    "settings_version",
    "launch_at_startup",  # 开机启动写在注册表里，恢复默认不该动系统
    "last_sync_at",
    "last_sync_summary",
)


@dataclass
class AppSettings:
    """所有可调项。空字符串表示"自动探测"。"""

    settings_version: int = SETTINGS_VERSION

    # 同步
    sync_interval_minutes: int = DEFAULT_SYNC_INTERVAL_MINUTES
    auto_sync_enabled: bool = True
    sync_on_start: bool = True
    minimize_after_sync: bool = False
    launch_at_startup: bool = False

    # 取 Token
    silent_capture_timeout_seconds: int = 40

    # 写入前预检（把"将要写入的内容"先渲染出来校验，避免写坏目标文件）
    preflight_enabled: bool = True

    # DSH 细节
    default_model_id: str = ""  # 空 = 用站点清单里的第一个

    # 界面
    close_to_tray: bool = True
    log_level: str = "INFO"

    # 目标开关（检测到哪个写哪个；可分别关闭）
    enable_dsh: bool = True
    enable_ccswitch: bool = True

    # 自定义路径（留空 = 自动探测）
    dsh_dir: str = ""
    ccswitch_dir: str = ""

    # DSH 细节
    unify_all_profiles: bool = True

    # CC Switch 细节
    ccswitch_running_policy: str = CCSWITCH_POLICY_PROMPT
    suppress_ccswitch_running_warning: bool = True
    # 「顺带刷新同站点旧卡片」会改动别的卡片，默认关（要的用户自己开）
    ccswitch_refresh_sibling_cards: bool = False

    # 备份
    backup_keep: int = 10

    # 最近一次成功同步时间（ISO 字符串，仅供界面显示）
    last_sync_at: str = ""
    last_sync_summary: str = ""

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
        if not isinstance(raw, dict):
            # 合法 JSON 但不是对象（手改成数组/字符串、别处写坏都算）：
            # 按"没有设置"处理，绝不让加载过程把程序带崩。
            logging.getLogger(__name__).warning(
                "设置文件结构不是 JSON 对象，按默认设置处理：%s", path
            )
            return cls()
        version = raw.get("settings_version")
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = 1
        known = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in raw.items() if k in known}
        settings = cls(**data)
        migrated = False
        if version < 2:
            # v1 → v2：默认勾选「统一所有 profile 到 madmodel」。只迁一次，
            # 之后再手动取消勾选就不会被打回。
            settings.unify_all_profiles = True
            migrated = True
            logging.getLogger(__name__).info(
                "设置已升级到 v%d：默认勾选「统一所有 profile 到 madmodel」", SETTINGS_VERSION
            )
        if version < 3:
            # v2 → v3：默认开启自动同步，并默认不弹 CC Switch 运行中提醒。
            # 间隔默认值已在 dataclass 里改成 180；用户自己填过的分钟数保留，不覆盖。
            settings.auto_sync_enabled = True
            settings.suppress_ccswitch_running_warning = True
            migrated = True
            logging.getLogger(__name__).info(
                "设置已升级到 v%d：默认开启自动同步，不再提示 CC Switch 正在运行（当前间隔 %d 分钟）",
                SETTINGS_VERSION,
                settings.sync_interval_minutes,
            )
        if version < SETTINGS_VERSION:
            settings.settings_version = SETTINGS_VERSION
            if migrated:
                try:
                    settings.save()
                except OSError as exc:  # pragma: no cover - 目录不可写时别让启动崩
                    logging.getLogger(__name__).warning("保存迁移后的设置失败：%s", exc)
        settings.normalize()
        return settings

    def save(self) -> None:
        self.normalize()
        path = paths.settings_file()
        payload = asdict(self)
        paths.write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

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

        try:
            self.silent_capture_timeout_seconds = int(self.silent_capture_timeout_seconds)
        except (TypeError, ValueError):
            self.silent_capture_timeout_seconds = 40
        self.silent_capture_timeout_seconds = max(10, min(self.silent_capture_timeout_seconds, 600))

        if self.log_level not in ("INFO", "DEBUG"):
            self.log_level = "INFO"

        self.default_model_id = (self.default_model_id or "").strip()
        self.dsh_dir = (self.dsh_dir or "").strip()
        self.ccswitch_dir = (self.ccswitch_dir or "").strip()


def reset_to_defaults(settings: AppSettings) -> AppSettings:
    """把设置恢复成默认值（保留开机启动与最近同步记录），返回同一个对象。"""

    defaults = AppSettings()
    for name in settings.__dataclass_fields__:
        if name in RESET_KEEP_FIELDS:
            continue
        setattr(settings, name, getattr(defaults, name))
    settings.settings_version = SETTINGS_VERSION
    return settings

