"""Field-level undo: only our finalized values are restored."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ruamel.yaml import YAML  # noqa: E402

from auto_thu_deepseek_api import backup_ops, paths  # noqa: E402
from auto_thu_deepseek_api.settings import AppSettings  # noqa: E402
from auto_thu_deepseek_api.sync import run_sync  # noqa: E402

from .sandbox import FAKE_NEW_TOKEN, FAKE_OLD_TOKEN, Sandbox  # noqa: E402


class ChangeJournalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sandbox = Sandbox.create(self.root)
        self._old_root = paths.data_root
        paths.data_root = lambda: self.root / "private"  # type: ignore[assignment]
        self.settings = AppSettings(
            dsh_dir=str(self.sandbox.dsh_dir),
            ccswitch_dir=str(self.sandbox.ccswitch_dir),
            preflight_enabled=False,
        )

    def tearDown(self) -> None:
        paths.data_root = self._old_root  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_restore_undoes_managed_fields_and_keeps_user_changes(self) -> None:
        credentials = self.sandbox.dsh_dir / ".credentials.yaml"
        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        old_credentials = credentials.read_bytes()
        old_profile = profile.read_text(encoding="utf-8")

        report = run_sync(
            self.settings,
            FAKE_NEW_TOKEN,
            verify=False,
            fetch_model_list=False,
        )
        self.assertTrue(report.ok, report.summary())

        # A later user change outside our managed fields must survive.
        data = YAML(typ="rt").load(profile.read_text(encoding="utf-8"))
        entries = {item["id"]: item for item in data if isinstance(item, dict)}
        entries["llm-pi-ai"]["config"]["userOwned"] = "keep-me"
        from io import StringIO

        stream = StringIO()
        YAML(typ="rt").dump(data, stream)
        profile.write_text(stream.getvalue(), encoding="utf-8")
        user_modified = profile.read_text(encoding="utf-8")

        messages = backup_ops.restore_original(self.settings)
        self.assertTrue(messages)
        self.assertEqual(credentials.read_bytes(), old_credentials, "\n".join(messages))
        restored = YAML(typ="rt").load(profile.read_text(encoding="utf-8"))
        restored_entries = {item["id"]: item for item in restored if isinstance(item, dict)}
        self.assertEqual(
            restored_entries["llm-pi-ai"]["config"]["userOwned"],
            "keep-me",
            "user-added field should survive field-level undo",
        )
        restored_provider = restored_entries["llm-pi-ai"]["config"]["providers"]["madmodel"]
        before = YAML(typ="rt").load(old_profile)
        before_entries = {item["id"]: item for item in before if isinstance(item, dict)}
        before_provider = before_entries["llm-pi-ai"]["config"]["providers"]["madmodel"]
        self.assertEqual(restored_provider.get("models"), before_provider.get("models"))
        self.assertNotEqual(profile.read_text(encoding="utf-8"), user_modified)

        db = self.sandbox.ccswitch_dir / "cc-switch.db"
        connection = sqlite3.connect(db)
        raw = connection.execute(
            "SELECT settings_config FROM providers WHERE id='deepseek-thu' AND app_type='codex'"
        ).fetchone()[0]
        connection.close()
        card = json.loads(raw)
        self.assertEqual(card["auth"]["OPENAI_API_KEY"], FAKE_OLD_TOKEN)

    def test_conflicting_token_is_not_overwritten(self) -> None:
        credentials = self.sandbox.dsh_dir / ".credentials.yaml"
        report = run_sync(
            self.settings,
            FAKE_NEW_TOKEN,
            verify=False,
            fetch_model_list=False,
        )
        self.assertTrue(report.ok, report.summary())
        text = credentials.read_text(encoding="utf-8")
        text = text.replace(FAKE_NEW_TOKEN, "user-put-a-different-token")
        credentials.write_text(text, encoding="utf-8")

        messages = backup_ops.restore_original(self.settings)
        self.assertTrue(any("冲突" in message for message in messages), messages)
        self.assertIn("user-put-a-different-token", credentials.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
