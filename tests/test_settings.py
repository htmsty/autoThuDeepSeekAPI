"""设置：默认值与 v1 → v2 迁移。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import paths  # noqa: E402
from auto_thu_deepseek_api.settings import SETTINGS_VERSION, AppSettings  # noqa: E402


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._original_root = paths.data_root
        paths.data_root = lambda: self.root  # type: ignore[assignment]

    def tearDown(self) -> None:
        paths.data_root = self._original_root  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_default_is_checked(self) -> None:
        settings = AppSettings()
        self.assertTrue(settings.unify_all_profiles, "新装默认应当勾选「统一所有 profile 到 madmodel」")
        self.assertEqual(settings.settings_version, SETTINGS_VERSION)

    def test_v1_settings_are_migrated_once(self) -> None:
        settings_path = self.root / "settings.json"
        settings_path.write_text(
            json.dumps({"unify_all_profiles": False, "sync_interval_minutes": 15}),
            encoding="utf-8",
        )
        migrated = AppSettings.load()
        self.assertTrue(migrated.unify_all_profiles, "老设置要一次性迁移成勾选")
        self.assertEqual(migrated.settings_version, SETTINGS_VERSION)
        self.assertEqual(migrated.sync_interval_minutes, 15, "其它设置不能被改掉")

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_version"], SETTINGS_VERSION)
        self.assertTrue(saved["unify_all_profiles"])

    def test_manual_uncheck_survives_reload(self) -> None:
        settings_path = self.root / "settings.json"
        settings = AppSettings()
        settings.unify_all_profiles = False
        settings.save()

        reloaded = AppSettings.load()
        self.assertFalse(reloaded.unify_all_profiles, "迁移过之后，用户手动取消勾选要保留")

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_version"], SETTINGS_VERSION)


if __name__ == "__main__":
    unittest.main()

