"""路径解析：程序目录、private 运行数据目录、各目标的默认位置。

原则：不写死任何机器相关的位置。能自动探测的自动探测，探测不到的交由用户在设置里指定。

private（运行数据）的位置可以改，规则见 `data_root()` 与 `switch_private_root()`：
选定的位置记在 `%LOCALAPPDATA%\\autoThuDeepSeekAPI\\private-location.json`（"指针文件"）里。
指针不能放在 private 自己里面（换了位置就找不回来了），也尽量不在桌面留文件；
「干净卸载」会把它一起删掉。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import constants

logger = logging.getLogger(__name__)

# 每次调用都去探测一次可写性太费（共享目录上尤其慢），而且两个实例同时探测时
# 同名探针文件可能互相踩到，导致误判"不可写"、数据被分流到 LOCALAPPDATA。这里缓存一次。
_DATA_ROOT_CACHE: Path | None = None

# private 目录里由程序管理的条目（换位置要搬、卸载要删的都是这些）
MANAGED_ENTRIES: tuple[str, ...] = (
    "settings.json",
    "state.json",
    "model-cache.json",
    "app.lock",
    "logs",
    "backups",
    "browser-profile",
)

POINTER_FILE_NAME = "private-location.json"

# 不允许当 private 的位置：系统目录、程序目录、盘符根、临时目录、桌面。
# 这些地方要么要管理员权限、要么容易被安全软件盯上，要么正是我们要避开的桌面。
_UNSAFE_PATH_PARTS = (
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "system32",
    "syswow64",
    "$recycle.bin",
    "system volume information",
    "recovery",
    "perflogs",
)


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


@dataclass(frozen=True)
class PrivateLocation:
    """指针文件里记的内容：private 现在在哪、以及上一次是从哪搬过来的。"""

    path: Path
    migrated_from: Path | None = None


def local_app_dir() -> Path:
    """`%LOCALAPPDATA%\\autoThuDeepSeekAPI\\`：放位置指针的地方（不在桌面、不在 private 里）。"""

    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / constants.APP_NAME


def private_pointer_file() -> Path:
    return local_app_dir() / POINTER_FILE_NAME


def read_private_override() -> PrivateLocation | None:
    """读位置指针；没有/读坏了都返回 None（退回默认位置）。"""

    path = private_pointer_file()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("private 位置指针读不出来，按默认位置处理：%s（%s）", path, exc)
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("path")
    if not isinstance(value, str) or not value.strip():
        return None
    migrated = raw.get("migrated_from")
    return PrivateLocation(
        path=Path(value).expanduser(),
        migrated_from=Path(migrated).expanduser() if isinstance(migrated, str) and migrated.strip() else None,
    )


def write_private_override(path: Path, migrated_from: Path | None = None) -> None:
    payload: dict[str, str] = {"path": str(path)}
    if migrated_from is not None:
        payload["migrated_from"] = str(migrated_from)
    write_text_atomic(private_pointer_file(), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    reset_data_root_cache()


def clear_private_override() -> None:
    """把位置指针删掉（恢复默认位置 / 干净卸载时用）。"""

    try:
        private_pointer_file().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - 删不掉不算致命
        logger.warning("删除 private 位置指针失败：%s", exc)
    reset_data_root_cache()


def desktop_dir() -> Path | None:
    """桌面目录：优先问 Windows 的 Shell，失败再退回常见路径。"""

    try:
        import winreg

        key_name = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
            expanded = Path(os.path.expandvars(str(Path(str(value)).expanduser())))
            if expanded.is_dir():
                return expanded
    except (ImportError, OSError):
        pass
    for candidate in (Path.home() / "Desktop", Path.home() / "桌面"):
        if candidate.is_dir():
            return candidate
    return None


def default_private_dir() -> Path:
    """默认的 private 位置：程序目录下（不可写时由 data_root() 退回 LOCALAPPDATA）。"""

    return app_dir() / "private"


def validate_private_dir(candidate: Path | str) -> tuple[bool, str]:
    """检查某个目录能不能当 private 用。

    拒绝：系统目录（Windows / System32）、Program Files、ProgramData、盘符根、
    临时目录、桌面；要求路径可写。返回 (是否可用, 说明)。
    """

    if not str(candidate).strip():
        return False, "位置不能为空"
    try:
        path = Path(os.path.expandvars(str(candidate))).expanduser()
        resolved = path.resolve()
    except (OSError, ValueError) as exc:
        return False, f"路径不合法：{exc}"

    if resolved.exists() and not resolved.is_dir():
        return False, f"这个位置是个文件，不是文件夹：{resolved}"
    if resolved.parent == resolved:
        return False, "不能直接用盘符根目录（例如 D:\\），请选一个子文件夹"

    lowered = [part.lower() for part in resolved.parts]
    for bad in _UNSAFE_PATH_PARTS:
        if bad in lowered:
            return False, f"这个位置属于系统目录、需要管理员权限，换个地方更安全：{resolved}"

    try:
        temp_root = Path(tempfile.gettempdir()).resolve()
    except OSError:  # pragma: no cover
        temp_root = Path(tempfile.gettempdir())
    if resolved == temp_root or temp_root in resolved.parents:
        return False, "不能放在临时目录里（随时会被系统清掉），换个地方"

    desktop = desktop_dir()
    if desktop is not None:
        try:
            desktop_resolved = desktop.resolve()
        except OSError:  # pragma: no cover
            desktop_resolved = desktop
        if resolved == desktop_resolved or desktop_resolved in resolved.parents:
            return False, "桌面正是要避开的位置（容易被误删/被安全软件关注），请另选一个文件夹"

    if not _is_writable(resolved):
        return False, f"这个位置写不进去（可能需要管理员权限，或盘符不可用）：{resolved}"
    return True, f"可以放在这里：{resolved}"


def data_root() -> Path:
    """运行数据根目录。

    顺序：位置指针指定的目录 → 程序目录下的 private/ → LOCALAPPDATA → 临时目录兜底。
    """

    global _DATA_ROOT_CACHE
    if _DATA_ROOT_CACHE is not None:
        return _DATA_ROOT_CACHE

    override = read_private_override()
    if override is not None:
        if _is_writable(override.path):
            _DATA_ROOT_CACHE = override.path
            return override.path
        logger.warning("位置指针指向的目录不可用，先退回默认位置：%s", override.path)

    portable = default_private_dir()
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


def managed_paths(root: Path | None = None) -> list[Path]:
    """private 目录里由程序管理的条目（别的文件不属于我们，永远不碰）。"""

    base = Path(root) if root is not None else data_root()
    return [base / name for name in MANAGED_ENTRIES]


def remove_managed_data(root: Path, *, keep: tuple[str, ...] = ()) -> list[str]:
    """删掉 private 里由程序管理的条目，返回说明行。

    只删顶层这些条目，用户自己放进来的文件一律不动；删完目录还剩下东西就留着，
    不整个删目录（「干净卸载」也是这个口径）。
    """

    messages: list[str] = []
    base = Path(root)
    for name in MANAGED_ENTRIES:
        if name in keep:
            continue
        target = base / name
        if not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            messages.append(f"已删除 {target}")
        except OSError as exc:
            messages.append(f"没删掉 {target}：{exc}")
    # 目录空了就顺手删掉；还留着用户的东西就保持原样
    try:
        if base.is_dir() and not any(base.iterdir()):
            base.rmdir()
            messages.append(f"已删除空目录 {base}")
    except OSError:  # pragma: no cover - 删不掉就留着
        pass
    return messages


def switch_private_root(new_root: Path | str) -> tuple[bool, str]:
    """把运行数据换到新位置：拷过去 → 写指针 → 尽力清理旧位置。

    重启后生效；旧位置里正在被占用的文件（日志、锁）删不掉也没关系，
    指针里记着 `migrated_from`，下次启动会再补一次清理。
    """

    ok, message = validate_private_dir(new_root)
    if not ok:
        return False, message
    try:
        target = Path(os.path.expandvars(str(new_root))).expanduser().resolve()
    except (OSError, ValueError) as exc:
        return False, f"路径不合法：{exc}"

    old = data_root()
    if old == target:
        return False, f"现在就是用这个位置：{target}"
    old_resolved = old.resolve()
    if target == old_resolved or old_resolved in target.parents or target in old_resolved.parents:
        return False, "新旧位置不能互相包含，请选一个独立的文件夹"

    target.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    failed: list[str] = []
    for name in MANAGED_ENTRIES:
        source = old / name
        if not source.exists():
            continue
        destination = target / name
        try:
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
            copied.append(name)
        except OSError as exc:
            failed.append(f"{name}（{exc}）")

    if failed and not copied:
        return False, "搬运失败：" + "、".join(failed)

    write_private_override(target, migrated_from=old)
    cleanup_messages = remove_managed_data(old, keep=("app.lock", "logs"))
    detail = f"已把运行数据搬到 {target}"
    if failed:
        detail += f"；这几项没搬成功：{'、'.join(failed)}"
    if cleanup_messages:
        detail += "；旧位置已清理"
    detail += "。重启程序后生效。"
    logger.info("private 位置已切换：%s → %s（旧位置清理 %d 项）", old, target, len(cleanup_messages))
    return True, detail


def finish_pending_migration() -> None:
    """启动时把上次换位置留下的旧数据补删一遍（删不掉就算了，不影响使用）。"""

    override = read_private_override()
    if override is None or override.migrated_from is None:
        return
    old = override.migrated_from
    current = data_root()
    if old == current or not old.is_dir():
        write_private_override(override.path, migrated_from=None)
        return
    # 到了这里程序已经在用新位置，旧位置的日志/锁也不会再被占用，可以全删
    messages = remove_managed_data(old)
    logger.info("补清理旧 private 位置 %s：%d 项", old, len(messages))
    write_private_override(override.path, migrated_from=None)


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

