"""写入与还原的行为验证（在临时沙盒里跑，不碰真实配置）。"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api.backup import BackupManager  # noqa: E402
from auto_thu_deepseek_api.paths import TargetPaths  # noqa: E402
from auto_thu_deepseek_api.targets.ccswitch import CcSwitchTarget  # noqa: E402
from auto_thu_deepseek_api.targets.dsh import DshTarget  # noqa: E402

from .sandbox import FAKE_NEW_TOKEN, FAKE_OLD_TOKEN, PROFILE_CROSSED, Sandbox  # noqa: E402

MODELS = [
    {"id": "DeepSeek-V4.1-Flash", "name": "DeepSeek-V4.1-Flash (THU)", "contextWindow": 1000000, "input": ["text", "image"]},
    {"id": "qwen3.8-27b", "name": "qwen3.8-27b (THU)", "contextWindow": 1000000, "input": ["text", "image"]},
]


class TargetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sandbox = Sandbox.create(self.root)
        self.backup = BackupManager(self.sandbox.backups_dir, keep=5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # —— DSH ——

    def test_dsh_writes_token_and_keeps_other_keys(self) -> None:
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.changed)

        text = (self.sandbox.dsh_dir / ".credentials.yaml").read_text(encoding="utf-8")
        self.assertIn(FAKE_NEW_TOKEN, text)
        self.assertIn("TSINGHUA_API_KEY", text)
        self.assertIn("client-connection/browser-session", text)

        from ruamel.yaml import YAML

        parsed = YAML(typ="safe").load(text)
        self.assertEqual(parsed["refs"]["MADMODEL_API_KEY"], FAKE_NEW_TOKEN)
        self.assertEqual(parsed["refs"]["TSINGHUA_API_KEY"], FAKE_OLD_TOKEN)

    def test_dsh_only_touches_profiles_with_madmodel(self) -> None:
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        web_before = (self.sandbox.dsh_dir / "profiles" / "web" / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertNotIn("madmodel", web_before)
        # 注释与其它条目要原样保留
        desktop = (self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn("# Your patch layer for this dsh profile", desktop)
        self.assertIn("ui-chat", desktop)

    def test_unify_writes_every_profile(self) -> None:
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            ),
            unify_all_profiles=True,
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        web = (self.sandbox.dsh_dir / "profiles" / "web" / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn("madmodel", web)
        self.assertIn("provider: madmodel", web)

    def test_dsh_repairs_crossed_fields(self) -> None:
        """DSH 界面把显示名/模型 id 写串时，要能被修回可用的样子。"""

        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        profile.write_text(PROFILE_CROSSED, encoding="utf-8")

        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.changed)

        from ruamel.yaml import YAML

        data = YAML(typ="safe").load(profile.read_text(encoding="utf-8"))
        entries = {entry["id"]: entry for entry in data}
        provider = entries["llm-pi-ai"]["config"]["providers"]["madmodel"]
        self.assertEqual(provider["displayName"], "DeepSeek (THU)")
        self.assertEqual(provider["reasoning"], "medium")
        ids = [model["id"] for model in provider["models"]]
        self.assertEqual(ids, ["DeepSeek-V4.1-Flash", "qwen3.8-27b"])
        self.assertEqual(provider["models"][0]["name"], "DeepSeek-V4.1-Flash (THU)")
        self.assertIn("reasoningEfforts", provider["models"][0])
        self.assertEqual(entries["agent-default-model"]["config"]["model"], "DeepSeek-V4.1-Flash")

    def test_dsh_updates_provider_behind_local_forward(self) -> None:
        """provider 走本地转发（127.0.0.1/v1）时，按 apiKeyEnv 认出来，不再建重复的。"""

        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        profile.write_text(
            """# local forward profile
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      thu-local:
        displayName: 我的中转
        apiKeyEnv: MADMODEL_API_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:8788/v1
        models:
          - id: old-model
            name: 老模型
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: thu-local
    model: old-model
""",
            encoding="utf-8",
        )
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(any("已找到 provider thu-local" in note for note in result.messages), result.messages)

        from ruamel.yaml import YAML

        data = YAML(typ="safe").load(profile.read_text(encoding="utf-8"))
        entries = {entry["id"]: entry for entry in data}
        providers = entries["llm-pi-ai"]["config"]["providers"]
        self.assertEqual(list(providers.keys()), ["thu-local"], "不该另外建一个 madmodel")
        self.assertEqual(providers["thu-local"]["displayName"], "我的中转", "不该覆盖用户自己的名称")
        self.assertEqual([m["id"] for m in providers["thu-local"]["models"]], ["DeepSeek-V4.1-Flash", "qwen3.8-27b"])

    def test_dsh_refuses_when_madmodel_key_is_taken(self) -> None:
        """madmodel 这个键被别的配置占用时，宁可不写也不覆盖。"""

        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        original = """# occupied
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      madmodel:
        displayName: 别人的配置
        api: anthropic
        baseURL: https://example.com/v1
"""
        profile.write_text(original, encoding="utf-8")
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertFalse(result.ok)
        self.assertTrue(any("已被其它配置占用" in message for message in result.errors), result.errors)
        self.assertEqual(profile.read_text(encoding="utf-8"), original, "文件应当原封不动")

    # —— CC Switch ——

    def test_ccswitch_writes_provider_meta_and_routing(self) -> None:
        target = CcSwitchTarget(
            TargetPaths(
                base_dir=self.sandbox.ccswitch_dir,
                db_file=self.sandbox.ccswitch_dir / "cc-switch.db",
                settings_file=self.sandbox.ccswitch_dir / "settings.json",
                live_state_file=self.sandbox.ccswitch_dir / "live-state.json",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        connection = sqlite3.connect(self.sandbox.ccswitch_dir / "cc-switch.db")
        row = connection.execute(
            "SELECT settings_config, meta, is_current FROM providers WHERE id=? AND app_type=?",
            ("deepseek-thu", "codex"),
        ).fetchone()
        settings = json.loads(row[0])
        meta = json.loads(row[1])
        self.assertEqual(settings["auth"]["OPENAI_API_KEY"], FAKE_NEW_TOKEN)
        self.assertEqual(meta["apiFormat"], "openai_chat")
        self.assertEqual(row[2], 1)
        self.assertIn("wire_api = \"responses\"", settings["config"])
        self.assertEqual(settings["modelCatalog"]["models"][1]["model"], "qwen3.8-27b")

        others = connection.execute(
            "SELECT is_current FROM providers WHERE app_type='codex' AND id='codex-official'"
        ).fetchone()
        self.assertEqual(others[0], 0)

        legacy = connection.execute(
            "SELECT settings_config FROM providers WHERE id='madmodel-legacy'"
        ).fetchone()
        self.assertEqual(json.loads(legacy[0])["auth"]["OPENAI_API_KEY"], FAKE_NEW_TOKEN)
        connection.close()

        settings_json = json.loads((self.sandbox.ccswitch_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings_json["currentProviderCodex"], "deepseek-thu")
        self.assertTrue(settings_json["showInTray"])

        live = json.loads((self.sandbox.ccswitch_dir / "live-state.json").read_text(encoding="utf-8"))
        self.assertEqual(live["apps"]["codex"]["proxy_route"], "deepseek-thu")
        # contract 不动
        self.assertEqual(live["apps"]["codex"]["contract"]["key"], "deadbeef")
        self.assertEqual(live["apps"]["claude"]["mode"], "direct")

    def test_ccswitch_creates_missing_card(self) -> None:
        connection = sqlite3.connect(self.sandbox.ccswitch_dir / "cc-switch.db")
        connection.execute("DELETE FROM providers WHERE id='deepseek-thu'")
        connection.commit()
        connection.close()

        target = CcSwitchTarget(
            TargetPaths(
                base_dir=self.sandbox.ccswitch_dir,
                db_file=self.sandbox.ccswitch_dir / "cc-switch.db",
                settings_file=self.sandbox.ccswitch_dir / "settings.json",
                live_state_file=self.sandbox.ccswitch_dir / "live-state.json",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        connection = sqlite3.connect(self.sandbox.ccswitch_dir / "cc-switch.db")
        row = connection.execute(
            "SELECT name, settings_config, meta FROM providers WHERE id='deepseek-thu'"
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], "DeepSeek (THU)")
        self.assertEqual(json.loads(row[1])["auth"]["OPENAI_API_KEY"], FAKE_NEW_TOKEN)
        self.assertEqual(json.loads(row[2])["apiFormat"], "openai_chat")

    def test_ccswitch_second_run_touches_nothing(self) -> None:
        """没有变化就别再备份、别再写库（CC Switch 运行中时这点很重要）。"""

        target = CcSwitchTarget(
            TargetPaths(
                base_dir=self.sandbox.ccswitch_dir,
                db_file=self.sandbox.ccswitch_dir / "cc-switch.db",
                settings_file=self.sandbox.ccswitch_dir / "settings.json",
                live_state_file=self.sandbox.ccswitch_dir / "live-state.json",
            )
        )
        first = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(first.changed)
        backups_after_first = len(self.backup.entries())

        second = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(second.ok, second.errors)
        self.assertFalse(second.changed)
        self.assertEqual(len(self.backup.entries()), backups_after_first)
        self.assertTrue(any("未改动 CC Switch 数据库" in note for note in second.messages), second.messages)

    # —— 备份与还原 ——

    def test_backup_and_restore_returns_original(self) -> None:
        credentials = self.sandbox.dsh_dir / ".credentials.yaml"
        original = credentials.read_text(encoding="utf-8")
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=credentials,
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertNotEqual(credentials.read_text(encoding="utf-8"), original)

        manager = BackupManager(self.sandbox.backups_dir, keep=5)
        messages = manager.restore()
        self.assertTrue(any("credentials" in message for message in messages), messages)
        self.assertEqual(credentials.read_text(encoding="utf-8"), original)

    def test_backup_prunes_old_snapshots(self) -> None:
        credentials = self.sandbox.dsh_dir / ".credentials.yaml"
        for token in ("a", "b", "c"):
            (self.root / f"copy-{token}.yaml").write_text(credentials.read_text(encoding="utf-8"), encoding="utf-8")
        for _ in range(7):
            self.backup.backup_file(credentials, "dsh-credentials")
        self.assertLessEqual(len(self.backup.entries()), 5)


if __name__ == "__main__":
    unittest.main()

