"""后台线程：同步写入不阻塞界面。"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

from .. import paths
from ..backup_ops import auto_backup_on_first_run
from ..preflight import run_preflight
from ..settings import AppSettings
from ..sync import SyncReport, both_targets_missing, detect_targets, run_sync
from ..targets.base import TargetStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartupProbeResult:
    """后台启动检查的结果。"""

    statuses: list[TargetStatus]
    targets_missing: bool
    traces: dict[str, str]
    backups_done: int = 0
    warnings: tuple[str, ...] = ()
    interrupted: bool = False


class StartupProbeWorker(QThread):
    """窗口显示后再做首次备份、ACL 收紧与目标探测。

    启动同步必须等这个线程结束：先备份原始配置再写，顺序不能为了启动速度反过来。
    """

    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self._settings = copy.deepcopy(settings)
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        """请启动检查在原始备份完成后尽快让出，供用户手动同步使用。"""

        self._cancel.set()

    def run(self) -> None:  # noqa: D102
        warnings: list[str] = []

        try:
            paths.harden_runtime_dirs()
        except Exception as exc:  # noqa: BLE001 - ACL 是 best-effort
            message = f"收紧运行数据目录权限失败：{exc}"
            logger.warning(message)
            warnings.append(message)

        backups_done = 0
        try:
            backups_done = auto_backup_on_first_run(self._settings)
        except Exception as exc:  # noqa: BLE001 - 首次备份失败按旧逻辑继续
            message = f"首次运行自动备份失败：{exc}"
            logger.warning(message)
            warnings.append(message)

        # 手动同步可以跳过剩下的探测，但绝不能跳过首次原始备份。
        if self._cancel.is_set():
            logger.info("启动检查已按用户请求中断；原始备份阶段已完成，跳过目标探测")
            self.ready.emit(
                StartupProbeResult(
                    statuses=[],
                    targets_missing=False,
                    traces={},
                    backups_done=backups_done,
                    warnings=tuple(warnings),
                    interrupted=True,
                )
            )
            return

        try:
            statuses = detect_targets(self._settings)
            missing, traces = both_targets_missing(self._settings)
        except Exception as exc:  # noqa: BLE001 - 线程里必须兜住所有异常
            logger.exception("启动探测失败")
            self.failed.emit(str(exc))
            return

        self.ready.emit(
            StartupProbeResult(
                statuses=statuses,
                targets_missing=missing,
                traces=traces,
                backups_done=backups_done,
                warnings=tuple(warnings),
            )
        )


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

