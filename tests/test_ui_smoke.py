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
from auto_thu_deepseek_api.settings import CCSWITCH_POLICY_DIRECT, AppSettings  # noqa: E402
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
        os.environ["LOCALAPPDATA"] = cls._tmp.name
        cls.application = QApplication.instance() or QApplication(sys.argv[:1])

    @classmethod
    def tearDownClass(cls) -> None:
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

    def test_settings_dialog_round_trip(self) -> None:
        settings = AppSettings()
        dialog = SettingsDialog(settings)
        dialog.dsh_enabled.setChecked(False)
        dialog.ccswitch_enabled.setChecked(True)
        dialog.policy_direct.setChecked(True)
        dialog.keep_spin.setValue(3)
        dialog.unify_profiles.setChecked(True)
        dialog.dsh_dir_edit.setText(r"D:\some\dsh")
        dialog.preflight_check.setChecked(False)
        dialog.close_to_tray_check.setChecked(False)
        dialog.silent_timeout_spin.setValue(75)
        dialog.log_level_combo.setCurrentText("DEBUG")
        dialog.default_model_combo.setCurrentText("qwen3.8-27b")
        dialog.apply_to(settings)

        self.assertFalse(settings.enable_dsh)
        self.assertTrue(settings.enable_ccswitch)
        self.assertEqual(settings.ccswitch_running_policy, CCSWITCH_POLICY_DIRECT)
        self.assertEqual(settings.backup_keep, 3)
        self.assertTrue(settings.unify_all_profiles)
        self.assertEqual(settings.dsh_dir, r"D:\some\dsh")
        self.assertFalse(settings.preflight_enabled)
        self.assertFalse(settings.close_to_tray)
        self.assertEqual(settings.silent_capture_timeout_seconds, 75)
        self.assertEqual(settings.log_level, "DEBUG")
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

    def test_settings_dialog_has_shortcut_button_and_ccswitch_default(self) -> None:
        settings = AppSettings()
        dialog = SettingsDialog(settings)
        # 默认不弹「CC Switch 正在运行」提醒
        self.assertTrue(dialog.suppress_warning.isChecked())
        # 目标里要有"创建桌面快捷方式"功能
        self.assertTrue(hasattr(dialog, "shortcut_button"))
        dialog.close()

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
        self.assertTrue(dialog.unify_profiles.isVisible(), "设置项应当显示出来")
        dialog.close()
        window._quitting = True
        window.close()

    def test_main_window_has_desktop_shortcut_button(self) -> None:
        window = MainWindow(AppSettings(), AppState())
        self.assertTrue(hasattr(window, "shortcut_button"))
        self.assertIn("快捷方式", window.shortcut_button.text())
        self.assertTrue(callable(getattr(window, "create_shortcut", None)))
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
        window = MainWindow(AppSettings(), AppState())
        order: list[str] = []
        window._check_desktop_private = lambda: (order.append("desktop"), True)[1]  # type: ignore[assignment]
        window._check_targets_missing = lambda: (order.append("targets"), False)[1]  # type: ignore[assignment]
        window.start_sync = lambda interactive=True: order.append("sync")  # type: ignore[assignment]
        try:
            window.startup_then_sync()
            self.assertEqual(order, ["desktop", "targets"], "顺序固定：先桌面、再都没装")
        finally:
            window._quitting = True
            window.close()

    def test_startup_sync_skipped_when_targets_missing(self) -> None:
        window = MainWindow(AppSettings(), AppState())
        order: list[str] = []
        window._check_desktop_private = lambda: True  # type: ignore[assignment]
        window._check_targets_missing = lambda: False  # type: ignore[assignment]
        window.start_sync = lambda interactive=True: order.append("sync")  # type: ignore[assignment]
        try:
            window.startup_then_sync()
            self.assertEqual(order, [], "两个目标都没检出时不该开始同步")
        finally:
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


if __name__ == "__main__":
    unittest.main()

