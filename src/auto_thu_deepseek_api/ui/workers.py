"""后台线程：同步写入不阻塞界面。"""

from __future__ import annotations

import copy
import logging
import threading

from PySide6.QtCore import QThread, Signal

from ..preflight import run_preflight
from ..settings import AppSettings
from ..sync import SyncReport, run_sync

logger = logging.getLogger(__name__)


class SyncWorker(QThread):
    """执行一次同步。"""

    reportReady = Signal(object)
    failed = Signal(str)

    def __init__(self, settings: AppSettings, token: str, parent=None, verify: bool = True) -> None:
        super().__init__(parent)
        self._settings = copy.deepcopy(settings)
        self._token = token
        self._verify = verify
        # 「强行终止并重试」用：给同步流程一个信号，让它在步骤之间安全停下
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        """请同步流程停下来（不是杀线程：正在写的那一份会先写完）。"""

        self._cancel.set()

    def run(self) -> None:  # noqa: D102
        try:
            report: SyncReport = run_sync(
                self._settings, self._token, verify=self._verify, cancel_event=self._cancel
            )
        except Exception as exc:  # noqa: BLE001 - 线程里必须兜住所有异常
            logger.exception("同步失败")
            self.failed.emit(str(exc))
            return
        self.reportReady.emit(report)


class PreflightWorker(QThread):
    """后台跑「校验写入内容」。

    预检本身是只读的，但读模型清单那一步可能要联网（站点前端包最大 8 MB），
    放在主线程里会把界面卡住几十秒 —— 所以和同步一样丢到后台线程。
    """

    reportReady = Signal(object)
    failed = Signal(str)

    def __init__(self, settings: AppSettings, token: str, parent=None) -> None:
        super().__init__(parent)
        self._settings = copy.deepcopy(settings)
        self._token = token

    def run(self) -> None:  # noqa: D102
        try:
            report = run_preflight(self._settings, self._token)
        except Exception as exc:  # noqa: BLE001 - 线程里必须兜住所有异常
            logger.exception("预检失败")
            self.failed.emit(str(exc))
            return
        self.reportReady.emit(report)

