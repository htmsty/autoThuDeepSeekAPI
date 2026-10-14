"""设置：默认值与旧版本迁移。"""

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
        self.assertEqual(migrated.sync_interval_minutes, 15, "用户自己填过的间隔不能被改掉")
        self.assertTrue(migrated.auto_sync_enabled, "迁移后默认开启自动同步")
        self.assertTrue(migrated.suppress_ccswitch_running_warning, "默认不再弹 CC Switch 运行提醒")

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_version"], SETTINGS_VERSION)
        self.assertTrue(saved["unify_all_profiles"])

    def test_defaults_match_goal(self) -> None:
        """目标要求：默认开启自动同步、间隔 180 分钟、默认不弹 CC Switch 提醒。"""

        settings = AppSettings()
        self.assertTrue(settings.auto_sync_enabled)
        self.assertEqual(settings.sync_interval_minutes, 180)
        self.assertTrue(settings.suppress_ccswitch_running_warning)
        self.assertFalse(
            settings.ccswitch_refresh_sibling_cards,
            "默认不该去刷新别人的卡片（要的用户自己开）",
        )

    def test_v2_settings_keep_user_interval(self) -> None:
        settings_path = self.root / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "settings_version": 2,
                    "sync_interval_minutes": 45,
                    "auto_sync_enabled": False,
                    "unify_all_profiles": False,
                }
            ),
            encoding="utf-8",
        )
        migrated = AppSettings.load()
        self.assertEqual(migrated.sync_interval_minutes, 45, "v2 升 v3 不改用户填的间隔")
        self.assertTrue(migrated.auto_sync_enabled)
        self.assertFalse(migrated.unify_all_profiles, "v2 已经迁过的选项不该被二次覆盖")

    def test_non_object_json_falls_back_to_defaults(self) -> None:
        """settings.json 是合法 JSON 但不是对象（手改成数组/字符串）时，
        按默认值启动即可，绝不能抛异常把程序带崩。"""

        path = self.root / "settings.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(AppSettings.load().sync_interval_minutes, 180)
        path.write_text('"nope"', encoding="utf-8")
        self.assertEqual(AppSettings.load().backup_keep, 10)

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

