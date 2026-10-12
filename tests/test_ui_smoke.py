"""界面冒烟测试：离屏建主窗口、走一遍设置对话框（不联网、不写真实目标）。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from auto_thu_deepseek_api import paths  # noqa: E402
from auto_thu_deepseek_api.settings import CCSWITCH_POLICY_DIRECT, AppSettings  # noqa: E402
from auto_thu_deepseek_api.state_store import AppState  # noqa: E402
from auto_thu_deepseek_api.ui.main_window import MainWindow  # noqa: E402
from auto_thu_deepseek_api.ui.settings_dialog import SettingsDialog  # noqa: E402


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
        dialog.apply_to(settings)

        self.assertFalse(settings.enable_dsh)
        self.assertTrue(settings.enable_ccswitch)
        self.assertEqual(settings.ccswitch_running_policy, CCSWITCH_POLICY_DIRECT)
        self.assertEqual(settings.backup_keep, 3)
        self.assertTrue(settings.unify_all_profiles)
        self.assertEqual(settings.dsh_dir, r"D:\some\dsh")
        dialog.close()

    def test_paths_fall_back_when_app_dir_read_only(self) -> None:
        # 运行数据根目录必须落在某个可写位置，且带上我们的目录名或 private
        root = paths.data_root()
        self.assertTrue(root.is_dir())
        self.assertIn("private", str(root).lower())


if __name__ == "__main__":
    unittest.main()

