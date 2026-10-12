"""主窗口：状态、操作、设置项与日志。"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import backup_ops, constants, paths, preflight, startup
from ..logging_setup import log_bus
from ..settings import CCSWITCH_POLICY_DIRECT, AppSettings
from ..state_store import AppState
from ..sync import SyncReport, detect_targets
from ..targets.ccswitch import is_running as ccswitch_is_running
from ..token.capture import TokenCaptureSession, build_profile
from ..token.jwt_utils import describe_remaining, is_expired
from .icon import app_icon
from .settings_dialog import SettingsDialog
from .workers import SyncWorker

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings, state: AppState) -> None:
        super().__init__()
        self.settings = settings
        self.state = state
        self._quitting = False
        self._sync_worker: SyncWorker | None = None
        self._capture: TokenCaptureSession | None = None
        self._browser_profile = None
        self._tray_hint_shown = False

        # 开机启动的真实状态以注册表为准：外面被人删掉时，界面上的勾要跟着回到未勾。
        try:
            self.settings.launch_at_startup = startup.is_enabled()
            if self.settings.launch_at_startup:
                startup.sync_command()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取开机启动状态失败：%s", exc)

        self.setWindowTitle(constants.APP_DISPLAY_NAME)
        self.setWindowIcon(app_icon())
        self.resize(880, 640)

        self._build_ui()
        self._build_tray()
        self._wire_signals()

        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(lambda: self.start_sync(interactive=False))
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
            status_grid.addWidget(widget, row, 1)
        status_grid.setColumnStretch(1, 1)
        layout.addWidget(status_box)

        # 操作
        actions_box = QGroupBox("操作")
        actions_layout = QVBoxLayout(actions_box)
        first_row = QHBoxLayout()
        self.sync_button = QPushButton("立即获取并同步")
        self.sync_button.setMinimumHeight(34)
        self.preflight_button = QPushButton("校验写入内容")
        self.preflight_button.setToolTip("只读检查：把将要写入 DSH / CC Switch 的内容渲染出来并校验合法性")
        self.backup_button = QPushButton("手动备份")
        self.backup_button.setToolTip("立刻把 DSH / CC Switch 的关键文件各留一份备份")
        self.restore_button = QPushButton("一键还原")
        self.restore_button.setToolTip("把各文件恢复成最早的备份（= 第一次修改前的状态）")
        for button in (self.sync_button, self.preflight_button, self.backup_button, self.restore_button):
            first_row.addWidget(button)
        first_row.addStretch(1)
        actions_layout.addLayout(first_row)

        second_row = QHBoxLayout()
        self.config_button = QPushButton("打开配置")
        self.log_button = QPushButton("打开日志")
        self.data_button = QPushButton("打开数据目录")
        self.exit_button = QPushButton("退出")
        for button in (self.config_button, self.log_button, self.data_button, self.exit_button):
            second_row.addWidget(button)
        second_row.addStretch(1)
        actions_layout.addLayout(second_row)

        settings_row = QHBoxLayout()
        self.startup_check = QCheckBox("开机启动（最小化到托盘）")
        self.startup_check.setChecked(self.settings.launch_at_startup)
        self.sync_on_start_check = QCheckBox("启动时立即同步")
        self.sync_on_start_check.setChecked(self.settings.sync_on_start)
        self.auto_sync_check = QCheckBox("自动同步")
        self.auto_sync_check.setChecked(self.settings.auto_sync_enabled)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 1440)
        self.interval_spin.setSuffix(" 分钟")
        self.interval_spin.setValue(self.settings.sync_interval_minutes)
        self.minimize_after_check = QCheckBox("同步完成立即最小化到托盘")
        self.minimize_after_check.setChecked(self.settings.minimize_after_sync)

        settings_row.addWidget(self.startup_check)
        settings_row.addWidget(self.sync_on_start_check)
        settings_row.addSpacing(12)
        settings_row.addWidget(self.auto_sync_check)
        settings_row.addWidget(self.interval_spin)
        for minutes in (20, 60, 180):
            preset = QPushButton(f"{minutes} 分")
            preset.setMaximumWidth(64)
            preset.clicked.connect(lambda _=False, value=minutes: self.interval_spin.setValue(value))
            settings_row.addWidget(preset)
        settings_row.addWidget(self.minimize_after_check)
        settings_row.addStretch(1)
        actions_layout.addLayout(settings_row)
        layout.addWidget(actions_box)

        # 日志
        log_box = QGroupBox("程序日志")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(font)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_box, 1)

        self.statusBar().showMessage(f"数据目录：{paths.data_root()}")

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(constants.APP_DISPLAY_NAME)

        menu = QMenu()
        open_action = QAction("打开主界面", self)
        open_action.triggered.connect(self.show_window)
        sync_action = QAction("立即获取并同步", self)
        sync_action.triggered.connect(lambda: self.start_sync(interactive=True))
        log_action = QAction("打开日志", self)
        log_action.triggered.connect(self.open_log)
        settings_action = QAction("设置", self)
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)

        menu.addAction(open_action)
        menu.addAction(sync_action)
        menu.addSeparator()
        menu.addAction(log_action)
        menu.addAction(settings_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _wire_signals(self) -> None:
        self.sync_button.clicked.connect(lambda: self.start_sync(interactive=True))
        self.preflight_button.clicked.connect(self.check_preflight)
        self.backup_button.clicked.connect(self.manual_backup)
        self.restore_button.clicked.connect(self.restore_backup)
        self.config_button.clicked.connect(self.open_settings)
        self.log_button.clicked.connect(self.open_log)
        self.data_button.clicked.connect(lambda: self._open_path(paths.data_root()))
        self.exit_button.clicked.connect(self.quit_app)

        self.startup_check.toggled.connect(self._toggle_startup)
        self.sync_on_start_check.toggled.connect(self._save_from_ui)
        self.auto_sync_check.toggled.connect(self._on_auto_toggled)
        self.interval_spin.valueChanged.connect(self._on_interval_changed)
        self.minimize_after_check.toggled.connect(self._save_from_ui)

        self.tray.activated.connect(self._on_tray_activated)
        log_bus.message.connect(self.append_log)

    # —— 设置项 ——

    def _save_from_ui(self) -> None:
        self.settings.sync_on_start = self.sync_on_start_check.isChecked()
        self.settings.auto_sync_enabled = self.auto_sync_check.isChecked()
        self.settings.sync_interval_minutes = self.interval_spin.value()
        self.settings.minimize_after_sync = self.minimize_after_check.isChecked()
        self.settings.save()

    def _toggle_startup(self, checked: bool) -> None:
        ok, message = startup.set_enabled(checked)
        logger.info(message)
        if not ok:
            QMessageBox.warning(self, "开机启动", message)
            self.startup_check.blockSignals(True)
            self.startup_check.setChecked(not checked)
            self.startup_check.blockSignals(False)
            return
        self.settings.launch_at_startup = checked
        self.settings.save()

    def _on_auto_toggled(self, checked: bool) -> None:
        self._save_from_ui()
        self._apply_auto_timer()
        logger.info("自动同步已%s（间隔 %d 分钟）", "开启" if checked else "关闭", self.settings.sync_interval_minutes)

    def _on_interval_changed(self, value: int) -> None:
        self._save_from_ui()
        self._apply_auto_timer()
        logger.info("同步间隔改为 %d 分钟", value)

    def _apply_auto_timer(self) -> None:
        if self.settings.auto_sync_enabled:
            self._auto_timer.start(max(1, self.settings.sync_interval_minutes) * 60 * 1000)
        else:
            self._auto_timer.stop()

    # —— 界面动作 ——

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        dialog.apply_to(self.settings)
        self.settings.save()
        logger.info("设置已保存")
        self.refresh_status()
        if dialog.manual_token:
            logger.info("使用手动粘贴的 Token 开始同步")
            self._start_sync_worker(dialog.manual_token)

    def open_log(self) -> None:
        log_dir = paths.logs_dir()
        if not log_dir.exists():
            QMessageBox.information(self, "打开日志", f"日志目录还不存在：{log_dir}")
            return
        self._open_path(log_dir)

    # —— 预检 / 备份 / 还原 ——

    def current_token(self) -> str:
        """当前还能用的 Token；没有就返回空串。"""

        token = self.state.last_token
        if token and not is_expired(token):
            return token
        return ""

    def check_preflight(self) -> None:
        token = self.current_token()
        if not token:
            QMessageBox.information(
                self, "校验写入内容", "需要先获取一次 Token（点「立即获取并同步」），再回来校验。"
            )
            return
        try:
            report = preflight.run_preflight(self.settings, token)
        except Exception as exc:  # noqa: BLE001 - 预检本身出问题也要如实说
            logger.exception("预检失败")
            QMessageBox.warning(self, "校验写入内容", f"预检失败：{exc}")
            return
        for line in report.text().splitlines():
            logger.info("预检 %s", line)
        QMessageBox.information(self, "校验写入内容", report.text())

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

    def restore_backup(self) -> None:
        entries = backup_ops.pending_restore(self.settings)
        if not entries:
            QMessageBox.information(self, "一键还原", "还没有任何备份，不需要还原。")
            return
        detail = "\n".join(
            f"· {Path(source).name} ← {Path(entry.backup).parent.name}" for source, entry in entries.items()
        )
        if QMessageBox.question(
            self,
            "一键还原",
            "将把下列文件恢复成最早的备份（当前状态会先被备份一份）：\n\n" + detail,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            messages = backup_ops.restore_original(self.settings)
        except Exception as exc:  # noqa: BLE001
            logger.exception("一键还原失败")
            QMessageBox.warning(self, "一键还原", f"还原失败：{exc}")
            return
        QMessageBox.information(self, "一键还原", "\n".join(messages) or "没有可还原的内容。")
        self.refresh_status()

    @staticmethod
    def _open_path(path) -> None:  # noqa: ANN001
        try:
            if os.name == "nt":
                os.startfile(str(path))  # noqa: S606 - 打开资源管理器是预期行为
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            logger.warning("打开 %s 失败：%s", path, exc)

    def append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason) -> None:  # noqa: ANN001
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window()

    def notify(self, message: str, title: str | None = None, warning: bool = False) -> None:
        icon = QSystemTrayIcon.MessageIcon.Warning if warning else QSystemTrayIcon.MessageIcon.Information
        self.tray.showMessage(title or constants.APP_DISPLAY_NAME, message, icon, 5000)

    # —— 关闭与退出 ——

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
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
        if self._capture is not None:
            self._capture.cancel()
        worker = self._sync_worker
        if worker is not None and worker.isRunning():
            # QThread 还在跑就销毁进程会直接崩，这里等一会儿；真等不到再强制收尾。
            logger.info("退出前等待同步线程结束…")
            if not worker.wait(5000):
                logger.warning("同步线程 5 秒内没有结束，强制终止")
                worker.terminate()
                worker.wait(2000)
        self._sync_worker = None
        self.settings.save()
        self.state.save()
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
        if not self.settings.enable_ccswitch:
            self.ccswitch_status.setText("已关闭写入（设置里可重新开启）")

        token = self.state.last_token
        if token and not is_expired(token):
            self.token_status.setText(f"{describe_remaining(token)}（{self.state.last_token_at or '时间未知'} 取回）")
        elif token:
            self.token_status.setText("已过期，需要重新获取")
        else:
            self.token_status.setText("尚未获取")

        self.last_sync_status.setText(self.settings.last_sync_summary or "尚未同步")

    # —— 同步流程 ——

    def start_sync(self, interactive: bool = True) -> None:
        if self._busy():
            logger.info("上一次同步还没结束，忽略这次请求")
            return
        if interactive and not self._ask_about_ccswitch():
            return
        # 上面那个对话框是阻塞的，等待期间自动同步可能已经跑起来了，这里再确认一次
        if self._busy():
            logger.info("等待对话框期间已经开始了同步，忽略这次请求")
            return
        if interactive:
            self._open_capture(silent=False)
        else:
            self._open_capture(silent=True)

    def _busy(self) -> bool:
        """正在同步，或正在取 Token。"""

        if self._sync_worker is not None and self._sync_worker.isRunning():
            return True
        return self._capture is not None

    def _ask_about_ccswitch(self) -> bool:
        """CC Switch 运行中时的处理：提示先关闭 / 直接改 / 取消。"""

        if not self.settings.enable_ccswitch:
            return True
        if self.settings.suppress_ccswitch_running_warning:
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

        if never_again.isChecked():
            self.settings.suppress_ccswitch_running_warning = True
            self.settings.ccswitch_running_policy = CCSWITCH_POLICY_DIRECT
            self.settings.save()

        clicked = box.clickedButton()
        if clicked is cancel_button:
            logger.info("用户取消了本次同步")
            return False
        if clicked is direct_button:
            logger.info("用户选择直接改 CC Switch 数据库")
            return True
        if clicked is close_button:
            if ccswitch_is_running():
                logger.warning("CC Switch 看起来仍在运行，按用户选择继续")
            return True
        return False

    def _open_capture(self, silent: bool) -> None:
        if self._browser_profile is None:
            self._browser_profile = build_profile(paths.browser_profile_dir())

        self._capture = TokenCaptureSession(
            self._browser_profile,
            silent=silent,
            timeout_seconds=None if not silent else self.settings.silent_capture_timeout_seconds,
            parent=self,
        )
        self._capture.captured.connect(self._on_token_captured)
        self._capture.failed.connect(self._on_capture_failed)
        self._capture.cancelled.connect(self._on_capture_cancelled)
        if silent:
            logger.info("开始静默取 Token（自动同步）")
        try:
            self._capture.start()
        except Exception as exc:  # noqa: BLE001 - 起不来的话不能把 _capture 留在那儿
            logger.exception("打开取 Token 流程失败")
            self._capture = None
            self.notify(f"取 Token 失败：{exc}", warning=True)

    def _on_token_captured(self, token: str) -> None:
        self._capture = None
        self.state.remember_token(token)
        self.refresh_status()
        self._start_sync_worker(token)

    def _on_capture_failed(self, message: str) -> None:
        self._capture = None
        logger.warning(message)
        fallback = self.state.last_token
        if fallback and not is_expired(fallback):
            logger.info("取新 Token 失败，沿用现有仍有效的 Token 继续同步")
            self._start_sync_worker(fallback)
            return
        self.refresh_status()
        self.notify(message + "：请点「立即获取并同步」并完成登录。", warning=True)

    def _on_capture_cancelled(self) -> None:
        self._capture = None
        logger.info("已取消取 Token")

    def _start_sync_worker(self, token: str) -> None:
        self.sync_button.setEnabled(False)
        self._sync_worker = SyncWorker(self.settings, token, self)
        self._sync_worker.reportReady.connect(self._on_sync_report)
        self._sync_worker.failed.connect(self._on_sync_failed)
        self._sync_worker.finished.connect(self._on_sync_thread_finished)
        logger.info("开始写入已启用的目标")
        self._sync_worker.start()

    def _on_sync_report(self, report: SyncReport) -> None:
        summary = report.summary()
        self.settings.last_sync_at = datetime.now().isoformat(timespec="seconds")
        self.settings.last_sync_summary = f"{self.settings.last_sync_at} · {summary}"
        self.settings.save()
        self.state.remember_status(summary)
        self.refresh_status()
        logger.info("同步完成：%s", summary)
        self.notify(summary, warning=not report.ok)
        if self.settings.minimize_after_sync:
            self.hide()

    def _on_sync_failed(self, message: str) -> None:
        logger.error("同步失败：%s", message)
        self.notify(f"同步失败：{message}", warning=True)

    def _on_sync_thread_finished(self) -> None:
        self.sync_button.setEnabled(True)
        self._sync_worker = None

