"""开机启动：写 HKCU 的 Run 项。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import constants, paths

logger = logging.getLogger(__name__)


def _command() -> str:
    """打包后指向 exe；源码运行时指向 pythonw + 模块入口。"""

    if paths.is_frozen():
        return f'"{Path(sys.executable).resolve()}" --minimized'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    # parents[0]=包目录, [1]=src, [2]=仓库根；run_dev.py 在仓库根
    script = Path(__file__).resolve().parents[2] / "run_dev.py"
    return f'"{interpreter}" "{script}" --minimized'


def current_command() -> str | None:
    """读出注册表里记的命令；没有就返回 None。"""

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, constants.STARTUP_REGISTRY_KEY) as key:
            value, _ = winreg.QueryValueEx(key, constants.STARTUP_VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError as exc:  # pragma: no cover
        logger.warning("读取开机启动命令失败：%s", exc)
        return None


def sync_command() -> str:
    """开机启动项存在、但指向的路径已经不是当前这份程序时，改写成当前路径。

    便携程序被搬到别的目录（或共享文件夹挂载点变了）以后，注册表里那条旧路径就废了，
    这里顺手纠正，不用用户再手动开关一次。
    """

    existing = current_command()
    if not existing:
        return ""
    wanted = _command()
    if existing == wanted:
        return ""
    ok, message = set_enabled(True)
    if ok:
        logger.info("开机启动项已更新为新路径：%s", wanted)
        return wanted
    logger.warning("更新开机启动项失败：%s", message)
    return ""


def is_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, constants.STARTUP_REGISTRY_KEY) as key:
            value, _ = winreg.QueryValueEx(key, constants.STARTUP_VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover - 注册表不可用时忽略
        logger.warning("读取开机启动项失败：%s", exc)
        return False


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """返回 (是否成功, 说明)。"""

    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, constants.STARTUP_REGISTRY_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                command = _command()
                winreg.SetValueEx(key, constants.STARTUP_VALUE_NAME, 0, winreg.REG_SZ, command)
                return True, f"已设置开机启动：{command}"
            try:
                winreg.DeleteValue(key, constants.STARTUP_VALUE_NAME)
            except FileNotFoundError:
                pass
            return True, "已取消开机启动"
    except OSError as exc:
        return False, f"设置开机启动失败：{exc}"

