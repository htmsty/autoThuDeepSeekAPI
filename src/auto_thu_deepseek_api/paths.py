"""路径解析：程序目录、private 运行数据目录、各目标的默认位置。

原则：不写死任何机器相关的位置。能自动探测的自动探测，探测不到的交由用户在设置里指定。
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import constants

# 每次调用都去探测一次可写性太费（共享目录上尤其慢），而且两个实例同时探测时
# 同名探针文件可能互相踩到，导致误判"不可写"、数据被分流到 LOCALAPPDATA。这里缓存一次。
_DATA_ROOT_CACHE: Path | None = None


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
        probe = path / f".write-probe-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def data_root() -> Path:
    """运行数据根目录：优先程序目录下的 private/，不可写时退回 LOCALAPPDATA。"""

    global _DATA_ROOT_CACHE
    if _DATA_ROOT_CACHE is not None:
        return _DATA_ROOT_CACHE

    portable = app_dir() / "private"
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / constants.APP_NAME / "private"
    for candidate in (portable, local):
        if _is_writable(candidate):
            _DATA_ROOT_CACHE = candidate
            return candidate

    # 最后兜底到临时目录：至少让程序跑得起来，而不是启动即崩。
    last_resort = Path(tempfile.gettempdir()) / constants.APP_NAME / "private"
    last_resort.mkdir(parents=True, exist_ok=True)
    _DATA_ROOT_CACHE = last_resort
    return last_resort


def reset_data_root_cache() -> None:
    """仅供测试：清掉缓存，让下一次重新探测。"""

    global _DATA_ROOT_CACHE
    _DATA_ROOT_CACHE = None


def write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    """先写 .tmp 再替换，避免写到一半崩掉把原文件弄成半截。

    目标被别的进程占着（Windows 上 os.replace 会报错）时退回直接写，宁可牺牲原子性也不能不写。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    try:
        os.replace(tmp, path)
    except OSError:
        path.write_text(text, encoding=encoding)
        try:
            tmp.unlink()
        except OSError:
            pass


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

