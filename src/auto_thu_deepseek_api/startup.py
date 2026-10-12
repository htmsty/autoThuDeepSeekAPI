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
    script = Path(__file__).resolve().parents[1] / "run_dev.py"
    return f'"{interpreter}" "{script}" --minimized'


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

