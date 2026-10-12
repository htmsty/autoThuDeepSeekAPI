"""程序入口：组装日志、设置、主窗口与定时器。"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import constants, paths
from .logging_setup import setup_logging
from .settings import AppSettings
from .state_store import AppState
from .ui.icon import app_icon
from .ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def _single_instance_guard() -> QLockFile | None:
    lock_path = paths.data_root() / "app.lock"
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        return None
    return lock


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

    paths.ensure_runtime_dirs()
    setup_logging()

    lock = _single_instance_guard()
    if lock is None:
        logger.info("程序已经在运行，退出本次启动")
        # 开机启动（--minimized）是后台拉起，别弹窗打扰用户
        if not start_minimized and QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.information(None, constants.APP_DISPLAY_NAME, "程序已经在运行（请看系统托盘）。")
        return 0

    settings = AppSettings.load()
    state = AppState.load()

    if not QSystemTrayIcon.isSystemTrayAvailable():
        logger.warning("系统托盘不可用：关闭窗口会直接退出程序")

    window = MainWindow(settings, state)
    if start_minimized:
        logger.info("以最小化方式启动")
    else:
        window.show()

    if settings.sync_on_start:
        QTimer.singleShot(1200, lambda: window.start_sync(interactive=False))
        logger.info("启动后 1.2 秒自动同步一次")

    logger.info("%s %s 已启动（作者：%s）", constants.APP_DISPLAY_NAME, constants.APP_VERSION, constants.APP_AUTHOR)
    return application.exec()

