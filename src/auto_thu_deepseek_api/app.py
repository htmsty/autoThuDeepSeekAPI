"""程序入口：组装日志、设置、主窗口与定时器。"""

from __future__ import annotations

import logging
import subprocess
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import constants, data_location, instance, paths, startup
from .logging_setup import setup_logging
from .settings import AppSettings
from .state_store import AppState
from .ui.icon import app_icon
from .ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def _lock_wait_seconds(argv: list[str]) -> float:
    """等单实例锁的秒数。

    自己拉起来的"重启"要多等一会儿（旧进程可能还在退）；用户正常双击启动就少等，
    免得"程序已经在运行"要愣 6 秒才提示。
    """

    return 6.0 if "--restarted" in argv else 0.6


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    start_minimized = "--minimized" in argv

    if "--selftest" in argv:
        from .selftest import main as selftest_main

        return selftest_main([arg for arg in argv if arg != "--selftest"])

    QApplication.setApplicationName(constants.APP_NAME)
    QApplication.setApplicationDisplayName(constants.APP_DISPLAY_NAME)
    QApplication.setOrganizationName(constants.APP_AUTHOR)

    application = QApplication(argv)
    application.setWindowIcon(app_icon())
    application.setQuitOnLastWindowClosed(False)

    # —— 启动早期：先处理"程序在桌面"这条提醒，**别先在桌面把 private 建出来** ——
    # 这一步之前不能调用任何会创建 private 的东西（日志、锁文件都算）。
    # 这段发生在日志与单实例锁之前，任何异常都不能让程序起不来。
    # 注意：提醒本身也要在"延迟创建"状态下弹——提醒文案里会取 data_root()，
    # 那个调用一旦会建目录，就又变成"还没问用户，桌面上已经多出 private 了"。
    early_state: AppState | None = None
    on_desktop = False
    paths.defer_creation(True)
    try:
        try:
            early_state = AppState.load()  # 只读：目录不存在时就是默认值
            on_desktop = (
                startup.is_app_on_desktop()
                and paths.read_private_override() is None
                and not early_state.desktop_note_ack
            )
        except Exception as exc:  # noqa: BLE001 - 启动早期不冒险
            logger.warning("启动早期检查失败（按「不在桌面」处理）：%s", exc)
            on_desktop = False
        if on_desktop and early_state is not None:
            logger.info("程序在桌面根目录，先提醒用户再决定 private 放哪")
            if not data_location.desktop_notice(None, early_state):
                logger.info("用户选择退出程序去换本程序位置，本次不启动")
                return 0
    finally:
        paths.defer_creation(False)

    paths.ensure_runtime_dirs()
    # 上次换过 private 位置的话，把旧位置里的残留补删一下（删不掉不影响使用）
    try:
        paths.finish_pending_migration()
    except Exception as exc:  # noqa: BLE001
        logger.warning("补清理旧 private 位置失败：%s", exc)
    setup_logging()

    if not instance.acquire_with_retry(_lock_wait_seconds(argv)):
        error_name = instance.last_error()
        if error_name and error_name != "LockFailedError":
            # 不是"有别的实例"，而是锁文件建不出来（目录只读、被占用等）：
            # 这种情况说"程序已经在运行"会误导用户。
            logger.error("获取运行锁失败（%s），运行数据目录：%s", error_name, paths.data_root())
            if not start_minimized and QSystemTrayIcon.isSystemTrayAvailable():
                QMessageBox.warning(
                    None,
                    constants.APP_DISPLAY_NAME,
                    f"无法在运行数据目录建立锁文件（{error_name}）。\n"
                    f"请检查这个目录是否可写：\n{paths.data_root()}",
                )
            return 0
        logger.info("程序已经在运行，退出本次启动")
        # 开机启动（--minimized）是后台拉起，别弹窗打扰用户
        if not start_minimized and QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.information(None, constants.APP_DISPLAY_NAME, "程序已经在运行（请看系统托盘）。")
        return 0

    settings = AppSettings.load()
    state = AppState.load()
    if settings.log_level == "DEBUG":
        setup_logging(level=logging.DEBUG)
        logger.info("日志级别已切到 DEBUG")

    if not QSystemTrayIcon.isSystemTrayAvailable():
        logger.warning("系统托盘不可用：关闭窗口会直接退出程序")

    window = MainWindow(settings, state)

    # 首次运行自动留一份原始配置，且这份备份不会被清理逻辑删掉，
    # 用户随时能在设置里"一键还原"到程序第一次动它之前的状态。
    try:
        from .backup_ops import auto_backup_on_first_run

        auto_backup_on_first_run(settings)
    except Exception as exc:  # noqa: BLE001 - 首次备份失败不该拖垮启动
        logger.warning("首次运行自动备份失败：%s", exc)

    if start_minimized:
        logger.info("以最小化方式启动")
    else:
        window.show()

    # 启动检查（程序在桌面 / 两个目标都没装）走完再决定要不要同步；
    # 顺序固定：先"程序在桌面"，再"都没装"。
    QTimer.singleShot(300, window.startup_then_sync)
    logger.info("启动检查与自动同步将在窗口显示后开始")

    logger.info("%s %s 已启动（作者：%s）", constants.APP_DISPLAY_NAME, constants.APP_VERSION, constants.APP_AUTHOR)
    code = application.exec()

    if window.restart_requested:
        try:
            command = startup.launch_command(extra=("--restarted",))
            subprocess.Popen(command, cwd=str(paths.app_dir()))  # noqa: S603 - 拉起自己
            logger.info("已按你的要求重新启动程序")
        except OSError as exc:
            logger.warning("自动重启失败，请手动打开程序：%s", exc)
    return code

