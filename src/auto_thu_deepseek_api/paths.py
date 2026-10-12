"""路径解析：程序目录、private 运行数据目录、各目标的默认位置。

原则：不写死任何机器相关的位置。能自动探测的自动探测，探测不到的交由用户在设置里指定。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import constants


def is_frozen() -> bool:
    """是否是打包后的程序。"""

    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序所在目录（打包后是 exe 所在目录，源码运行时是项目根目录）。"""

    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_root() -> Path:
    """运行数据根目录：优先程序目录下的 private/，不可写时退回 LOCALAPPDATA。"""

    portable = app_dir() / "private"
    if _is_writable(portable):
        return portable
    fallback = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / constants.APP_NAME / "private"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def logs_dir() -> Path:
    return data_root() / "logs"


def backups_dir() -> Path:
    return data_root() / "backups"


def browser_profile_dir() -> Path:
    return data_root() / "browser-profile"


def settings_file() -> Path:
    return data_root() / "settings.json"


def state_file() -> Path:
    return data_root() / "state.json"


def ensure_runtime_dirs() -> None:
    for path in (logs_dir(), backups_dir(), browser_profile_dir()):
        path.mkdir(parents=True, exist_ok=True)


def assets_dir() -> Path:
    """资源目录（打包后从解包目录取）。"""

    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", str(app_dir())))
        return base / "assets"
    return app_dir() / "assets"


def asset(name: str) -> Path:
    return assets_dir() / name


def home_dir() -> Path:
    return Path.home()


@dataclass(frozen=True)
class TargetPaths:
    """一个目标的实际路径（默认值或用户自定义值）。"""

    base_dir: Path
    credentials_file: Path | None = None
    profiles_dir: Path | None = None
    db_file: Path | None = None
    settings_file: Path | None = None
    live_state_file: Path | None = None


def default_dsh_dir() -> Path:
    return home_dir() / constants.DSH_DIR_NAME


def default_ccswitch_dir() -> Path:
    return home_dir() / constants.CCSWITCH_DIR_NAME


def resolve_dsh_paths(custom_dir: str | None) -> TargetPaths:
    base = Path(custom_dir).expanduser() if custom_dir else default_dsh_dir()
    return TargetPaths(
        base_dir=base,
        credentials_file=base / constants.DSH_CREDENTIAL_FILE,
        profiles_dir=base / constants.DSH_PROFILES_DIR,
    )


def resolve_ccswitch_paths(custom_dir: str | None) -> TargetPaths:
    base = Path(custom_dir).expanduser() if custom_dir else default_ccswitch_dir()
    return TargetPaths(
        base_dir=base,
        db_file=base / constants.CCSWITCH_DB_FILE,
        settings_file=base / constants.CCSWITCH_SETTINGS_FILE,
        live_state_file=base / constants.CCSWITCH_LIVE_STATE_FILE,
    )

