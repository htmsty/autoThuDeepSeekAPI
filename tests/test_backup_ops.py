"""手动备份 / 一键还原（界面上那两个按钮背后的逻辑）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import backup_ops  # noqa: E402
from auto_thu_deepseek_api.settings import AppSettings  # noqa: E402

from .sandbox import FAKE_NEW_TOKEN, Sandbox  # noqa: E402


class BackupOpsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sandbox = Sandbox.create(self.root)
        self._original_data_root = backup_ops.paths.data_root
        backup_ops.paths.data_root = lambda: self.root / "private"  # type: ignore[assignment]
        self.settings = AppSettings(
            dsh_dir=str(self.sandbox.dsh_dir),
            ccswitch_dir=str(self.sandbox.ccswitch_dir),
        )

    def tearDown(self) -> None:
        backup_ops.paths.data_root = self._original_data_root  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_manual_backup_then_restore(self) -> None:
        credentials = self.sandbox.dsh_dir / ".credentials.yaml"
        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        original_credentials = credentials.read_text(encoding="utf-8")
        original_profile = profile.read_text(encoding="utf-8")

        done, lines = backup_ops.manual_backup(self.settings)
        self.assertGreaterEqual(done, 4, lines)  # 凭据 + 2 个 profile + cc-switch 若干文件
        self.assertTrue(any("credentials.yaml" in line for line in lines), lines)

        # 模拟"被改坏了"
        credentials.write_text("broken\n", encoding="utf-8")
        profile.write_text("# gone\n", encoding="utf-8")

        messages = backup_ops.restore_original(self.settings)
        self.assertTrue(messages, "应当有还原结果")
        self.assertEqual(credentials.read_text(encoding="utf-8"), original_credentials)
        self.assertEqual(profile.read_text(encoding="utf-8"), original_profile)

    def test_manual_backup_with_missing_files_reports_clearly(self) -> None:
        self.settings.enable_ccswitch = False
        self.settings.enable_dsh = False
        done, lines = backup_ops.manual_backup(self.settings)
        self.assertEqual(done, 0)
        self.assertTrue(any("没有找到可备份的文件" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
