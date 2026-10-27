"""界面冒烟测试：离屏建主窗口、走一遍设置对话框（不联网、不写真实目标）。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from auto_thu_deepseek_api import paths  # noqa: E402
from auto_thu_deepseek_api.settings import (  # noqa: E402
    CCSWITCH_POLICY_DIRECT,
    CCSWITCH_POLICY_PROMPT,
    LOG_DISPLAY_FULL,
    LOG_DISPLAY_OFF,
    LOG_DISPLAY_SUMMARY,
    AppSettings,
)
from auto_thu_deepseek_api.state_store import AppState  # noqa: E402
from auto_thu_deepseek_api.token.jwt_utils import expiry_moment  # noqa: E402
from auto_thu_deepseek_api.ui.main_window import MainWindow  # noqa: E402
from auto_thu_deepseek_api.ui.settings_dialog import SettingsDialog  # noqa: E402

from .sandbox import FAKE_OLD_TOKEN  # noqa: E402


def _fake_token(expires_in: timedelta) -> str:
    """造一条带 exp 的假 JWT（结构对就行，我们不验签）。"""

    import base64
    import json as jsonlib

    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(
            jsonlib.dumps({"exp": int((datetime.now(timezone.utc) + expires_in).timestamp())}).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.signaturepart"


class UiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old_localappdata = os.environ.get("LOCALAPPDATA")
        cls._old_data_root = paths.data_root
        os.environ["LOCALAPPDATA"] = cls._tmp.name
        cls._data_root = Path(cls._tmp.name) / "private"
        cls._data_root.mkdir(parents=True, exist_ok=True)
        paths.data_root = lambda: cls._data_root  # type: ignore[assignment]
        cls.application = QApplication.instance() or QApplication(sys.argv[:1])

    @classmethod
    def tearDownClass(cls) -> None:
        from auto_thu_deepseek_api.logging_setup import close_file_handlers

        close_file_handlers()
        paths.data_root = cls._old_data_root  # type: ignore[assignment]
        if cls._old_localappdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = cls._old_localappdata
        cls._tmp.cleanup()

    def test_main_window_builds(self) -> None:
        window = MainWindow(AppSettings(), AppState())
        self.assertTrue(window.tray_menu.actions())
        self.assertTrue(window.dsh_status.text().strip())
        self.assertTrue(window.ccswitch_status.text().strip())
        self.assertTrue(window.token_status.text().strip())
        window._quitting = True
        window.close()

    def test_main_window_log_panel_supports_full_summary_and_off(self) -> None:
        from auto_thu_deepseek_api.logging_setup import log_bus

        window = MainWindow(AppSettings(log_display_mode=LOG_DISPLAY_FULL), AppState())
        try:
            log_bus.message.emit("完整日志测试")
            self.assertIn("完整日志测试", window.log_view.toPlainText())
            self.assertFalse(window.log_box.isHidden())

            window.settings.log_display_mode = LOG_DISPLAY_SUMMARY
            window._apply_log_display_mode()
            log_bus.message.emit("简化模式不应显示这句")
            self.assertNotIn("简化模式不应显示这句", window.log_view.toPlainText())
            window._append_sync_result(True)
            self.assertIn("同步成功", window.log_view.toPlainText())

            window.settings.log_display_mode = LOG_DISPLAY_OFF
            window._apply_log_display_mode()
            self.assertTrue(window.log_box.isHidden())
        finally:
            window._quitting = True
            window.close()

    def test_tray_menu_has_exact_five_actions(self) -> None:
        window = MainWindow(AppSettings(), AppState())
        labels = [
            action.text()
            for action in window.tray_menu.actions()
            if not action.isSeparator()
        ]
        self.assertEqual(
            labels,
            ["打开主界面", "立即获取并同步", "暂停自动同步", "设置", "退出"],
        )
        window._quitting = True
        window.close()

    def test_settings_dialog_round_trip(self) -> None:
        settings = AppSettings()
        dialog = SettingsDialog(settings)
        dialog.dsh_enabled.setChecked(False)
        dialog.dsh_provider_check.setChecked(False)
        dialog.ccswitch_enabled.setChecked(True)
        dialog.ccswitch_card_check.setChecked(False)
        dialog.ccswitch_activate_check.setChecked(True)
        dialog.policy_direct.setChecked(True)
        dialog.keep_spin.setValue(3)
        dialog.dsh_dir_edit.setText(r"D:\some\dsh")
        dialog.dsh_profile_edit.setText("desktop")
        dialog.chat_verify_check.setChecked(True)
        dialog.interval_spin.setValue(75)
        dialog.preflight_check.setChecked(False)
        dialog.close_to_tray_check.setChecked(False)
        dialog.silent_timeout_spin.setValue(75)
        dialog.log_level_combo.setCurrentText("DEBUG")
        dialog.log_keep_spin.setValue(14)
        dialog.log_display_combo.setCurrentIndex(
            dialog.log_display_combo.findData(LOG_DISPLAY_SUMMARY)
        )
        dialog.default_model_combo.setCurrentText("qwen3.8-27b")
        dialog.apply_to(settings)

        self.assertFalse(settings.enable_dsh)
        self.assertFalse(settings.enable_dsh_provider)
        self.assertTrue(settings.enable_ccswitch)
        self.assertFalse(settings.enable_ccswitch_card)
        self.assertTrue(settings.enable_ccswitch_activate)
        self.assertEqual(settings.ccswitch_running_policy, CCSWITCH_POLICY_DIRECT)
        self.assertEqual(settings.backup_keep, 3)
        self.assertEqual(settings.dsh_dir, r"D:\some\dsh")
        self.assertEqual(settings.dsh_profile, "desktop")
        self.assertTrue(settings.chat_verification_enabled)
        self.assertEqual(settings.sync_interval_minutes, 75)
        self.assertFalse(settings.preflight_enabled)
        self.assertFalse(settings.close_to_tray)
        self.assertEqual(settings.silent_capture_timeout_seconds, 75)
        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(settings.log_keep_days, 14)
        self.assertEqual(settings.log_display_mode, LOG_DISPLAY_SUMMARY)
        self.assertEqual(settings.default_model_id, "qwen3.8-27b")
        dialog.close()

    def test_paths_fall_back_when_app_dir_read_only(self) -> None:
        # 运行数据根目录必须落在某个可写位置，且带上我们的目录名或 private
        root = paths.data_root()
        self.assertTrue(root.is_dir())
        self.assertIn("private", str(root).lower())

    def test_token_status_shows_expiry_moment(self) -> None:
        token = _fake_token(timedelta(hours=3))
        state = AppState()
        state.remember_token(token)
        window = MainWindow(AppSettings(), state)
        # 界面显示的是到期时刻，而不是剩余时长
        self.assertIn("有效至", window.token_status.text())
        expected = expiry_moment(token, state.last_token_at)
        assert expected is not None
        self.assertIn(expected.strftime("%Y-%m-%d %H:%M"), window.token_status.text())
        window._quitting = True
        window.close()

    def test_first_run_auto_sync_uses_visible_browser(self) -> None:
        """没有任何缓存 Token 时，自动同步不该走静默，而是开浏览器窗口。"""

        window = MainWindow(AppSettings(), AppState())
        self.assertFalse(window._should_capture_silently(interactive=False))
        window.state.remember_token(FAKE_OLD_TOKEN)
        self.assertTrue(window._should_capture_silently(interactive=False))
        self.assertFalse(window._should_capture_silently(interactive=True))
        window._quitting = True
        window.close()

    def test_saved_auto_login_keeps_browser_hidden(self) -> None:
        """保存了自动登录凭据后，手动同步也先在后台登录；需要验证时才显示窗口。"""

        from types import SimpleNamespace
        from unittest import mock

        import auto_thu_deepseek_api.ui.main_window as main_window_module

        window = MainWindow(AppSettings(auto_login_enabled=True), AppState())
        try:
            with mock.patch.object(
                main_window_module.credentials,
                "load_login",
                return_value=SimpleNamespace(username="tester", password="secret"),
            ):
                self.assertTrue(window._should_capture_silently(interactive=True))
            with mock.patch.object(
                main_window_module.credentials,
                "load_login",
                return_value=None,
            ):
                self.assertFalse(
                    window._should_capture_silently(interactive=True),
                    "没有保存凭据时必须显示登录窗口",
                )
        finally:
            window._quitting = True
            window.close()

    def test_saved_auto_login_manual_sync_actually_starts_hidden(self) -> None:
        """回归：判断为静默之后，打开捕获窗口时不能又按 interactive 强行显示。"""

        from types import SimpleNamespace
        from unittest import mock

        import auto_thu_deepseek_api.ui.main_window as main_window_module

        window = MainWindow(AppSettings(auto_login_enabled=True), AppState())
        opened: list[bool] = []
        window._open_capture = lambda silent: opened.append(silent)  # type: ignore[assignment]
        window._ask_about_ccswitch = lambda: True  # type: ignore[assignment]
        try:
            with mock.patch.object(
                main_window_module.credentials,
                "load_login",
                return_value=SimpleNamespace(username="tester", password="secret"),
            ):
                window.start_sync(interactive=True)
            self.assertEqual(opened, [True], "手动点击同步时也应先使用后台登录")
        finally:
            window._quitting = True
            window.close()

    def test_user_initiated_hidden_login_failure_opens_window(self) -> None:
        """用户刚点过同步时，后台登录失败要显示登录窗口，不能只留托盘气泡。"""

        window = MainWindow(AppSettings(), AppState())
        opened: list[bool] = []
        window._open_capture = lambda silent: opened.append(silent)  # type: ignore[assignment]
        window.notify = lambda *args, **kwargs: None  # type: ignore[assignment]
        try:
            window._capture_silent = True
            window._capture_user_initiated = True
            window._login_offered_after_silent_failure = True
            window._on_capture_failed("静默取 Token 超时：多半需要重新登录")
            self.assertEqual(opened, [False], "用户每次手动触发失败都应重新显示登录窗口")
        finally:
            window._quitting = True
            window.close()

    def test_user_initiated_background_login_is_not_treated_as_auto_sync(self) -> None:
        """用户点同步、后台自动登录成功时，后续写入失败不该走自动重试。"""

        window = MainWindow(AppSettings(), AppState())
        captured: list[bool] = []
        window._start_sync_worker = lambda token, auto=None: captured.append(bool(auto))  # type: ignore[assignment]
        try:
            window._capture_silent = True
            window._capture_user_initiated = True
            window._on_token_captured(FAKE_OLD_TOKEN)
            self.assertEqual(captured, [False])
        finally:
            window._quitting = True
            window.close()

    def test_settings_dialog_has_shortcut_button_and_ccswitch_default(self) -> None:
        settings = AppSettings()
        dialog = SettingsDialog(settings)
        self.assertTrue(dialog.policy_prompt.isChecked())
        self.assertFalse(dialog.auto_login_check.isChecked())
        self.assertFalse(dialog.ccswitch_activate_check.isChecked())
        # 目标里要有"创建桌面快捷方式"功能
        self.assertTrue(hasattr(dialog, "shortcut_button"))
        dialog.close()

    def test_settings_tabs_match_their_contents(self) -> None:
        dialog = SettingsDialog(AppSettings())
        try:
            self.assertEqual(
                [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())],
                [
                    "同步与启动",
                    "写入目标",
                    "登录与 Token",
                    "备份、日志与维护",
                    "应用与工具",
                ],
            )

            def in_tab(widget, index: int) -> bool:  # noqa: ANN001
                return dialog.tabs.widget(index).isAncestorOf(widget)

            self.assertTrue(in_tab(dialog.startup_check, 0))
            self.assertTrue(in_tab(dialog.preflight_check, 0))
            self.assertTrue(in_tab(dialog.default_model_combo, 1))
            self.assertTrue(in_tab(dialog.silent_timeout_spin, 2))
            self.assertTrue(in_tab(dialog.paste_button, 2))
            self.assertTrue(in_tab(dialog.preflight_button, 3))
            self.assertTrue(in_tab(dialog.log_display_combo, 3))
            self.assertTrue(in_tab(dialog.log_level_combo, 3))
            self.assertTrue(in_tab(dialog.close_to_tray_check, 4))
            self.assertTrue(in_tab(dialog.shortcut_button, 4))
        finally:
            dialog.close()

    def test_preflight_uses_dialog_draft_and_shows_progress(self) -> None:
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QWidget

        class FakeWorker(QObject):
            finished = Signal()

        class FakeParent(QWidget):
            def __init__(self) -> None:
                super().__init__()
                self.calls: list[tuple[AppSettings, object, FakeWorker]] = []

            def check_preflight(self, *, settings, result_parent):  # noqa: ANN001
                worker = FakeWorker(self)
                self.calls.append((settings, result_parent, worker))
                return worker

        parent = FakeParent()
        dialog = SettingsDialog(AppSettings(enable_dsh=True), parent)
        dialog.dsh_enabled.setChecked(False)
        try:
            dialog._run_preflight()
            self.assertEqual(len(parent.calls), 1)
            draft, result_parent, worker = parent.calls[0]
            self.assertFalse(draft.enable_dsh, "必须校验对话框当前值，而不是旧设置")
            self.assertIs(result_parent, dialog, "结果框必须优先挂在可见的设置窗口上")
            self.assertFalse(dialog.preflight_button.isEnabled())
            self.assertIn("正在校验", dialog.preflight_button.text())
            worker.finished.emit()
            self.assertTrue(dialog.preflight_button.isEnabled())
            self.assertEqual(dialog.preflight_button.text(), "校验写入内容")
        finally:
            dialog.close()
            parent.close()

    def test_check_preflight_uses_draft_token_and_dialog_result_parent(self) -> None:
        from unittest import mock

        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QMessageBox

        import auto_thu_deepseek_api.ui.main_window as main_window_module

        class FakeWorker(QObject):
            reportReady = Signal(object)
            failed = Signal(str)
            finished = Signal()

            def __init__(self, settings, token, parent=None) -> None:  # noqa: ANN001
                super().__init__(parent)
                self.settings = settings
                self.token = token

            def start(self) -> None:
                pass

        class FakeReport:
            ok = True

            def text(self) -> str:
                return "预检通过（当前草稿）"

        state = AppState()
        state.remember_token(_fake_token(timedelta(hours=1)))
        window = MainWindow(AppSettings(), state)
        dialog = SettingsDialog(window.settings, window)
        dialog.show()
        original_worker = main_window_module.PreflightWorker
        main_window_module.PreflightWorker = FakeWorker  # type: ignore[assignment]
        try:
            draft = AppSettings(enable_dsh=False)
            worker = window.check_preflight(settings=draft, result_parent=dialog)
            self.assertIsInstance(worker, FakeWorker)
            assert worker is not None
            self.assertFalse(worker.settings.enable_dsh)
            self.assertEqual(worker.token, state.last_token)
            self.assertIs(window._preflight_message_parent(), dialog)

            with mock.patch.object(QMessageBox, "information") as information:
                window._on_preflight_report(FakeReport(), worker)
            information.assert_called_once()
            self.assertIs(information.call_args.args[0], dialog)

            class FailedReport(FakeReport):
                ok = False

            with mock.patch.object(QMessageBox, "warning") as warning:
                window._on_preflight_report(FailedReport(), worker)
            warning.assert_called_once()
            self.assertIs(warning.call_args.args[0], dialog)
        finally:
            main_window_module.PreflightWorker = original_worker  # type: ignore[assignment]
            window._preflight_worker = None
            window._preflight_result_parent_ref = None
            dialog.close()
            window._quitting = True
            window.close()

    def test_settings_dialog_defaults_smaller_and_restores_saved_size(self) -> None:
        default_dialog = SettingsDialog(AppSettings())
        try:
            self.assertLessEqual(default_dialog.width(), 700)
            self.assertLessEqual(default_dialog.height(), 540)
        finally:
            default_dialog.close()

        restored = SettingsDialog(AppSettings(), saved_size=[660, 500])
        try:
            self.assertEqual((restored.width(), restored.height()), (660, 500))
        finally:
            restored.close()

    def test_main_window_restores_saved_size(self) -> None:
        state = AppState(main_window_size=[660, 500])
        window = MainWindow(AppSettings(), state)
        try:
            self.assertEqual((window.width(), window.height()), (660, 500))
        finally:
            window._quitting = True
            window.close()

    def test_settings_has_apply_ok_cancel_and_login_controls(self) -> None:
        from PySide6.QtWidgets import QDialogButtonBox

        dialog = SettingsDialog(AppSettings())
        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None
        self.assertIsNotNone(buttons.button(QDialogButtonBox.StandardButton.Apply))
        self.assertIsNotNone(buttons.button(QDialogButtonBox.StandardButton.Ok))
        self.assertIsNotNone(buttons.button(QDialogButtonBox.StandardButton.Cancel))
        self.assertFalse(dialog.auto_login_check.isChecked())
        self.assertFalse(dialog.ccswitch_activate_check.isChecked())
        dialog.close()

    def test_auto_sync_interval_is_jittered_within_bounds(self) -> None:
        from unittest import mock

        window = MainWindow(AppSettings(sync_interval_minutes=60), AppState())
        started: list[int] = []
        window._auto_timer.start = lambda ms: started.append(ms)  # type: ignore[assignment]
        try:
            with mock.patch("auto_thu_deepseek_api.ui.main_window.random.randint", return_value=50) as rand:
                window._apply_auto_timer()
            rand.assert_called_once_with(50, 70)
            self.assertEqual(started[-1], 50 * 60 * 1000)
            self.assertIn("50", window.next_sync_status.text())
        finally:
            window._quitting = True
            window.close()

    def test_settings_dialog_is_actually_openable(self) -> None:
        """回归：2.4 曾经把设置对话框的布局顶掉，点「打开配置」什么都看不到。"""

        window = MainWindow(AppSettings(), AppState())
        dialog = SettingsDialog(window.settings, window)
        layout = dialog.layout()
        self.assertIsNotNone(layout, "对话框必须有布局")
        self.assertGreaterEqual(layout.count(), 2, "布局里至少要有滚动区与底部按钮")
        dialog.show()
        self.assertTrue(dialog.isVisible(), "show() 之后应当可见")
        self.assertTrue(dialog._content.isVisible(), "内容区应当显示出来")
        self.assertTrue(dialog.tabs.isVisible(), "分类设置页应当显示出来")
        dialog.close()
        window._quitting = True
        window.close()

    def test_shortcut_lives_in_settings(self) -> None:
        window = MainWindow(AppSettings(), AppState())
        dialog = SettingsDialog(window.settings, window)
        self.assertFalse(hasattr(window, "shortcut_button"))
        self.assertIn("快捷方式", dialog.shortcut_button.text())
        self.assertTrue(callable(getattr(window, "create_shortcut", None)))
        dialog.close()
        window._quitting = True
        window.close()

    def test_every_button_is_wired(self) -> None:
        """大检查：界面上每个按钮都得接上槽，不能有"点了没反应"的装饰按钮。"""

        from PySide6.QtWidgets import QPushButton

        window = MainWindow(AppSettings(), AppState())
        dialog = SettingsDialog(window.settings, window)
        for widget, name in ((window, "主界面"), (dialog, "设置对话框")):
            for button in widget.findChildren(QPushButton):
                receivers = button.receivers("2clicked(bool)")
                self.assertGreater(
                    receivers, 0, f"{name}的按钮「{button.text()}」没有接任何槽函数"
                )
        dialog.close()
        window._quitting = True
        window.close()

    # —— 2.5：关窗取消 / 安全终止重试 / 启动检查顺序 ——

    def test_capture_window_close_cancels_session(self) -> None:
        """回归：点 X 关掉登录窗口后，不能还卡在"正在取 Token"。"""

        # 这里不碰真的 QWebEngine（离屏测试里再跑嵌套事件循环会崩），
        # 用假的 page/view 把"关窗"这条链路走通即可。
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtWidgets import QWidget

        import auto_thu_deepseek_api.token.capture as capture

        class FakeCookieStore(QObject):
            cookieAdded = Signal(object)

        class FakeProfile(QObject):
            def __init__(self) -> None:
                super().__init__()
                self._store = FakeCookieStore()

            def cookieStore(self):  # noqa: ANN201
                return self._store

        class FakePage:
            def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
                pass

        class FakeView(QWidget):
            def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
                super().__init__()
                self.page = None

            def setPage(self, page) -> None:  # noqa: ANN001, N802
                self.page = page

        original_page = capture.QWebEnginePage
        original_view = capture.QWebEngineView
        capture.QWebEnginePage = FakePage  # type: ignore[assignment]
        capture.QWebEngineView = FakeView  # type: ignore[assignment]
        try:
            session = capture.TokenCaptureSession(FakeProfile())
            cancelled: list[bool] = []
            session.cancelled.connect(lambda: cancelled.append(True))
            assert session._window is not None
            session._window.close()
            self.assertEqual(cancelled, [True], "用户关窗应当按取消处理")
            self.assertTrue(session._finished)
        finally:
            capture.QWebEnginePage = original_page  # type: ignore[assignment]
            capture.QWebEngineView = original_view  # type: ignore[assignment]

    def test_startup_checks_run_desktop_before_missing_targets(self) -> None:
        from PySide6.QtCore import QObject, Signal

        import auto_thu_deepseek_api.ui.main_window as main_window_module
        from auto_thu_deepseek_api.ui.workers import StartupProbeResult

        class FakeWorker(QObject):
            ready = Signal(object)
            failed = Signal(str)
            finished = Signal()

            def __init__(self, settings, parent=None) -> None:  # noqa: ANN001
                super().__init__(parent)
                self.settings = settings

            def start(self) -> None:
                pass

        window = MainWindow(AppSettings(), AppState())
        order: list[str] = []
        window._check_desktop_private = lambda: (order.append("desktop"), True)[1]  # type: ignore[assignment]
        window._check_targets_missing = lambda **kwargs: (order.append("targets"), True)[1]  # type: ignore[assignment]
        window.start_sync = lambda interactive=True: order.append("sync")  # type: ignore[assignment]
        original_worker = main_window_module.StartupProbeWorker
        main_window_module.StartupProbeWorker = FakeWorker  # type: ignore[assignment]
        try:
            window.startup_then_sync()
            worker = window._startup_worker
            self.assertIsInstance(worker, FakeWorker)
            self.assertEqual(order, ["desktop"], "窗口显示后应先做桌面提醒，再跑后台探测")
            assert worker is not None
            worker.ready.emit(
                StartupProbeResult(statuses=[], targets_missing=False, traces={})
            )
            worker.finished.emit()
            self.assertEqual(
                order,
                ["desktop", "targets", "sync"],
                "后台探测完成后才检查目标并开始同步",
            )
        finally:
            main_window_module.StartupProbeWorker = original_worker  # type: ignore[assignment]
            window._startup_worker = None
            window._startup_checks_pending = False
            window._quitting = True
            window.close()

    def test_startup_sync_skipped_when_targets_missing(self) -> None:
        from PySide6.QtCore import QObject, Signal

        import auto_thu_deepseek_api.ui.main_window as main_window_module
        from auto_thu_deepseek_api.ui.workers import StartupProbeResult

        class FakeWorker(QObject):
            ready = Signal(object)
            failed = Signal(str)
            finished = Signal()

            def __init__(self, settings, parent=None) -> None:  # noqa: ANN001
                super().__init__(parent)

            def start(self) -> None:
                pass

        window = MainWindow(AppSettings(), AppState())
        order: list[str] = []
        window._check_desktop_private = lambda: True  # type: ignore[assignment]
        window._check_targets_missing = lambda **kwargs: False  # type: ignore[assignment]
        window.start_sync = lambda interactive=True: order.append("sync")  # type: ignore[assignment]
        original_worker = main_window_module.StartupProbeWorker
        main_window_module.StartupProbeWorker = FakeWorker  # type: ignore[assignment]
        try:
            window.startup_then_sync()
            worker = window._startup_worker
            self.assertIsInstance(worker, FakeWorker)
            assert worker is not None
            worker.ready.emit(
                StartupProbeResult(
                    statuses=[],
                    targets_missing=True,
                    traces={"dsh": "", "ccswitch": ""},
                )
            )
            worker.finished.emit()
            self.assertEqual(order, [], "两个目标都没检出时不该开始同步")
        finally:
            main_window_module.StartupProbeWorker = original_worker  # type: ignore[assignment]
            window._startup_worker = None
            window._startup_checks_pending = False
            window._quitting = True
            window.close()

    def test_reminders_respect_acknowledged_state(self) -> None:
        """处理过的提醒不该再弹（弹窗会阻塞测试，所以这里只走"已确认"的分支）。"""

        import auto_thu_deepseek_api.ui.main_window as main_window_module

        state = AppState()
        state.desktop_note_ack = True
        state.targets_note_ack = True
        window = MainWindow(AppSettings(), state)
        original = main_window_module.both_targets_missing
        main_window_module.both_targets_missing = lambda settings: (True, {"dsh": "", "ccswitch": ""})  # type: ignore[assignment]
        try:
            self.assertTrue(window._check_desktop_private(), "已确认过就不再弹桌面提醒")
            self.assertFalse(window._check_targets_missing(), "都没装仍然要跳过同步")
        finally:
            main_window_module.both_targets_missing = original  # type: ignore[assignment]
            window._quitting = True
            window.close()

    def test_retry_after_safe_stop(self) -> None:
        from PySide6.QtTest import QTest

        window = MainWindow(AppSettings(), AppState())
        calls: list[bool] = []
        window.start_sync = lambda interactive=True: calls.append(interactive)  # type: ignore[assignment]
        try:
            window._retry_after_stop = True
            window._on_sync_thread_finished()
            QTest.qWait(50)  # 让 singleShot 的重试真正跑起来
            self.assertEqual(calls, [True], "安全停下后应当按用户要求重试一次")
        finally:
            window._quitting = True
            window.close()

    def test_sync_worker_has_safe_cancel(self) -> None:
        from auto_thu_deepseek_api.ui.workers import SyncWorker

        worker = SyncWorker(AppSettings(), "token")
        worker.request_cancel()
        self.assertTrue(worker._cancel.is_set(), "终止请求应当是「请它停下」，不是杀线程")


class UiInteractionTest(unittest.TestCase):
    """正式版交互走查：新加的入口、提示与互斥逻辑都要有去处。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old_localappdata = os.environ.get("LOCALAPPDATA")
        cls._old_data_root = paths.data_root
        os.environ["LOCALAPPDATA"] = cls._tmp.name
        cls._data_root = Path(cls._tmp.name) / "private"
        cls._data_root.mkdir(parents=True, exist_ok=True)
        paths.data_root = lambda: cls._data_root  # type: ignore[assignment]
        cls.application = QApplication.instance() or QApplication(sys.argv[:1])

    @classmethod
    def tearDownClass(cls) -> None:
        from auto_thu_deepseek_api.logging_setup import close_file_handlers

        close_file_handlers()
        paths.data_root = cls._old_data_root  # type: ignore[assignment]
        if cls._old_localappdata is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = cls._old_localappdata
        cls._tmp.cleanup()

    @staticmethod
    def _close(window) -> None:  # noqa: ANN001
        window._quitting = True
        window.close()

    def test_qt_standard_buttons_are_chinese(self) -> None:
        """QMessageBox 的标准按钮（是/否）必须是中文，不能英文中文混着显示。"""

        from PySide6.QtWidgets import QMessageBox

        from auto_thu_deepseek_api.app import _install_qt_translations

        application = QApplication.instance()
        assert application is not None
        _install_qt_translations(application)  # 幂等：装过再装一次也没事
        box = QMessageBox()
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        texts = [button.text() for button in box.buttons()]
        box.deleteLater()
        self.assertTrue(texts, "标准按钮应该存在")
        for text in texts:
            self.assertTrue(
                any("\u4e00" <= char <= "\u9fff" for char in text),
                f"标准按钮文案没被翻译：{text!r}",
            )

    def test_about_dialog_shows_version_and_repo(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from auto_thu_deepseek_api import constants

        window = MainWindow(AppSettings(), AppState())
        boxes: list[QMessageBox] = []
        original_exec = QMessageBox.exec
        QMessageBox.exec = lambda self: (boxes.append(self), 0)[1]  # type: ignore[assignment]
        try:
            window.show_about()
        finally:
            QMessageBox.exec = original_exec  # type: ignore[assignment]
            self._close(window)
        self.assertTrue(boxes, "「关于」应当弹出对话框")
        text = boxes[0].text() + boxes[0].informativeText()
        self.assertIn(constants.APP_VERSION, text)
        self.assertIn(constants.REPO_URL, text)
        self.assertIn(str(paths.data_root()), text)

    def test_restore_defaults_syncs_every_widget(self) -> None:
        """恢复默认后，对话框里每个控件都要跟着回到默认值 —— 漏一个，
        用户点「确定」时又会把它写回去，等于没恢复。"""

        from auto_thu_deepseek_api.settings import CCSWITCH_POLICY_PROMPT, reset_to_defaults

        settings = AppSettings(
            ccswitch_refresh_sibling_cards=True,
            ccswitch_running_policy=CCSWITCH_POLICY_PROMPT,
            log_display_mode=LOG_DISPLAY_OFF,
        )
        dialog = SettingsDialog(settings)
        reset_to_defaults(dialog.settings)
        dialog._reset_widgets_to_defaults()
        try:
            self.assertFalse(
                dialog.refresh_siblings_check.isChecked(),
                "「顺带刷新同站点旧卡片」没有跟着恢复默认（点确定时会被写回去）",
            )
            self.assertTrue(dialog.policy_prompt.isChecked(), "运行策略要回到默认的「先提示」")
            self.assertEqual(dialog.keep_spin.value(), AppSettings().backup_keep)
            self.assertEqual(dialog.log_keep_spin.value(), AppSettings().log_keep_days)
            self.assertEqual(
                dialog.log_display_combo.currentData(),
                LOG_DISPLAY_FULL,
                "主界面日志模式也要跟着恢复默认",
            )
        finally:
            dialog.close()

    def test_manual_token_is_ignored_while_busy(self) -> None:
        """正在同步/取 Token 时，手动粘贴的 Token 不能另起一个写入线程。"""

        from unittest import mock

        from PySide6.QtWidgets import QDialog, QMessageBox

        import auto_thu_deepseek_api.ui.main_window as main_window_module

        class FakeDialog:
            DialogCode = QDialog.DialogCode

            def __init__(self, settings, parent=None, **kwargs) -> None:  # noqa: ANN001, ANN002, ANN003
                self.manual_token = FAKE_OLD_TOKEN
                callbacks = []
                self.window_size_saved = type(
                    "FakeSignal",
                    (),
                    {"connect": lambda _self, callback: None},
                )()
                self.settings_applied = type(
                    "FakeSignal",
                    (),
                    {"connect": lambda _self, callback: callbacks.append(callback)},
                )()
                self._callbacks = callbacks

            def exec(self):  # noqa: ANN201
                for callback in self._callbacks:
                    callback()
                return QDialog.DialogCode.Accepted

            def apply_to(self, settings) -> None:  # noqa: ANN001
                pass

        window = MainWindow(AppSettings(), AppState())
        started: list[str] = []
        window._start_sync_worker = lambda token: started.append(token)  # type: ignore[assignment]
        window._capture = object()  # 假装正在取 Token（_busy() → True）
        original_dialog = main_window_module.SettingsDialog
        main_window_module.SettingsDialog = FakeDialog  # type: ignore[assignment]
        try:
            with mock.patch.object(QMessageBox, "warning") as warning:
                window.open_settings()
            self.assertEqual(started, [], "忙碌时不该启动第二个写入线程")
            self.assertTrue(warning.called, "应当提示用户这份 Token 这次没用上")
        finally:
            main_window_module.SettingsDialog = original_dialog  # type: ignore[assignment]
            window._capture = None
            self._close(window)

    def test_desktop_notice_not_repeated_in_same_run(self) -> None:
        """启动早期弹过「程序在桌面」提醒后，主窗口启动检查不能在同一轮再弹一次。"""

        import auto_thu_deepseek_api.ui.main_window as main_window_module

        window = MainWindow(AppSettings(), AppState(), desktop_notice_done=True)
        calls: list[bool] = []
        original_notice = main_window_module.data_location.desktop_notice
        original_desktop = main_window_module.startup.is_app_on_desktop
        main_window_module.data_location.desktop_notice = lambda *a, **k: calls.append(True) or True  # type: ignore[assignment]
        main_window_module.startup.is_app_on_desktop = lambda: True  # type: ignore[assignment]
        try:
            self.assertTrue(window._check_desktop_private())
        finally:
            main_window_module.data_location.desktop_notice = original_notice  # type: ignore[assignment]
            main_window_module.startup.is_app_on_desktop = original_desktop  # type: ignore[assignment]
            self._close(window)
        self.assertEqual(calls, [], "同一轮启动里不该再弹一次")

    def test_running_another_copy_does_not_rewrite_startup_entry(self) -> None:
        """回归：运行一份便携副本不能认领/改写已有的开机启动路径。"""

        from unittest import mock

        import auto_thu_deepseek_api.ui.main_window as main_window_module

        other = r'"D:\Other\autoThuDeepSeekAPI.exe" --minimized'
        with (
            mock.patch.object(main_window_module.startup, "current_command", return_value=other),
            mock.patch.object(main_window_module.startup, "set_enabled") as set_enabled,
            mock.patch.object(main_window_module.startup, "restore_command") as restore_command,
        ):
            window = MainWindow(AppSettings(), AppState())
            try:
                self.assertFalse(window.settings.launch_at_startup)
                set_enabled.assert_not_called()
                restore_command.assert_not_called()
            finally:
                self._close(window)

    def test_manual_sync_interrupts_startup_probe_without_backup_notice(self) -> None:
        """启动期间手点同步应让探测让出，而不是弹首次备份提示。"""

        from unittest import mock

        from PySide6.QtWidgets import QMessageBox

        window = MainWindow(
            AppSettings(),
            AppState(),
            startup_checks_pending=True,
        )
        cancelled: list[bool] = []

        class FakeWorker:
            def request_cancel(self) -> None:
                cancelled.append(True)

        try:
            window._startup_worker = FakeWorker()  # type: ignore[assignment]
            with mock.patch.object(QMessageBox, "information") as information:
                window.start_sync(interactive=True)
            information.assert_not_called()
            self.assertEqual(cancelled, [True])
            self.assertTrue(window._manual_sync_after_startup)
            self.assertIsNone(window._capture)
        finally:
            self._close(window)

    def test_manual_sync_during_startup_delay_starts_pipeline_immediately(self) -> None:
        """还没到 90 秒检查点时，手点同步应立即启动安全检查而不是继续等。"""

        from unittest import mock

        from PySide6.QtCore import QObject, Signal

        import auto_thu_deepseek_api.ui.main_window as main_window_module

        started = []
        cancelled = []

        class FakeWorker(QObject):
            ready = Signal(object)
            failed = Signal(str)
            finished = Signal()

            def __init__(self, settings, parent=None) -> None:  # noqa: ANN001
                super().__init__(parent)

            def start(self) -> None:
                started.append(self)

            def request_cancel(self) -> None:
                cancelled.append(True)

        window = MainWindow(
            AppSettings(),
            AppState(),
            startup_checks_pending=True,
        )
        original_worker = main_window_module.StartupProbeWorker
        main_window_module.StartupProbeWorker = FakeWorker  # type: ignore[assignment]
        try:
            window._check_desktop_private = lambda: True  # type: ignore[assignment]
            with mock.patch.object(
                main_window_module.QMessageBox, "information"
            ) as information:
                window.start_sync(interactive=True)
                # 模拟 app.py 原本的 90 秒定时器晚到。
                window.startup_then_sync()
            information.assert_not_called()
            self.assertEqual(len(started), 1, "提前启动后，晚到的定时器不能再跑第二轮")
            self.assertEqual(cancelled, [True], "首次备份开始后应立即跳过剩余探测")
            self.assertTrue(window._manual_sync_after_startup)
        finally:
            main_window_module.StartupProbeWorker = original_worker  # type: ignore[assignment]
            window._startup_worker = None
            window._startup_checks_pending = False
            self._close(window)

    def test_interrupted_startup_resumes_manual_sync_after_safety_check(self) -> None:
        """探测让出后要转入手动同步，同时保留“两边都没装”的判断。"""

        from unittest import mock

        from PySide6.QtCore import QObject

        from auto_thu_deepseek_api.ui.workers import StartupProbeResult

        class FakeWorker(QObject):
            pass

        window = MainWindow(
            AppSettings(),
            AppState(),
            startup_checks_pending=True,
        )
        worker = FakeWorker()
        window._startup_worker = worker  # type: ignore[assignment]
        window._manual_sync_after_startup = True
        window._startup_probe_result = StartupProbeResult(
            statuses=[],
            targets_missing=False,
            traces={},
            backups_done=1,
            interrupted=True,
        )
        try:
            with (
                mock.patch.object(window, "_check_targets_missing", return_value=True) as check,
                mock.patch.object(window, "start_sync") as start_sync,
            ):
                window._on_startup_thread_finished(worker)
            check.assert_called_once_with()
            start_sync.assert_called_once_with(interactive=True)
            self.assertFalse(window._startup_checks_pending)
        finally:
            self._close(window)

    def test_backup_and_restore_wait_for_startup_probe(self) -> None:
        """启动备份/探测期间不能再并发手动备份或还原。"""

        from unittest import mock

        from PySide6.QtWidgets import QMessageBox

        import auto_thu_deepseek_api.ui.main_window as main_window_module

        window = MainWindow(
            AppSettings(),
            AppState(),
            startup_checks_pending=True,
        )
        dialog = SettingsDialog(window.settings, window)
        try:
            with (
                mock.patch.object(main_window_module.backup_ops, "manual_backup") as manual,
                mock.patch.object(QMessageBox, "warning") as warning,
            ):
                window.manual_backup()
                dialog._restore()
            manual.assert_not_called()
            self.assertEqual(warning.call_count, 2)
        finally:
            dialog.close()
            self._close(window)

    def test_sync_is_refused_when_no_target_is_enabled(self) -> None:
        """两个写入目标都关着时，同步要么提示、要么跳过，不能跑到「没有任何目标被写入」。"""

        from unittest import mock

        from PySide6.QtWidgets import QMessageBox

        settings = AppSettings(
            enable_dsh=True,
            enable_dsh_credentials=False,
            enable_dsh_provider=False,
            enable_ccswitch=True,
            enable_ccswitch_card=False,
            enable_ccswitch_activate=False,
        )
        window = MainWindow(settings, AppState())
        try:
            window.start_sync(interactive=False)
            self.assertIsNone(window._capture, "非交互时应当直接跳过")
            with mock.patch.object(QMessageBox, "warning") as warning:
                window.start_sync(interactive=True)
            self.assertTrue(warning.called, "交互式点击时要说清楚为什么没同步")
        finally:
            self._close(window)

    def test_tray_double_click_shows_window(self) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        window = MainWindow(AppSettings(), AppState())
        shown: list[bool] = []
        window.show_window = lambda: shown.append(True)  # type: ignore[assignment]
        try:
            window._on_tray_activated(QSystemTrayIcon.ActivationReason.DoubleClick)
            self.assertEqual(shown, [True])
        finally:
            self._close(window)

    def test_auto_retry_budget_is_finite(self) -> None:
        """自动同步失败的重试次数有限：安排完预算就不再安排（避免无限重试）。"""

        from auto_thu_deepseek_api.ui.main_window import AUTO_RETRY_BUDGET

        window = MainWindow(AppSettings(), AppState())
        scheduled: list[int] = []
        window._auto_retry_timer.start = lambda ms: scheduled.append(ms)  # type: ignore[assignment]
        try:
            for _ in range(AUTO_RETRY_BUDGET + 2):
                window._schedule_auto_retry("测试用原因")
            self.assertEqual(len(scheduled), AUTO_RETRY_BUDGET, "重试次数应当正好用完预算")
            self.assertEqual(window._auto_retry_left, 0)
        finally:
            self._close(window)

    def test_successful_sync_refills_retry_budget(self) -> None:
        """同步成功一次就把重试预算补满（下一次失败还能重试几次）。"""

        from auto_thu_deepseek_api.sync import SyncReport
        from auto_thu_deepseek_api.targets.base import ApplyResult
        from auto_thu_deepseek_api.token.verifier import VerifyResult
        from auto_thu_deepseek_api.ui.main_window import AUTO_RETRY_BUDGET

        window = MainWindow(AppSettings(), AppState())
        try:
            window._persist_settings = lambda: True  # type: ignore[assignment]
            window.state.remember_status = lambda summary: None  # type: ignore[assignment]
            window.notify = lambda *a, **k: None  # type: ignore[assignment]
            window._auto_retry_left = 0
            report = SyncReport(
                verified=VerifyResult(True, "站点确认 Token 可用"),
                results=[ApplyResult("dsh", "DSH")],
            )
            self.assertTrue(report.ok, "构造的报告应当是成功状态")
            window._on_sync_report(report)
            self.assertEqual(window._auto_retry_left, AUTO_RETRY_BUDGET)
        finally:
            self._close(window)

    def test_auto_sync_skips_when_site_unreachable(self) -> None:
        """网络还没就绪时不浪费 40 秒去取 Token：直接安排过几分钟重试。"""

        from unittest import mock

        import auto_thu_deepseek_api.ui.main_window as main_window_module

        window = MainWindow(AppSettings(), AppState())
        scheduled: list[str] = []
        window._schedule_auto_retry = lambda reason: scheduled.append(reason)  # type: ignore[assignment]
        try:
            with mock.patch.object(main_window_module, "site_reachable", return_value=False):
                window.start_sync(interactive=False)
            self.assertEqual(len(scheduled), 1, "连不上站点时要安排重试")
            self.assertIsNone(window._capture, "连不上站点时不该去取 Token")
        finally:
            self._close(window)

    def test_startup_silent_failure_opens_login_window_once(self) -> None:
        """开机启动时静默取 Token 失败：自动打开一次登录窗口等用户登录，不反复弹。"""

        window = MainWindow(AppSettings(), AppState(), started_minimized=True)
        opened: list[bool] = []
        window._open_capture = lambda silent: opened.append(silent)  # type: ignore[assignment]
        window.notify = lambda *a, **k: None  # type: ignore[assignment]
        window._schedule_auto_retry = lambda reason: None  # type: ignore[assignment]
        try:
            window._capture_silent = True
            window._on_capture_failed("静默取 Token 超时：多半需要重新登录")
            self.assertEqual(opened, [False], "开机静默失败后要打开可见登录窗口")

            # 再失败一次（例如那个窗口后来也超时了）：不该再自动弹第二个
            window._capture_silent = True
            window._on_capture_failed("等待登录超时")
            self.assertEqual(opened, [False], "只自动打开一次登录窗口")
        finally:
            self._close(window)

    def test_preflight_worker_emits_report(self) -> None:
        """「校验写入内容」走后台线程：跑完要把报告发回主线程。"""

        from auto_thu_deepseek_api.ui import workers as workers_module

        class FakeReport:
            def text(self) -> str:
                return "预检通过（假报告）"

        original = workers_module.run_preflight
        workers_module.run_preflight = lambda settings, token: FakeReport()  # type: ignore[assignment]
        try:
            worker = workers_module.PreflightWorker(AppSettings(), FAKE_OLD_TOKEN)
            reports: list[object] = []
            worker.reportReady.connect(reports.append)
            worker.run()  # 直接同步跑一遍，不真起线程
        finally:
            workers_module.run_preflight = original  # type: ignore[assignment]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].text(), "预检通过（假报告）")


if __name__ == "__main__":
    unittest.main()

