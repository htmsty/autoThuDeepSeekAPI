"""预检：将要写入模型文件的内容必须合法，而且和真正写下去的内容一致。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api.backup import BackupManager  # noqa: E402
from auto_thu_deepseek_api.preflight import run_preflight  # noqa: E402
from auto_thu_deepseek_api.settings import AppSettings  # noqa: E402
from auto_thu_deepseek_api.sync import build_targets  # noqa: E402

from .sandbox import FAKE_NEW_TOKEN, PROFILE_WITH_JS, Sandbox  # noqa: E402

MODELS = [
    {
        "id": "DeepSeek-V4.1-Flash",
        "name": "DeepSeek-V4.1-Flash (THU)",
        "contextWindow": 1000000,
        "input": ["text", "image"],
        "reasoningEfforts": {"low": "low", "high": "high", "max": "max"},
    },
    {
        "id": "qwen3.8-27b",
        "name": "qwen3.8-27b (THU)",
        "contextWindow": 1000000,
        "input": ["text", "image"],
    },
]


class PreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sandbox = Sandbox.create(self.root)
        self.settings = AppSettings(
            dsh_dir=str(self.sandbox.dsh_dir),
            ccswitch_dir=str(self.sandbox.ccswitch_dir),
            unify_all_profiles=True,
        )
        self.backup = BackupManager(self.sandbox.backups_dir, keep=5)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _snapshot(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for path in self.root.rglob("*"):
            if path.is_file():
                files[str(path)] = path.read_text(encoding="utf-8", errors="replace")
        return files

    def test_preflight_passes_and_changes_nothing(self) -> None:
        before = self._snapshot()
        report = run_preflight(self.settings, FAKE_NEW_TOKEN, MODELS)
        self.assertTrue(report.ok, report.text())
        self.assertEqual(report.blocked_targets(), set())
        self.assertIn("预检通过", report.text())
        self.assertEqual(self._snapshot(), before, "预检不能改动任何文件")

        names = [item.name for item in report.items]
        self.assertTrue(any("凭据" in name for name in names), names)
        self.assertTrue(any("profile" in name for name in names), names)
        self.assertTrue(any("卡片" in name for name in names), names)

    def test_preflight_blocks_when_target_cannot_be_written(self) -> None:
        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        profile.write_text(
            """# occupied
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      madmodel:
        displayName: 别人的配置
        api: anthropic
        baseURL: https://example.com/v1
""",
            encoding="utf-8",
        )
        report = run_preflight(self.settings, FAKE_NEW_TOKEN, MODELS)
        self.assertFalse(report.ok)
        self.assertIn("dsh", report.blocked_targets())

    def test_preflight_accepts_profile_with_js_expressions(self) -> None:
        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        profile.write_text(PROFILE_WITH_JS, encoding="utf-8")
        report = run_preflight(self.settings, FAKE_NEW_TOKEN, MODELS)
        self.assertTrue(report.ok, report.text())

    def test_rendered_content_matches_what_actually_gets_written(self) -> None:
        """预检渲染出来的文本，必须和真写下去的逐字一致。"""

        report = run_preflight(self.settings, FAKE_NEW_TOKEN, MODELS)
        self.assertTrue(report.ok, report.text())

        rendered: dict[str, str] = {}
        for target in build_targets(self.settings):
            target.dry_run = True
            target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
            for item in target.rendered:
                rendered[str(item.path)] = item.text

        written_nothing = self._snapshot()
        self.assertNotEqual(rendered, {}, "干跑应该渲染出内容")
        for path, text in rendered.items():
            self.assertNotIn(text, written_nothing.values(), "干跑不能真的写盘")

        for target in build_targets(self.settings):
            target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)

        for path, text in rendered.items():
            target_path = Path(path)
            if target_path.suffix == ".db":
                continue  # 数据库卡片是渲染成 JSON 校验的，不比对二进制
            try:
                actual = target_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # 二进制文件跳过
            self.assertEqual(actual, text, f"{path} 的实际写入与预检渲染不一致")


if __name__ == "__main__":
    unittest.main()
