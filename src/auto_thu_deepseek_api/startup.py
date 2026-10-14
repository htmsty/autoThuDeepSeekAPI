"""开机启动：写 HKCU 的 Run 项。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from . import constants, paths
from .paths import desktop_dir  # 桌面目录的探测统一放在 paths 里（private 位置校验也要用）

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


def _target_command() -> tuple[str, str, str]:
    """返回 (目标路径, 参数, 工作目录)，供快捷方式使用。"""

    if paths.is_frozen():
        exe = str(Path(sys.executable).resolve())
        return exe, "", str(Path(exe).parent)
    # 源码模式也优先 pythonw：用 python.exe 建出来的快捷方式每次双击都会闪一下控制台
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw.resolve() if pythonw.exists() else Path(sys.executable).resolve()
    script = Path(__file__).resolve().parents[2] / "run_dev.py"
    return str(interpreter), f'"{script}"', str(script.parent)


def is_app_on_desktop() -> bool:
    """程序是不是**直接**放在桌面根目录（桌面下的子文件夹不算）。"""

    desktop = desktop_dir()
    if desktop is None:
        return False
    try:
        return paths.app_dir().resolve() == desktop.resolve()
    except OSError:  # pragma: no cover - 路径解析失败就当不在桌面
        return False


def launch_command(extra: tuple[str, ...] = ()) -> list[str]:
    """重新拉起自己的命令行（重启程序用；源码模式指回 run_dev.py）。"""

    if paths.is_frozen():
        return [str(Path(sys.executable).resolve()), *extra]
    script = Path(__file__).resolve().parents[2] / "run_dev.py"
    return [str(Path(sys.executable).resolve()), str(script), *extra]


def create_desktop_shortcut(name: str = constants.APP_DISPLAY_NAME) -> tuple[bool, str]:
    """在桌面建一个指向本程序的快捷方式（.lnk）。返回 (是否成功, 说明)。"""

    desktop = desktop_dir()
    if desktop is None:
        return False, "找不到桌面目录，无法创建快捷方式"
    safe_name = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or constants.APP_NAME
    shortcut = desktop / f"{safe_name}.lnk"
    exe, arguments, workdir = _target_command()
    if not Path(exe).exists():
        return False, f"程序路径不存在：{exe}"

    # 用 PowerShell 调系统自带的 WScript.Shell 生成 .lnk，避免依赖 pywin32。
    # 各路径通过环境变量传入，免得命令行 / 编码把中文或空格弄坏。
    script = (
        "$shell = New-Object -ComObject WScript.Shell\r\n"
        "$link = $shell.CreateShortcut($env:ATD_LNK)\r\n"
        "$link.TargetPath = $env:ATD_TARGET\r\n"
        "$link.Arguments = $env:ATD_ARGS\r\n"
        "$link.WorkingDirectory = $env:ATD_WORKDIR\r\n"
        "$link.IconLocation = \"$env:ATD_TARGET,0\"\r\n"
        "$link.Save()\r\n"
    )
    env = dict(os.environ)
    env.update(
        {
            "ATD_LNK": str(shortcut),
            "ATD_TARGET": exe,
            "ATD_ARGS": arguments,
            "ATD_WORKDIR": workdir,
        }
    )
    try:
        # 打包版没有控制台，这里也把 PowerShell 的窗口藏掉，免得闪一下黑框
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"创建快捷方式失败：{exc}"

    if completed.returncode != 0 or not shortcut.exists():
        detail = (completed.stderr or completed.stdout or "").strip()
        return False, f"创建快捷方式失败：{detail or 'PowerShell 未生成文件'}"
    return True, f"已在桌面创建快捷方式：{shortcut}"

