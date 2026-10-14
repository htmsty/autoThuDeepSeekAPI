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

from .sandbox import FAKE_NEW_TOKEN, FAKE_OLD_TOKEN, PROFILE_CROSSED, PROFILE_WITH_JS, Sandbox  # noqa: E402

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

    def test_dsh_rebuilds_broken_credentials_file(self) -> None:
        """凭据文件语法坏了也要能救：留底 → 按行捞回大写键 → 重写成合法结构。"""

        credentials = self.sandbox.dsh_dir / ".credentials.yaml"
        credentials.write_text(
            "version: 1\n"
            "records:\n"
            "  client-connection/browser-session:\n"
            "    kind: grant\n"
            "    payload:\n"
            "      version: 1\n"
            "refs:\n"
            "  DEEPSEEK_API_KEY: " + FAKE_OLD_TOKEN + "\n"
            "  MADMODEL_API_KEY: " + FAKE_OLD_TOKEN + "\n"
            "\tbad-indent: oops\n",  # 制表符缩进 + 结构错乱，真解析会失败
            encoding="utf-8",
        )
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=credentials,
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(any("留底并尝试重建" in note for note in result.messages), result.messages)

        from ruamel.yaml import YAML

        parsed = YAML(typ="safe").load(credentials.read_text(encoding="utf-8"))
        self.assertEqual(parsed["refs"]["MADMODEL_API_KEY"], FAKE_NEW_TOKEN)
        self.assertEqual(parsed["refs"]["DEEPSEEK_API_KEY"], FAKE_OLD_TOKEN, "能捞回的键要留着")
        self.assertTrue(
            any("broken" in entry.label for entry in self.backup.entries()),
            "坏文件要先留底",
        )

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

    def test_unify_points_default_model_at_the_provider_we_updated(self) -> None:
        """统一时默认模型要指向真正被更新的那个 provider，而不是死写 madmodel。"""

        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        profile.write_text(
            """# local forward + unify
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      thu-local:
        displayName: 我的中转
        apiKeyEnv: MADMODEL_API_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:8788/v1
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: tsinghua
    model: "1"
""",
            encoding="utf-8",
        )
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

        from ruamel.yaml import YAML

        data = YAML(typ="safe").load(profile.read_text(encoding="utf-8"))
        entries = {entry["id"]: entry for entry in data}
        self.assertEqual(entries["agent-default-model"]["config"]["provider"], "thu-local")
        self.assertEqual(entries["agent-default-model"]["config"]["model"], "DeepSeek-V4.1-Flash")

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
        # provider 级 reasoning：有值就必须所有模型都支持；没设就不动（用户/DSH 自己的选择）
        supported_sets = [
            {str(level) for level in model["reasoningEfforts"].keys()}
            for model in provider["models"]
            if model.get("reasoningEfforts")
        ]
        common = set.intersection(*supported_sets)
        reasoning = provider.get("reasoning")
        self.assertTrue(
            reasoning is None or reasoning in common,
            f"provider reasoning 不合法：{reasoning}（合法集合 {common}）",
        )
        ids = [model["id"] for model in provider["models"]]
        self.assertEqual(ids, ["DeepSeek-V4.1-Flash", "qwen3.8-27b"])
        self.assertEqual(provider["models"][0]["name"], "DeepSeek-V4.1-Flash (THU)")
        self.assertIn("reasoningEfforts", provider["models"][0])
        self.assertEqual(entries["agent-default-model"]["config"]["model"], "DeepSeek-V4.1-Flash")

    def test_dsh_keeps_js_tags_and_original_style(self) -> None:
        """profile 里的 !!js 表达式与注释必须原样保留，顶层缩进风格也别整份重排。"""

        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        profile.write_text(PROFILE_WITH_JS, encoding="utf-8")

        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        text = profile.read_text(encoding="utf-8")
        self.assertIn("!!js", text)
        self.assertIn("process.env.DSH_VIEW", text)
        self.assertIn("# 带 !!js 的补丁", text)
        self.assertTrue(
            any(line.startswith("- id: ui-chat") for line in text.splitlines()),
            "顶层序列应保持顶格写法",
        )

        from ruamel.yaml import YAML

        data = YAML(typ="rt").load(text)  # 往返模式能读懂 !!js
        entries = {entry["id"]: entry for entry in data}
        provider = entries["llm-pi-ai"]["config"]["providers"]["madmodel"]
        self.assertEqual([model["id"] for model in provider["models"]], ["DeepSeek-V4.1-Flash", "qwen3.8-27b"])

    # —— 思考档：用户报的 "does not support reasoning effort medium" ——

    def test_pick_effort_prefers_supported_levels(self) -> None:
        self.assertEqual(DshTarget._pick_effort(["low", "high", "max"]), "high")
        self.assertEqual(DshTarget._pick_effort(["low", "medium", "xhigh"]), "medium")
        self.assertEqual(DshTarget._pick_effort(["minimal"]), "minimal")
        self.assertEqual(DshTarget._pick_effort([]), "")

    def test_safe_provider_reasoning_only_uses_common_levels(self) -> None:
        models = [
            {"id": "A", "reasoningEfforts": {"low": "low", "high": "high", "max": "max"}},
            {"id": "B", "reasoningEfforts": {"low": "low", "medium": "medium", "xhigh": "xhigh"}},
        ]
        self.assertEqual(DshTarget._safe_provider_reasoning(models), "low", "只有 low 是公共档位")
        self.assertEqual(DshTarget._safe_provider_reasoning([{"id": "A", "reasoningEfforts": {"high": "high"}}]), "high")
        self.assertEqual(DshTarget._safe_provider_reasoning([{"id": "A"}, {"id": "B"}]), "", "都没有档位就别写")
        self.assertEqual(
            DshTarget._safe_provider_reasoning(
                [
                    {"id": "A", "reasoningEfforts": {"high": "high"}},
                    {"id": "B", "reasoningEfforts": {"low": "low"}},
                ]
            ),
            "",
            "没有公共档位就不写 provider 级 reasoning",
        )

    def test_dsh_writes_only_supported_reasoning(self) -> None:
        """复现用户第一次运行就报错的场景：provider reasoning=medium + DeepSeek-V4.1-Flash 只支持 low/high/max。"""

        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        profile.write_text(
            """# 第一次运行的样子
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      madmodel:
        displayName: DeepSeek (THU)
        api: openai-completions
        reasoning: medium
        baseURL: https://madmodel.cs.tsinghua.edu.cn/v1
        apiKeyEnv: MADMODEL_API_KEY
        models:
          - id: DeepSeek-V4.1-Flash
            name: DeepSeek-V4.1-Flash (THU)
            contextWindow: 1000000
            input:
              - text
              - image
            reasoningEfforts:
              low: low
              high: high
              max: max
          - id: qwen3.8-27b
            name: qwen3.8-27b (THU)
            contextWindow: 1000000
            input:
              - text
              - image
            reasoningEfforts:
              low: low
              medium: medium
              xhigh: xhigh
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: madmodel
    model: DeepSeek-V4.1-Flash
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

        from ruamel.yaml import YAML

        data = YAML(typ="rt").load(profile.read_text(encoding="utf-8"))
        entries = {entry["id"]: entry for entry in data}
        provider = entries["llm-pi-ai"]["config"]["providers"]["madmodel"]
        supported_sets = [
            {str(level) for level in model["reasoningEfforts"].keys()}
            for model in provider["models"]
            if model.get("reasoningEfforts")
        ]
        common = set.intersection(*supported_sets)
        self.assertIn(provider.get("reasoning"), common, "provider 级 reasoning 必须所有模型都支持")

        config = entries["agent-default-model"]["config"]
        chosen = next(model for model in provider["models"] if model["id"] == config["model"])
        self.assertIn(
            config.get("reasoningEffort"),
            {str(level) for level in chosen["reasoningEfforts"].keys()},
            "默认模型必须显式写一个它支持的思考档",
        )

    def test_dsh_keeps_valid_user_reasoning(self) -> None:
        """用户设的 thinking 档位合法就一个字都不改——**第一次同步之后尤其不能改**。"""

        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        profile.write_text(
            """- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      madmodel:
        displayName: DeepSeek (THU)
        api: openai-completions
        apiKeyEnv: MADMODEL_API_KEY
        baseURL: https://madmodel.cs.tsinghua.edu.cn/v1
        reasoning: low
        models:
          - id: DeepSeek-V4.1-Flash
            name: DeepSeek-V4.1-Flash (THU)
            contextWindow: 1000000
            input: [text, image]
            reasoningEfforts:
              low: low
              high: high
              max: max
          - id: qwen3.8-27b
            name: qwen3.8-27b (THU)
            contextWindow: 1000000
            input: [text, image]
            reasoningEfforts:
              low: low
              high: high
              max: max
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: madmodel
    model: DeepSeek-V4.1-Flash
    reasoningEffort: max
""",
            encoding="utf-8",
        )
        models = [
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
                "reasoningEfforts": {"low": "low", "high": "high", "max": "max"},
            },
        ]
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, models, self.backup)
        self.assertTrue(result.ok, result.errors)

        from ruamel.yaml import YAML

        data = YAML(typ="rt").load(profile.read_text(encoding="utf-8"))
        entries = {entry["id"]: entry for entry in data}
        provider = entries["llm-pi-ai"]["config"]["providers"]["madmodel"]
        # low / high / max 都合法；程序自己"更想"写 high，但不能动用户设的 low
        self.assertEqual(provider.get("reasoning"), "low", "合法的用户档位被改掉了")
        self.assertEqual(
            entries["agent-default-model"]["config"].get("reasoningEffort"),
            "max",
            "默认模型上合法的用户档位也被改掉了",
        )

    def test_dsh_does_not_add_provider_reasoning_when_absent(self) -> None:
        """原本没有 provider reasoning 的，不替用户加（缺省不算「不合法」）。"""

        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        profile.write_text(
            """- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      madmodel:
        displayName: DeepSeek (THU)
        api: openai-completions
        apiKeyEnv: MADMODEL_API_KEY
        baseURL: https://madmodel.cs.tsinghua.edu.cn/v1
        models:
          - id: DeepSeek-V4.1-Flash
            name: DeepSeek-V4.1-Flash (THU)
            contextWindow: 1000000
            input: [text, image]
            reasoningEfforts:
              low: low
              high: high
""",
            encoding="utf-8",
        )
        models = [
            {
                "id": "DeepSeek-V4.1-Flash",
                "name": "DeepSeek-V4.1-Flash (THU)",
                "contextWindow": 1000000,
                "input": ["text", "image"],
                "reasoningEfforts": {"low": "low", "high": "high"},
            }
        ]
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, models, self.backup)
        self.assertTrue(result.ok, result.errors)

        from ruamel.yaml import YAML

        data = YAML(typ="rt").load(profile.read_text(encoding="utf-8"))
        entries = {entry["id"]: entry for entry in data}
        provider = entries["llm-pi-ai"]["config"]["providers"]["madmodel"]
        self.assertNotIn("reasoning", provider, "用户没设 provider 档位，不该替他加")

    def test_new_provider_gets_safe_reasoning(self) -> None:
        """我们自己新建的 provider 才给一个兜底档位，而且必须是所有模型都支持的。"""

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

        from ruamel.yaml import YAML

        web = YAML(typ="rt").load(
            (self.sandbox.dsh_dir / "profiles" / "web" / "cordis.patch.yml").read_text(encoding="utf-8")
        )
        entries = {entry["id"]: entry for entry in web}
        provider = entries["llm-pi-ai"]["config"]["providers"]["madmodel"]
        common = set.intersection(
            *[
                {str(level) for level in model["reasoningEfforts"].keys()}
                for model in provider["models"]
                if model.get("reasoningEfforts")
            ]
        )
        self.assertIn(provider.get("reasoning"), common, "新建 provider 的兜底档位必须公共")

    def test_credentials_note_says_it_is_not_a_validation(self) -> None:
        """日志里"已是该 Token，无需改动"只是文件比对，不是 API 校验——文案要说清楚。"""

        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        first = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(first.ok, first.errors)

        second = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        note = next((message for message in second.messages if "无需改写" in message), "")
        self.assertTrue(note, second.messages)
        self.assertIn("本目标不做网络校验", note)
        self.assertIn("指纹", note)

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
        # 第一条是"第一次修改前"的原始备份，之后按 keep 保留最近的普通备份
        entries = self.backup.entries()
        self.assertEqual(len(entries), 5, "原始备份 1 份 + 普通备份 keep-1 份")
        oldest = min(entries, key=lambda item: Path(item.backup).parent.name)
        self.assertEqual(
            Path(oldest.backup).read_text(encoding="utf-8"),
            credentials.read_text(encoding="utf-8"),
            "最早那份必须始终保留",
        )

    def test_original_backup_is_never_pruned(self) -> None:
        """首份（original）备份是"第一次修改前"的状态，备份再多也不能被清掉。"""

        credentials = self.sandbox.dsh_dir / ".credentials.yaml"
        original_text = credentials.read_text(encoding="utf-8")
        first = self.backup.backup_file(credentials, "original-first-run")
        self.assertIsNotNone(first)
        for _ in range(10):
            credentials.write_text(credentials.read_text(encoding="utf-8") + "# 改动\n", encoding="utf-8")
            self.backup.backup_file(credentials, "auto-sync")

        entries = self.backup.entries()
        originals = [entry for entry in entries if entry.is_original]
        self.assertEqual(len(originals), 1, entries)
        self.assertEqual(Path(originals[0].backup).read_text(encoding="utf-8"), original_text)

        # 一键还原必须回到最初那份，而不是最近一份
        messages = BackupManager(self.sandbox.backups_dir, keep=5).restore()
        self.assertTrue(any("credentials" in message for message in messages), messages)
        self.assertEqual(credentials.read_text(encoding="utf-8"), original_text)


if __name__ == "__main__":
    unittest.main()

