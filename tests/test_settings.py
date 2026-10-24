"""设置：默认值与旧版本迁移。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import paths  # noqa: E402
from auto_thu_deepseek_api.settings import (  # noqa: E402
    CCSWITCH_POLICY_DIRECT,
    CCSWITCH_POLICY_PROMPT,
    LOG_DISPLAY_FULL,
    LOG_DISPLAY_OFF,
    LOG_DISPLAY_SUMMARY,
    SETTINGS_VERSION,
    AppSettings,
)


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._original_root = paths.data_root
        paths.data_root = lambda: self.root  # type: ignore[assignment]

    def tearDown(self) -> None:
        paths.data_root = self._original_root  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_safe_defaults(self) -> None:
        settings = AppSettings()
        self.assertEqual(settings.settings_version, SETTINGS_VERSION)
        self.assertFalse(settings.auto_login_enabled)
        self.assertFalse(settings.chat_verification_enabled)
        self.assertEqual(settings.log_display_mode, LOG_DISPLAY_FULL)
        self.assertTrue(settings.enable_ccswitch_card)
        self.assertFalse(settings.enable_ccswitch_activate)

    def test_log_display_mode_round_trip_and_invalid_value_falls_back(self) -> None:
        settings = AppSettings(log_display_mode=LOG_DISPLAY_SUMMARY)
        settings.save()
        self.assertEqual(AppSettings.load().log_display_mode, LOG_DISPLAY_SUMMARY)

        path = self.root / "settings.json"
        path.write_text(
            json.dumps({"log_display_mode": "unexpected"}),
            encoding="utf-8",
        )
        self.assertEqual(AppSettings.load().log_display_mode, LOG_DISPLAY_FULL)

        settings.log_display_mode = LOG_DISPLAY_OFF
        settings.normalize()
        self.assertEqual(settings.log_display_mode, LOG_DISPLAY_OFF)

    def test_v1_settings_are_migrated_once(self) -> None:
        settings_path = self.root / "settings.json"
        settings_path.write_text(
            json.dumps({"sync_interval_minutes": 15}),
            encoding="utf-8",
        )
        migrated = AppSettings.load()
        self.assertEqual(migrated.settings_version, SETTINGS_VERSION)
        self.assertEqual(migrated.sync_interval_minutes, 15, "用户自己填过的间隔不能被改掉")
        self.assertTrue(migrated.auto_sync_enabled, "迁移后默认开启自动同步")

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_version"], SETTINGS_VERSION)
        self.assertNotIn("unify_all_profiles", saved)

    def test_defaults_match_goal(self) -> None:
        """默认开启自动同步、60 分钟，并将高风险 CC Switch 路由激活关闭。"""

        settings = AppSettings()
        self.assertTrue(settings.auto_sync_enabled)
        self.assertEqual(settings.sync_interval_minutes, 60)
        self.assertEqual(settings.ccswitch_running_policy, CCSWITCH_POLICY_PROMPT)
        self.assertFalse(settings.enable_ccswitch_activate)
        self.assertFalse(
            settings.ccswitch_refresh_sibling_cards,
            "默认不该去刷新别人的卡片（要的用户自己开）",
        )

    def test_v3_policy_merges_suppress_switch(self) -> None:
        """v3 → v4：老的「不再弹提醒」复选框合并进「运行策略」这一个选项。"""

        path = self.root / "settings.json"
        path.write_text(
            json.dumps({"settings_version": 3, "suppress_ccswitch_running_warning": False}),
            encoding="utf-8",
        )
        settings = AppSettings.load()
        self.assertEqual(
            settings.ccswitch_running_policy,
            CCSWITCH_POLICY_PROMPT,
            "明确要求提醒的用户要保持在「先提示」",
        )
        self.assertEqual(settings.settings_version, SETTINGS_VERSION)

        path.write_text(
            json.dumps({"settings_version": 3, "suppress_ccswitch_running_warning": True}),
            encoding="utf-8",
        )
        self.assertEqual(
            AppSettings.load().ccswitch_running_policy,
            CCSWITCH_POLICY_DIRECT,
            "老默认「不再弹提醒」等于「直接改」",
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

    def test_non_object_json_falls_back_to_defaults(self) -> None:
        """settings.json 是合法 JSON 但不是对象（手改成数组/字符串）时，
        按默认值启动即可，绝不能抛异常把程序带崩。"""

        path = self.root / "settings.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(AppSettings.load().sync_interval_minutes, 60)
        path.write_text('"nope"', encoding="utf-8")
        self.assertEqual(AppSettings.load().backup_keep, 10)

    def test_manual_safe_switches_survive_reload(self) -> None:
        settings_path = self.root / "settings.json"
        settings = AppSettings()
        settings.enable_ccswitch_activate = True
        settings.auto_login_enabled = False
        settings.save()

        reloaded = AppSettings.load()
        self.assertTrue(reloaded.enable_ccswitch_activate)
        self.assertFalse(reloaded.auto_login_enabled)

        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["settings_version"], SETTINGS_VERSION)
        self.assertNotIn("unify_all_profiles", saved)

    def test_v4_default_180_migrates_to_60_but_user_value_is_kept(self) -> None:
        path = self.root / "settings.json"
        path.write_text(
            json.dumps({"settings_version": 4, "sync_interval_minutes": 180}),
            encoding="utf-8",
        )
        self.assertEqual(AppSettings.load().sync_interval_minutes, 60)
        path.write_text(
            json.dumps({"settings_version": 4, "sync_interval_minutes": 45}),
            encoding="utf-8",
        )
        self.assertEqual(AppSettings.load().sync_interval_minutes, 45)


if __name__ == "__main__":
    unittest.main()

