"""主窗口：状态、操作、设置项与日志。"""

from __future__ import annotations

import copy
import logging
import os
import random
import subprocess
import time
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import backup_ops, constants, credentials, data_location, instance, paths, startup
from ..logging_setup import close_file_handlers, setup_logging
from ..settings import CCSWITCH_POLICY_DIRECT, AppSettings
from ..state_store import AppState
from ..sync import SyncReport, both_targets_missing, detect_targets
from ..targets.ccswitch import is_running as ccswitch_is_running
from ..token.capture import TokenCaptureSession, build_profile
from ..token.jwt_utils import describe_expiry, is_expired
from ..token.verifier import site_reachable
from .icon import app_icon
from .settings_dialog import SettingsDialog
from .workers import PreflightWorker, SyncWorker

logger = logging.getLogger(__name__)

# 自动同步失败后的重试策略：开机时网络 / 登录会话可能都还没就绪，
# 失败一次就干等下一个周期（默认 60 分钟）太久，这里补几次短间隔重试。
AUTO_RETRY_BUDGET = 3
AUTO_RETRY_DELAY_MS = 5 * 60 * 1000


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: AppSettings,
        state: AppState,
        *,
        desktop_notice_done: bool = False,
        started_minimized: bool = False,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.state = state
        # 启动早期（主窗口还没建）已经弹过"程序在桌面"的提醒：这一次启动别再弹第二遍
        self._desktop_notice_done = bool(desktop_notice_done)
        # 是不是开机启动（--minimized）拉起来的：静默失败要打开登录窗口、重试策略更积极
        self._started_minimized = bool(started_minimized)
        self._quitting = False
        self._sync_worker: SyncWorker | None = None
        self._preflight_worker: PreflightWorker | None = None
        self._capture: TokenCaptureSession | None = None
        self._browser_profile = None
        self._tray_hint_shown = False
        # 「强行终止并重试」：等当前同步线程安全停下后再补一次同步
        self._retry_after_stop = False
        # private 位置改了要重启才生效；干净卸载后不要再把设置写回磁盘
        self._restart_requested = False
        self._skip_save_on_quit = False
        self._moving_from: Path | None = None
        # 正在搬 private / 卸载：这期间不让自动同步插进来
        self._maintenance = False
        # 自动同步失败后的短间隔重试（开机场景）
        self._auto_retry_left = AUTO_RETRY_BUDGET
        self._auto_retry_timer = QTimer(self)
        self._auto_retry_timer.setSingleShot(True)
        self._auto_retry_timer.timeout.connect(lambda: self.start_sync(interactive=False))
        # 本次取 Token 是不是静默（自动）模式；本次同步是不是自动发起的
        self._capture_silent = False
        self._capture_user_initiated = False
        self._sync_is_auto = False
        # 开机静默失败后只自动打开一次登录窗口，避免反复弹
        self._login_offered_after_silent_failure = False

        # 开机启动的真实状态以注册表为准：外面被人删掉时，界面上的勾要跟着回到未勾。
        try:
            self.settings.launch_at_startup = startup.is_enabled()
            if self.settings.launch_at_startup:
                # 路径变了就改成当前路径；值为空（被别处写坏）就直接重写一遍。
                current = startup.current_command()
                if current is None or not current.strip():
                    ok, message = startup.set_enabled(True)
                    if not ok:
                        logger.warning("修开机启动项失败：%s", message)
                else:
                    startup.sync_command()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取开机启动状态失败：%s", exc)

        self.setWindowTitle(constants.APP_DISPLAY_NAME)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(620, 420)
        self.resize(720, 500)

        self._build_ui()
        self._build_tray()
        self._wire_signals()
        self._restore_main_window_size()

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self._auto_tick)
        self._apply_auto_timer()
        self.refresh_status()

    # —— 界面 ——

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # 状态
        status_box = QGroupBox("状态")
        status_grid = QGridLayout(status_box)
        self.dsh_status = QLabel("—")
        self.ccswitch_status = QLabel("—")
        self.token_status = QLabel("—")
        self.last_sync_status = QLabel("—")
        for row, (title, widget) in enumerate(
            (
                ("DeepSeek Harness", self.dsh_status),
                ("CC Switch", self.ccswitch_status),
                ("Token", self.token_status),
                ("上次同步", self.last_sync_status),
            )
        ):
            label = QLabel(f"{title}：")
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            status_grid.addWidget(label, row, 0)
            widget.setWordWrap(True)
            # 路径这类内容要能选中复制（报障时用得上）
            widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            status_grid.addWidget(widget, row, 1)
        status_grid.setColumnStretch(1, 1)
        layout.addWidget(status_box)

        # 操作
        actions_box = QGroupBox("操作")
        actions_layout = QVBoxLayout(actions_box)
        action_row = QHBoxLayout()
        self.sync_button = QPushButton("立即获取并同步")
        self.sync_button.setMinimumHeight(38)
        self.config_button = QPushButton("设置…")
        self.config_button.setToolTip("同步、目标、登录、备份与维护都在这里")
        self.config_button.setMinimumHeight(38)
        self.exit_button = QPushButton("退出")
        action_row.addWidget(self.sync_button, 2)
        action_row.addWidget(self.config_button, 1)
        action_row.addWidget(self.exit_button, 1)
        actions_layout.addLayout(action_row)
        self.next_sync_status = QLabel("下一次自动同步：计算中…")
        self.next_sync_status.setWordWrap(True)
        actions_layout.addWidget(self.next_sync_status)
        layout.addWidget(actions_box)

        layout.addStretch(1)

        self._restore_status_hint()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(constants.APP_DISPLAY_NAME)

        menu = QMenu()
        open_action = QAction("打开主界面", self)
        open_action.triggered.connect(self.show_window)
        sync_action = QAction("立即获取并同步", self)
        sync_action.triggered.connect(lambda: self.start_sync(interactive=True))
        self.pause_action = QAction("暂停自动同步", self)
        self.pause_action.triggered.connect(self.toggle_auto_pause)
        settings_action = QAction("设置", self)
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)

        menu.addAction(open_action)
        menu.addAction(sync_action)
        menu.addAction(self.pause_action)
        menu.addAction(settings_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self._update_pause_action()
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _wire_signals(self) -> None:
        self.sync_button.clicked.connect(lambda: self.start_sync(interactive=True))
        self.config_button.clicked.connect(self.open_settings)
        self.exit_button.clicked.connect(self.quit_app)
        self.tray.activated.connect(self._on_tray_activated)

    # —— 设置项 ——

    def _persist_settings(self) -> bool:
        """保存设置。写不进去（目录只读、U 盘拔了等）只记日志 + 托盘提醒，不抛异常。"""

        try:
            self.settings.save()
            return True
        except OSError as exc:
            logger.error("保存设置失败：%s", exc)
            self.notify(f"保存设置失败：{exc}", warning=True)
            return False

    def _persist_state(self) -> bool:
        """保存运行状态。同样不允许把异常抛回 Qt 事件循环。"""

        try:
            self.state.save()
            return True
        except OSError as exc:
            logger.error("保存运行状态失败：%s", exc)
            return False

    def _on_auto_toggled(self, checked: bool) -> None:
        self.settings.auto_sync_enabled = checked
        self._persist_settings()
        self._apply_auto_timer()
        self._update_pause_action()
        logger.info("自动同步已%s（间隔 %d 分钟）", "开启" if checked else "关闭", self.settings.sync_interval_minutes)

    def toggle_auto_pause(self) -> None:
        self._on_auto_toggled(not self.settings.auto_sync_enabled)

    def _update_pause_action(self) -> None:
        action = getattr(self, "pause_action", None)
        if action is not None:
            action.setText(
                "暂停自动同步" if self.settings.auto_sync_enabled else "继续自动同步"
            )

    def _apply_auto_timer(self) -> None:
        if self.settings.auto_sync_enabled:
            base = max(1, self.settings.sync_interval_minutes)
            jitter = constants.SYNC_INTERVAL_JITTER_MINUTES
            low = max(1, base - jitter)
            high = max(low, min(24 * 60, base + jitter))
            minutes = random.randint(low, high)
            self._auto_timer.start(minutes * 60 * 1000)
            self.next_sync_status.setText(
                f"下一次自动同步：约 {minutes} 分钟后（基准 {base} 分钟）"
            )
            logger.info("下一次自动同步预计在 %d 分钟后（基准 %d 分钟）", minutes, base)
        else:
            self._auto_timer.stop()
            self.next_sync_status.setText("自动同步已暂停")
        self._update_pause_action()

    def _auto_tick(self) -> None:
        # 下一轮在开始前先排好，避免某次提前返回时自动同步永久停摆。
        self._apply_auto_timer()
        self.start_sync(interactive=False)

    # —— 界面动作 ——

    def open_settings(self) -> None:
        dialog = SettingsDialog(
            self.settings,
            self,
            saved_size=self.state.settings_dialog_size,
        )
        dialog.window_size_saved.connect(self._remember_settings_dialog_size)
        dialog.settings_applied.connect(lambda: self._consume_manual_dialog_token(dialog))
        dialog.exec()

    def _restore_main_window_size(self) -> None:
        size = self.state.main_window_size
        if not isinstance(size, list) or len(size) != 2:
            return
        try:
            width, height = int(size[0]), int(size[1])
        except (TypeError, ValueError):
            return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, max(self.minimumWidth(), available.width() - 40))
            height = min(height, max(self.minimumHeight(), available.height() - 40))
        self.resize(max(self.minimumWidth(), width), max(self.minimumHeight(), height))

    def _remember_main_window_size(self) -> None:
        self.state.main_window_size = [self.width(), self.height()]

    def _remember_settings_dialog_size(self, width: int, height: int) -> None:
        self.state.settings_dialog_size = [int(width), int(height)]
        if not self._skip_save_on_quit:
            self._persist_state()

    def apply_settings_from_dialog(self, draft: AppSettings) -> bool:
        """Apply one settings draft immediately (used by Apply and OK)."""

        if self._skip_save_on_quit or self._quitting:
            # 设置对话框里刚做过"干净卸载"或正在退出：别再写设置文件，否则刚删的会被建回来
            return False

        old_settings = copy.deepcopy(self.settings)
        old_startup = startup.is_enabled()
        startup_changed = draft.launch_at_startup != old_startup
        if startup_changed:
            ok, message = startup.set_enabled(draft.launch_at_startup)
            logger.info(message)
            if not ok:
                QMessageBox.warning(self, "开机启动", message)
                return False

        previous_level = self.settings.log_level
        for field in fields(AppSettings):
            setattr(self.settings, field.name, getattr(draft, field.name))
        self.settings.launch_at_startup = startup.is_enabled()
        if not self._persist_settings():
            for field in fields(AppSettings):
                setattr(self.settings, field.name, getattr(old_settings, field.name))
            if startup_changed:
                startup.set_enabled(old_startup)
            return False
        if self.settings.log_level != previous_level:
            # 改日志级别立刻生效，不用等下次启动
            setup_logging(logging.DEBUG if self.settings.log_level == "DEBUG" else logging.INFO)
            logger.info("日志级别已切换为 %s", self.settings.log_level)
        logger.info("设置已保存")
        self.apply_settings_to_ui()
        self._apply_auto_timer()
        self.refresh_status()
        return True

    def _consume_manual_dialog_token(self, dialog: SettingsDialog) -> None:
        if not dialog.manual_token:
            return
        token = dialog.manual_token
        dialog.manual_token = ""
        if self._maintenance or self._busy():
            # 忙碌时不能再起一个写入线程：两个线程同时写同一批文件会打架
            logger.info("正在忙，手动粘贴的 Token 这次没有使用")
            QMessageBox.warning(
                self,
                "手动粘贴 Token",
                "当前正在同步或正在取 Token，这份 Token 这次没有使用。\n"
                "等它结束以后再粘贴一次（或用「立即获取并同步」）。",
            )
            return
        try:
            self.state.remember_token(token)
        except OSError as exc:
            logger.error("保存手动粘贴的 Token 失败：%s", exc)
        self.refresh_status()
        logger.info("使用手动粘贴的 Token 开始同步")
        self._start_sync_worker(token, auto=False)

    def open_log(self) -> None:
        log_dir = paths.logs_dir()
        if not log_dir.exists():
            QMessageBox.information(self, "打开日志", f"日志目录还不存在：{log_dir}")
            return
        self._open_path(log_dir)

    def create_shortcut(self) -> None:
        """在桌面生成一个指向本程序的快捷方式（主界面与设置里都能点）。"""

        try:
            ok, message = startup.create_desktop_shortcut()
        except Exception as exc:  # noqa: BLE001 - 建不了快捷方式也要如实说
            logger.exception("创建桌面快捷方式失败")
            QMessageBox.warning(self, "创建桌面快捷方式", f"创建失败：{exc}")
            return
        logger.info(message)
        if ok:
            QMessageBox.information(self, "创建桌面快捷方式", message)
        else:
            QMessageBox.warning(self, "创建桌面快捷方式", message)

    # —— 启动检查（先"程序在桌面"，再"两个目标都没装"） ——

    def startup_then_sync(self) -> None:
        """启动后的第一件事：先跑两条提醒，再决定要不要跑启动同步。"""

        if not self.run_startup_checks():
            return
        if self.settings.sync_on_start:
            logger.info("启动后自动同步一次")
            self.start_sync(interactive=False)
        else:
            logger.info("设置里关掉了「启动时立即同步」，本次不自动同步")

    def run_startup_checks(self) -> bool:
        """返回 True 表示可以继续这次启动同步。"""

        if not self._check_desktop_private():
            return False
        return self._check_targets_missing()

    def _check_desktop_private(self) -> bool:
        """程序直接在桌面根目录时提醒用户；返回 False 表示已经退出程序。"""

        if self._desktop_notice_done:
            return True
        if self.state.desktop_note_ack:
            return True
        if paths.read_private_override() is not None:
            # private 已经挪到别处，桌面不会再生成它，不用提醒
            return True
        if not startup.is_app_on_desktop():
            return True

        if data_location.desktop_notice(
            self,
            self.state,
            prepare=self._prepare_for_data_move,
            after=self._after_data_move,
        ):
            return True
        self.quit_app()
        return False

    def _check_targets_missing(self) -> bool:
        """DSH 与 CC Switch 都没检出时提醒一次并跳过这次同步；返回是否继续同步。"""

        missing, traces = both_targets_missing(self.settings)
        if not missing:
            return True
        logger.info("DSH 与 CC Switch 都没有痕迹，跳过这次同步：%s", traces)
        if not self.state.targets_note_ack:
            box = QMessageBox(self)
            box.setWindowTitle("没有检测到 DSH / CC Switch")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(
                "未检测到 DSH / CC Switch 的配置：如果已安装，请先分别运行一次让它们生成配置然后退出，"
                "或在上方指定路径，"
            )
            box.setInformativeText("这一次同步已经跳过。")
            box.addButton("知道了", QMessageBox.ButtonRole.AcceptRole)
            box.exec()
            self.state.targets_note_ack = True
            self._persist_state()
            logger.info("已提醒过「都没检出」，以后不再弹这个提醒")
        return False

    def choose_private_dir(self) -> bool:
        """选一个新的 private 位置并搬过去。返回是否搬成功。"""

        if self._busy():
            QMessageBox.warning(
                self, "更改 private 位置", "正在同步或正在取 Token，等它结束再改 private 位置。"
            )
            return False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return data_location.choose_private_dir(
                self,
                prepare=self._prepare_for_data_move,
                after=self._after_data_move,
                offer_restart=self._offer_restart,
            )
        finally:
            QApplication.restoreOverrideCursor()

    # —— 搬迁/卸载前的准备与收尾 ——

    def _release_browser_profile(self) -> None:
        """放掉内置浏览器档案：不然 browser-profile 里的 Cookies 被占着，搬不动也删不掉。"""

        if self._capture is not None:
            self._capture.cancel()
            self._capture = None
        profile = self._browser_profile
        self._browser_profile = None
        if profile is None:
            return
        try:
            profile.deleteLater()
        except (RuntimeError, AttributeError):  # pragma: no cover - 已经没了就算了
            pass
        # 让 Qt 真正处理 deleteLater（普通 processEvents 不会处理 DeferredDelete），
        # 再给 Chromium 一点时间把手里的文件关掉。剩下的（极少数仍被占的）会在
        # 下次启动时由 paths.finish_pending_migration() 补删干净。
        for _ in range(12):
            # 普通 processEvents 不处理 DeferredDelete，这里显式投递一次
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QApplication.processEvents()
            time.sleep(0.05)
        logger.info("已释放内置浏览器档案（browser-profile 解锁）")

    def _prepare_for_data_move(self) -> None:
        """搬 private 之前：停取 Token、放掉浏览器、关掉日志文件句柄。"""

        self._maintenance = True
        self._auto_timer.stop()
        self._moving_from = paths.data_root()
        self._release_browser_profile()
        closed = close_file_handlers()
        logger.info("搬 private 前已关闭 %d 个日志文件句柄", closed)

    def _after_data_move(self) -> None:
        """搬完之后：在新位置重开日志，再补一次旧位置清理。"""

        setup_logging(logging.DEBUG if self.settings.log_level == "DEBUG" else logging.INFO)
        old = self._moving_from
        self._moving_from = None
        if old is None or old == paths.data_root() or not old.is_dir():
            pass
        else:
            leftovers = paths.remove_managed_data(old)
            logger.info("旧 private 位置补清理：%s", "；".join(leftovers) or "没有残留")
        # 维护结束，自动同步计时器按当前设置重新起来
        self._maintenance = False
        self._apply_auto_timer()

    def _offer_restart(self, message: str) -> None:
        if QMessageBox.question(
            self,
            "更改 private 位置",
            message + "\n\n现在重启程序吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self.restart_app()

    def prepare_for_uninstall(self) -> None:
        """干净卸载之前：停取 Token、放掉浏览器、关日志、让出单实例锁。"""

        self._maintenance = True
        self._auto_timer.stop()
        self._release_browser_profile()
        close_file_handlers()
        instance.release()
        logger.info("已准备好干净卸载（浏览器、日志、单实例锁都已放开）")

    # —— 维护动作 ——

    def apply_settings_to_ui(self) -> None:
        """Refresh the compact main window after settings are applied."""

        self._apply_auto_timer()
        self.refresh_status()

    def restart_app(self) -> None:
        """重启自己（private 位置改了需要重启才生效）。"""

        self._restart_requested = True
        # 先让出单实例锁，并把旧 private 位置的最后一点残留清掉
        # （不然新进程起来会被判成"程序已经在运行"，第一次就自动退出）
        instance.release()
        try:
            paths.finish_pending_migration()
        except Exception as exc:  # noqa: BLE001 - 清不掉不影响重启
            logger.warning("重启前清理旧 private 位置失败：%s", exc)
        logger.info("按要求重启程序")
        self.quit_app()

    @property
    def restart_requested(self) -> bool:
        return self._restart_requested

    def quit_after_uninstall(self) -> None:
        """干净卸载后退出：不要再写设置/状态，否则刚删掉的文件会被重新创建。"""

        self._skip_save_on_quit = True
        instance.release()
        logger.info("干净卸载完成，退出程序（不再写回设置文件）")
        self.quit_app()

    # —— 预检 / 备份 / 还原 ——

    def current_token(self) -> str:
        """当前还能用的 Token；没有就返回空串。"""

        token = self.state.last_token
        # is_expired 返回 None 表示"Token 没带 exp、判断不了"，这种情况当还能用来处理，
        # 真正的有效性交给站点校验；只有明确已过期（True）才拒绝。
        if not token or is_expired(token) is True:
            return ""
        return token

    def check_preflight(self) -> None:
        token = self.current_token()
        if not token:
            QMessageBox.information(
                self, "校验写入内容", "需要先获取一次 Token（点「立即获取并同步」），再回来校验。"
            )
            return
        if self._preflight_worker is not None:
            logger.info("上一次校验还没结束，忽略这次请求")
            return
        self._show_status_hint("正在校验将要写入的内容…（只读，不改文件）")
        worker = PreflightWorker(self.settings, token, self)
        self._preflight_worker = worker
        worker.reportReady.connect(lambda report, w=worker: self._on_preflight_report(report, w))
        worker.failed.connect(lambda message, w=worker: self._on_preflight_failed(message, w))
        worker.finished.connect(lambda w=worker: self._on_preflight_finished(w))
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 - 预检本身出问题也要如实说
            logger.exception("预检线程启动失败")
            self._preflight_worker = None
            self._restore_status_hint()
            QMessageBox.warning(self, "校验写入内容", f"预检启动失败：{exc}")

    def _on_preflight_report(self, report, worker=None) -> None:  # noqa: ANN001
        if worker is not None and worker is not self._preflight_worker:
            return
        for line in report.text().splitlines():
            logger.info("预检 %s", line)
        QMessageBox.information(self, "校验写入内容", report.text())

    def _on_preflight_failed(self, message: str, worker=None) -> None:
        if worker is not None and worker is not self._preflight_worker:
            return
        logger.error("预检失败：%s", message)
        QMessageBox.warning(self, "校验写入内容", f"预检失败：{message}")

    def _on_preflight_finished(self, worker=None) -> None:
        if worker is None:
            worker = self.sender()
        if worker is not None and worker is not self._preflight_worker:
            return
        self._preflight_worker = None
        self._restore_status_hint()

    def manual_backup(self) -> None:
        try:
            done, lines = backup_ops.manual_backup(self.settings)
        except Exception as exc:  # noqa: BLE001
            logger.exception("手动备份失败")
            QMessageBox.warning(self, "手动备份", f"备份失败：{exc}")
            return
        QMessageBox.information(
            self,
            "手动备份",
            "\n".join(lines) + f"\n\n共 {done} 份\n备份目录：{backup_ops.backup_dir()}",
        )

    # —— 状态栏与关于 ——

    def _restore_status_hint(self) -> None:
        """状态栏回到默认提示（运行数据目录）。"""

        self.statusBar().showMessage(f"数据目录：{paths.data_root()}")

    def _open_data_dir(self) -> None:
        self._open_path(paths.data_root())

    def _show_status_hint(self, message: str) -> None:
        """状态栏显示当前正在做什么：取 Token / 写入 / 校验都是要等好几秒的动作。"""

        self.statusBar().showMessage(message)

    def show_about(self) -> None:
        """关于：版本、作者、项目主页与运行数据目录（报障时要能看到版本号）。"""

        box = QMessageBox(self)
        box.setWindowTitle("关于")
        box.setIconPixmap(app_icon().pixmap(64, 64))
        box.setText(f"{constants.APP_DISPLAY_NAME} {constants.APP_VERSION}")
        box.setInformativeText(
            f"作者：{constants.APP_AUTHOR}\n"
            f"项目主页：{constants.REPO_URL}\n"
            f"运行数据目录：{paths.data_root()}\n\n"
            "完整使用说明见项目主页里的 README.md 与 docs/USER-GUIDE.md。"
        )
        box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.exec()

    @staticmethod
    def _open_path(path) -> None:  # noqa: ANN001
        try:
            if os.name == "nt":
                os.startfile(str(path))  # noqa: S606 - 打开资源管理器是预期行为
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            logger.warning("打开 %s 失败：%s", path, exc)

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason) -> None:  # noqa: ANN001
        # 左键单击或双击都打开主界面（不同用户习惯不一样）
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window()

    def notify(self, message: str, title: str | None = None, warning: bool = False) -> None:
        icon = QSystemTrayIcon.MessageIcon.Warning if warning else QSystemTrayIcon.MessageIcon.Information
        self.tray.showMessage(title or constants.APP_DISPLAY_NAME, message, icon, 5000)

    # —— 关闭与退出 ——

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        if not self._skip_save_on_quit:
            self._remember_main_window_size()
        if self._quitting:
            event.accept()
            return
        if not self.settings.close_to_tray or not QSystemTrayIcon.isSystemTrayAvailable():
            # 设置里选了"关掉就退出"，那就真的退出
            event.accept()
            self.quit_app()
            return
        event.ignore()
        self.hide()
        if not self._skip_save_on_quit:
            self._persist_state()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage(
                constants.APP_DISPLAY_NAME,
                "已最小化到托盘：左键单击图标打开主界面，右键单击打开菜单。",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    def quit_app(self) -> None:
        self._quitting = True
        self._auto_timer.stop()
        self._auto_retry_timer.stop()
        if self._capture is not None:
            self._capture.cancel()
        worker = self._sync_worker
        if worker is not None and worker.isRunning():
            # QThread 还在跑就销毁进程会直接崩。先请它在步骤之间安全停下，再等它；
            # 只有实在等不到才强制收尾（这时可能正在写文件，日志里必须说清）。
            logger.info("退出前请同步线程在安全位置停下…")
            worker.request_cancel()
            if not worker.wait(20000):
                logger.warning("同步线程 20 秒内没有停下，只能强制结束（可能正在写文件）")
                worker.terminate()
                worker.wait(2000)
        self._sync_worker = None
        preflight_worker = self._preflight_worker
        if preflight_worker is not None and preflight_worker.isRunning():
            # 预检是只读的，但 QThread 在运行中被销毁会让 Qt 直接崩。
            # 等一小会儿；还等不到就从窗口上摘下来、交给 QApplication 保活，
            # 别让窗口析构把它一起删掉。
            logger.info("退出前等校验线程结束（最多 5 秒）…")
            if not preflight_worker.wait(5000):
                logger.warning("校验线程还没结束（只读操作），交给进程退出处理")
                preflight_worker.setParent(None)
                application = QApplication.instance()
                if application is not None:
                    orphans = getattr(application, "_atd_orphan_threads", None)
                    if orphans is None:
                        orphans = []
                        application._atd_orphan_threads = orphans  # type: ignore[attr-defined]
                    orphans.append(preflight_worker)
        self._preflight_worker = None
        if not self._skip_save_on_quit:
            self._remember_main_window_size()
            self._persist_settings()
            self._persist_state()
        self.tray.hide()
        self.close()
        from PySide6.QtWidgets import QApplication

        QApplication.quit()

    # —— 状态刷新 ——

    def refresh_status(self) -> None:
        try:
            statuses = detect_targets(self.settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("探测目标失败：%s", exc)
            statuses = []

        for status in statuses:
            text = ("已安装 · " if status.detected else "未检测到 · ") + status.detail
            if status.key == "dsh":
                self.dsh_status.setText(text)
            elif status.key == "ccswitch":
                self.ccswitch_status.setText(text)

        if not self.settings.enable_dsh:
            self.dsh_status.setText("已关闭写入（设置里可重新开启）")
        elif not (
            self.settings.enable_dsh_credentials or self.settings.enable_dsh_provider
        ):
            self.dsh_status.setText("已关闭全部 DSH 写入项（设置里可重新开启）")
        if not self.settings.enable_ccswitch:
            self.ccswitch_status.setText("已关闭写入（设置里可重新开启）")
        elif not (
            self.settings.enable_ccswitch_card or self.settings.enable_ccswitch_activate
        ):
            self.ccswitch_status.setText("已关闭全部 CC Switch 写入项（设置里可重新开启）")

        token = self.state.last_token
        if token and is_expired(token) is not True:
            self.token_status.setText(
                f"{describe_expiry(token, self.state.last_token_at)}"
                f"（{self.state.last_token_at or '时间未知'} 取回）"
            )
        elif token:
            self.token_status.setText("已过期，需要重新获取")
        else:
            self.token_status.setText("尚未获取")

        self.last_sync_status.setText(self.settings.last_sync_summary or "尚未同步")

    # —— 同步流程 ——

    def start_sync(self, interactive: bool = True) -> None:
        if self._maintenance:
            logger.info("正在搬 private 位置或卸载，本次同步先跳过")
            return
        dsh_active = self.settings.enable_dsh and (
            self.settings.enable_dsh_credentials or self.settings.enable_dsh_provider
        )
        ccswitch_active = self.settings.enable_ccswitch and (
            self.settings.enable_ccswitch_card or self.settings.enable_ccswitch_activate
        )
        if not (dsh_active or ccswitch_active):
            logger.warning("DSH 与 CC Switch 的所有具体写入项都关着，本次同步没有意义")
            if interactive:
                QMessageBox.warning(
                    self,
                    "没有启用任何写入目标",
                    "DSH 与 CC Switch 的具体写入项现在都是关着的，同步不会写任何东西。\n"
                    "请先在「设置…」里打开至少一个写入目标。",
                )
            return
        if not interactive and not site_reachable():
            # 开机时网络经常还没就绪：先别去取 Token（那要白等 40 秒），过几分钟自动重试
            logger.info("站点暂时连不上（可能网络还没就绪），本次自动同步先跳过")
            self._schedule_auto_retry("网络还没就绪")
            return
        if self._busy():
            if not interactive:
                # 自动同步撞上"还在忙"就直接跳过：别在用户正登录/正写入时弹窗打扰
                logger.info("上一次同步还没结束，自动同步这次先跳过")
                return
            logger.info("上一次同步还没结束，询问是否强行终止并重试")
            if not self._ask_force_retry():
                return
            # 强行终止只是"请它停下"：取 Token 能立刻取消；同步线程会在步骤之间停下。
            # 线程还没结束就先记一笔，等它真正结束再重试（见 _on_sync_thread_finished）。
            worker = self._sync_worker
            if worker is not None and worker.isRunning():
                self._retry_after_stop = True
                logger.info("已请求安全停止，等它结束后自动重试")
                return
        if interactive and not self._ask_about_ccswitch():
            return
        # 上面那个对话框是阻塞的，等待期间自动同步可能已经跑起来了，这里再确认一次
        if self._busy():
            logger.info("等待对话框期间已经开始了同步，忽略这次请求")
            return
        # 第一次运行、还没有任何历史 Token 时，浏览器档案里也没有可复用的登录会话，
        # 静默取只会白等超时。保存了自动登录凭据时，手动点击也先走后台登录。
        silent = self._should_capture_silently(interactive)
        self._capture_user_initiated = interactive
        if not silent:
            logger.info("还没有缓存 Token，改开浏览器窗口完成首次登录")
            self._open_capture(silent=False)
            return
        self._open_capture(silent=True)

    def _ask_force_retry(self) -> bool:
        """上一次还没结束时问用户：强行终止并按原样重试，还是取消。"""

        box = QMessageBox(self)
        box.setWindowTitle("上一次同步还没结束")
        box.setIcon(QMessageBox.Icon.Warning)
        if self._capture is not None:
            box.setText("上一次取 Token 还没结束（登录窗口可能还开着）。")
        else:
            box.setText("上一次同步还在处理，目标文件可能正写到一半。")
        box.setInformativeText(
            "「强行终止并重试」会在安全的位置停下当前动作，然后重新开始同步。\n"
            "正在写入的那一份文件会先写完（失败会自动回滚），不会写坏配置。\n"
            "如果正卡在网络校验上，会等这一步返回（最多约半分钟）再停下。"
        )
        force_button = box.addButton("强行终止并重试", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not force_button:
            logger.info("用户取消了强行终止")
            return False

        if self._capture is not None:
            # 取 Token 的取消是同步完成的，cancel() 里会把 _capture 清掉
            self._capture.cancel()
        worker = self._sync_worker
        if worker is not None and worker.isRunning():
            logger.info("已请求同步线程安全停止（不杀线程）")
            worker.request_cancel()
        return True

    def _should_capture_silently(self, interactive: bool) -> bool:
        """自动同步时是否静默取 Token。

        第一次运行还没有任何历史 Token，浏览器档案里没有可复用的登录会话，
        静默只会白等超时，所以直接开窗口；之后有缓存就走静默。
        用户保存了自动登录凭据时，手动点击同步也先在后台尝试登录；只有验证码、
        二次验证或页面异常时才把登录窗口显示出来。
        """

        if self.settings.auto_login_enabled:
            try:
                if credentials.load_login() is not None:
                    return True
            except OSError as exc:
                logger.warning("读取自动登录凭据失败：%s", exc)
        if interactive:
            return False
        return bool(self.state.last_token) or bool(self.current_token())

    def _busy(self) -> bool:
        """正在同步，或正在取 Token。"""

        # 线程对象还在就算忙：哪怕它刚好跑完、finished 信号还没派发到主线程，
        # 这时候放新的同步进来会让两轮并排跑（旧线程收尾时还会误清新线程的状态）。
        if self._sync_worker is not None:
            return True
        return self._capture is not None

    def _ask_about_ccswitch(self) -> bool:
        """CC Switch 运行中时的处理：提示先关闭 / 直接改 / 取消。"""

        if not self.settings.enable_ccswitch:
            return True
        if self.settings.ccswitch_running_policy == CCSWITCH_POLICY_DIRECT:
            return True
        if not ccswitch_is_running():
            return True

        box = QMessageBox(self)
        box.setWindowTitle("CC Switch 正在运行")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("检测到 CC Switch 正在运行。")
        box.setInformativeText(
            "直接改它的数据库有可能被它覆盖。建议先完全退出 CC Switch，再点「关闭好了，继续」。"
        )
        close_button = box.addButton("关闭好了，继续", QMessageBox.ButtonRole.AcceptRole)
        direct_button = box.addButton("直接改", QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        never_again = QCheckBox("以后不再提示（以后都直接改）")
        box.setCheckBox(never_again)
        box.exec()
        clicked = box.clickedButton()

        if clicked is cancel_button or clicked is None:
            # 取消（或直接关掉对话框）就什么都不改：连"以后不再提示"的勾选也不生效，
            # 免得"取消了一次同步"却把设置悄悄改掉
            logger.info("用户取消了本次同步（勾选状态不保存）")
            return False

        if never_again.isChecked():
            self.settings.ccswitch_running_policy = CCSWITCH_POLICY_DIRECT
            self._persist_settings()

        if clicked is direct_button:
            logger.info("用户选择直接改 CC Switch 数据库")
            return True
        if clicked is close_button:
            if ccswitch_is_running():
                logger.warning("CC Switch 看起来仍在运行，按用户选择继续")
            return True
        return False

    def _open_capture(self, silent: bool) -> None:
        self._capture_silent = silent
        if self._browser_profile is None:
            self._browser_profile = build_profile(paths.browser_profile_dir())

        auto_username = ""
        auto_password = ""
        if self.settings.auto_login_enabled:
            try:
                stored = credentials.load_login()
            except OSError as exc:
                logger.warning("读取自动登录凭据失败：%s", exc)
                stored = None
            if stored is None:
                logger.info("已启用自动登录，但 Credential Manager 里没有凭据")
                if silent:
                    silent = False
            else:
                auto_username = stored.username
                auto_password = stored.password

        self._capture = TokenCaptureSession(
            self._browser_profile,
            silent=silent,
            timeout_seconds=None if not silent else self.settings.silent_capture_timeout_seconds,
            auto_username=auto_username,
            auto_password=auto_password,
            parent=self,
        )
        self._capture.captured.connect(self._on_token_captured)
        self._capture.failed.connect(self._on_capture_failed)
        self._capture.cancelled.connect(self._on_capture_cancelled)
        self._capture.manual_intervention_required.connect(self._on_manual_login_required)
        if silent:
            if auto_username:
                logger.info("开始后台取 Token（使用已保存的账号密码自动登录）")
                self._show_status_hint("正在后台登录并获取 Token…")
            else:
                logger.info("开始静默取 Token（使用已保存的登录会话）")
                self._show_status_hint("正在静默获取 Token…")
        else:
            self._show_status_hint("正在获取 Token：请在登录窗口完成登录…")
        try:
            self._capture.start()
        except Exception as exc:  # noqa: BLE001 - 起不来的话不能把 _capture 留在那儿
            logger.exception("打开取 Token 流程失败")
            self._capture = None
            self._capture_user_initiated = False
            self.notify(f"取 Token 失败：{exc}", warning=True)

    def _on_manual_login_required(self, reason: str) -> None:
        self._show_status_hint("自动登录已暂停，请在登录窗口中完成验证…")
        self.notify(f"{reason}。已暂停自动登录并打开登录窗口。", warning=True)

    def _on_token_captured(self, token: str) -> None:
        self._capture = None
        was_user_initiated = self._capture_user_initiated
        self._capture_user_initiated = False
        try:
            self.state.remember_token(token)
        except OSError as exc:
            # 状态文件写不进去不该拦住同步：Token 已经在内存里，这次先用着
            logger.error("保存 Token 到状态文件失败：%s", exc)
            self.notify(f"Token 保存失败（本次同步继续）：{exc}", warning=True)
        self.refresh_status()
        self._show_status_hint("已获取 Token，正在写入目标…")
        self._start_sync_worker(token, auto=not was_user_initiated)

    def _on_capture_failed(self, message: str) -> None:
        self._capture = None
        self._restore_status_hint()
        was_silent = self._capture_silent
        was_user_initiated = self._capture_user_initiated
        self._capture_silent = False
        self._capture_user_initiated = False
        logger.warning(message)
        fallback = self.state.last_token
        if fallback and not is_expired(fallback):
            logger.info("取新 Token 失败，沿用现有仍有效的 Token 继续同步")
            self._start_sync_worker(fallback, auto=not was_user_initiated)
            return
        self.refresh_status()
        should_open_login = (
            was_silent
            and (self._started_minimized or was_user_initiated)
            and not self._login_offered_after_silent_failure
        )
        if was_silent and was_user_initiated:
            # 用户刚点过同步：每次都应把失败的登录页显示出来。
            should_open_login = True
        if should_open_login:
            # 后台登录失败时，开机启动或用户刚点过同步都直接把登录窗口打开；
            # 用户手动点击后不能只留一条托盘气泡，否则看起来像“完全没反应”。
            self._login_offered_after_silent_failure = True
            logger.info("后台取 Token 失败：打开登录窗口等用户登录")
            self.notify("自动更新需要重新登录，已打开登录窗口", warning=True)
            self._capture_user_initiated = was_user_initiated
            self._open_capture(silent=False)
            return
        self.notify(message + "：请点「立即获取并同步」并完成登录。", warning=True)
        if was_silent:
            self._schedule_auto_retry("取 Token 失败")

    def _on_capture_cancelled(self) -> None:
        self._capture = None
        self._capture_user_initiated = False
        self._restore_status_hint()
        logger.info("已取消取 Token")

    def _start_sync_worker(self, token: str, *, auto: bool | None = None) -> None:
        # auto=True 表示这次是自动（静默）发起的：失败后要安排短间隔重试
        self._sync_is_auto = self._capture_silent if auto is None else auto
        self.sync_button.setEnabled(False)
        worker = SyncWorker(self.settings, token, self)
        self._sync_worker = worker
        worker.reportReady.connect(lambda report, w=worker: self._on_sync_report(report, w))
        worker.failed.connect(lambda message, w=worker: self._on_sync_failed(message, w))
        worker.finished.connect(lambda w=worker: self._on_sync_thread_finished(w))
        logger.info("开始写入已启用的目标")
        self._show_status_hint("正在写入已启用的目标…")
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 - 线程起不来也要把界面恢复过来
            logger.exception("同步线程启动失败")
            self._sync_worker = None
            self.sync_button.setEnabled(True)
            self.notify(f"同步启动失败：{exc}", warning=True)

    def _on_sync_report(self, report: SyncReport, worker=None) -> None:
        if worker is not None and worker is not self._sync_worker:
            logger.info("上一次同步的结果晚到了，忽略（已经有新的同步在跑）")
            return
        summary = report.summary()
        self.settings.last_sync_at = datetime.now().isoformat(timespec="seconds")
        self.settings.last_sync_summary = f"{self.settings.last_sync_at} · {summary}"
        self._persist_settings()
        try:
            self.state.remember_status(summary)
        except OSError as exc:
            logger.error("保存同步状态失败：%s", exc)
        self.refresh_status()
        self._restore_status_hint()
        if report.cancelled:
            # 是我们自己请它停下的，别弹"同步完成"的通知吓人
            logger.info("同步已按你的要求安全停下")
            return
        if report.ok:
            # 成功一次就把重试预算补满（下一次失败还能重试几次）
            self._auto_retry_left = AUTO_RETRY_BUDGET
            self._apply_auto_timer()
        elif self._sync_is_auto:
            self._schedule_auto_retry("上次同步没有成功")
        logger.info("同步完成：%s", summary)
        self.notify(summary, warning=not report.ok)
        if self.settings.minimize_after_sync:
            self.hide()

    def _on_sync_failed(self, message: str, worker=None) -> None:
        if worker is not None and worker is not self._sync_worker:
            logger.info("上一次同步的错误晚到了，忽略（已经有新的同步在跑）")
            return
        logger.error("同步失败：%s", message)
        self._restore_status_hint()
        self.notify(f"同步失败：{message}", warning=True)
        if self._sync_is_auto:
            self._schedule_auto_retry("同步过程出错")

    def _on_sync_thread_finished(self, worker=None) -> None:
        if worker is None:
            worker = self.sender()
        if worker is not None and worker is not self._sync_worker:
            # 老线程的收尾信号：别动新线程的状态
            logger.info("上一次同步的线程收尾，忽略（已经有新的同步在跑）")
            return
        self.sync_button.setEnabled(True)
        self._sync_worker = None
        self._restore_status_hint()
        if self._retry_after_stop:
            self._retry_after_stop = False
            logger.info("上一次同步已安全停下，按你的要求重新开始")
            # 用绑定方法而不是 lambda：窗口已经关掉/程序正在退出时，这里能立刻安全返回
            QTimer.singleShot(0, self._retry_sync_now)

    def _retry_sync_now(self) -> None:
        if self._quitting:
            return
        self.start_sync(interactive=True)

    def _schedule_auto_retry(self, reason: str) -> None:
        """自动同步没成功时，过几分钟再试一次，而不是干等下一个周期。

        开机场景里网络、站点登录会话都可能还没就绪；重试预算用完就安静等下一个周期，
        成功一次会把预算补满（见 _on_sync_report）。
        """

        if self._quitting or self._maintenance:
            return
        if self._auto_retry_left <= 0:
            logger.info("自动重试次数已用完（原因：%s），等下一个同步周期", reason)
            return
        self._auto_retry_left -= 1
        minutes = AUTO_RETRY_DELAY_MS // 60000
        logger.info(
            "%d 分钟后自动重试一次（原因：%s；还能重试 %d 次）",
            minutes,
            reason,
            self._auto_retry_left,
        )
        self._auto_retry_timer.start(AUTO_RETRY_DELAY_MS)

