"""主窗口：状态、操作、设置项与日志。"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QFont
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
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import backup_ops, constants, data_location, instance, paths, preflight, startup
from ..logging_setup import close_file_handlers, log_bus, setup_logging
from ..settings import CCSWITCH_POLICY_DIRECT, AppSettings
from ..state_store import AppState
from ..sync import SyncReport, both_targets_missing, detect_targets
from ..targets.ccswitch import is_running as ccswitch_is_running
from ..token.capture import TokenCaptureSession, build_profile
from ..token.jwt_utils import describe_expiry, is_expired
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
        # 「强行终止并重试」：等当前同步线程安全停下后再补一次同步
        self._retry_after_stop = False
        # private 位置改了要重启才生效；干净卸载后不要再把设置写回磁盘
        self._restart_requested = False
        self._skip_save_on_quit = False
        self._moving_from: Path | None = None

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
        self.shortcut_button = QPushButton("创建桌面快捷方式")
        self.shortcut_button.setToolTip("在桌面生成一个指向本程序的快捷方式，双击即可启动")
        self.log_button = QPushButton("打开日志")
        self.data_button = QPushButton("打开数据目录")
        self.exit_button = QPushButton("退出")
        for button in (
            self.config_button,
            self.shortcut_button,
            self.log_button,
            self.data_button,
            self.exit_button,
        ):
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
        self.shortcut_button.clicked.connect(self.create_shortcut)
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
        if self._skip_save_on_quit or self._quitting:
            # 设置对话框里刚做过"干净卸载"或正在退出：别再写设置文件，否则刚删的会被建回来
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

        if self.state.desktop_note_ack:
            return True
        if paths.read_private_override() is not None:
            # private 已经挪到别处，桌面不会再生成它，不用提醒
            return True
        if not startup.is_app_on_desktop():
            return True

        if data_location.desktop_notice(self, self.state):
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
            self.state.save()
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
            return
        leftovers = paths.remove_managed_data(old)
        logger.info("旧 private 位置补清理：%s", "；".join(leftovers) or "没有残留")

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

        self._release_browser_profile()
        close_file_handlers()
        instance.release()
        logger.info("已准备好干净卸载（浏览器、日志、单实例锁都已放开）")

    # —— 维护动作 ——

    def apply_settings_to_ui(self) -> None:
        """把 settings 的值刷到主界面控件上（「恢复默认设置」之后要立刻看到）。"""

        for widget, value in (
            (self.sync_on_start_check, self.settings.sync_on_start),
            (self.auto_sync_check, self.settings.auto_sync_enabled),
            (self.minimize_after_check, self.settings.minimize_after_sync),
        ):
            widget.blockSignals(True)
            widget.setChecked(value)
            widget.blockSignals(False)
        self.interval_spin.blockSignals(True)
        self.interval_spin.setValue(self.settings.sync_interval_minutes)
        self.interval_spin.blockSignals(False)
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
            # QThread 还在跑就销毁进程会直接崩。先请它在步骤之间安全停下，再等它；
            # 只有实在等不到才强制收尾（这时可能正在写文件，日志里必须说清）。
            logger.info("退出前请同步线程在安全位置停下…")
            worker.request_cancel()
            if not worker.wait(20000):
                logger.warning("同步线程 20 秒内没有停下，只能强制结束（可能正在写文件）")
                worker.terminate()
                worker.wait(2000)
        self._sync_worker = None
        if not self._skip_save_on_quit:
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
        if self._busy():
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
        # 静默取只会白等超时。这种情况直接开浏览器窗口让用户登录；之后有了缓存就静默取。
        if not self._should_capture_silently(interactive):
            logger.info("还没有缓存 Token，改开浏览器窗口完成首次登录")
            self._open_capture(silent=False)
            return
        self._open_capture(silent=not interactive)

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
            "正在写入的那一份文件会先写完（失败会自动回滚），不会写坏配置。"
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
        """

        if interactive:
            return False
        return bool(self.state.last_token) or bool(self.current_token())

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
        if report.cancelled:
            # 是我们自己请它停下的，别弹"同步完成"的通知吓人
            logger.info("同步已按你的要求安全停下")
            return
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
        if self._retry_after_stop:
            self._retry_after_stop = False
            logger.info("上一次同步已安全停下，按你的要求重新开始")
            # 用绑定方法而不是 lambda：窗口已经关掉/程序正在退出时，这里能立刻安全返回
            QTimer.singleShot(0, self._retry_sync_now)

    def _retry_sync_now(self) -> None:
        if self._quitting:
            return
        self.start_sync(interactive=True)

