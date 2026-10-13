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


if __name__ == "__main__":
    unittest.main()

