"""后台线程：同步写入不阻塞界面。"""

from __future__ import annotations

import copy
import logging

from PySide6.QtCore import QThread, Signal

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

    def run(self) -> None:  # noqa: D102
        try:
            report: SyncReport = run_sync(self._settings, self._token, verify=self._verify)
        except Exception as exc:  # noqa: BLE001 - 线程里必须兜住所有异常
            logger.exception("同步失败")
            self.failed.emit(str(exc))
            return
        self.reportReady.emit(report)

