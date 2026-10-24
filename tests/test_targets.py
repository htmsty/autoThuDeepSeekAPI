"""写入与还原的行为验证（在临时沙盒里跑，不碰真实配置）。"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import crypto  # noqa: E402
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

    def test_dsh_credentials_only_rewrites_our_line(self) -> None:
        """凭据文件里还有别的工具的 key：只改 MADMODEL_API_KEY 那一行，其余原文一字不动。"""

        credentials = self.sandbox.dsh_dir / ".credentials.yaml"
        original = """# 这份文件别的工具也在用：注释、单引号、流式写法都别动
version: 1
records:
  client-connection/browser-session: {kind: grant, secret: "sXi1FWkuO"}   # 别人的记录
refs:
  DEEPSEEK_API_KEY: 'deepseek-old-key'        # 别的工具
  MADMODEL_API_KEY: old-token-value           # 我们的
  TSINGHUA_API_KEY: "tsinghua-old-key"
"""
        credentials.write_text(original, encoding="utf-8")
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=credentials,
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        after = credentials.read_text(encoding="utf-8")
        self.assertIn(FAKE_NEW_TOKEN, after)
        # 别的行（含注释、引号风格、流式写法）必须逐字保持
        for keep in (
            "# 这份文件别的工具也在用：注释、单引号、流式写法都别动",
            'client-connection/browser-session: {kind: grant, secret: "sXi1FWkuO"}   # 别人的记录',
            "DEEPSEEK_API_KEY: 'deepseek-old-key'        # 别的工具",
            'TSINGHUA_API_KEY: "tsinghua-old-key"',
            "# 我们的",
        ):
            self.assertIn(keep, after, f"这一行被改动了：{keep}")

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

    def test_managed_profile_is_updated_without_copying_to_other_profiles(self) -> None:
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            ),
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        web = (self.sandbox.dsh_dir / "profiles" / "web" / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertNotIn("madmodel", web)
        desktop = (
            self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("madmodel", desktop)

    def test_unify_points_default_model_at_our_own_provider(self) -> None:
        """统一模式只把默认模型指向**我们自己的** provider；别人的本地转发一个字节都不动。"""

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
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        from ruamel.yaml import YAML

        data = YAML(typ="safe").load(profile.read_text(encoding="utf-8"))
        entries = {entry["id"]: entry for entry in data}
        # thu-local 只是"键名不是 madmodel、地址也不是清华站"的本地转发 —— 不认识它，
        # 于是按"统一"的语义新建我们自己的 madmodel，并把默认模型指到它
        self.assertEqual(entries["agent-default-model"]["config"]["provider"], "madmodel")
        providers = entries["llm-pi-ai"]["config"]["providers"]
        thu = providers["thu-local"]
        self.assertEqual(thu.get("displayName"), "我的中转", "不该覆盖用户自己的名称")
        self.assertEqual(thu.get("baseURL"), "http://127.0.0.1:8788/v1")
        self.assertNotIn("models", thu, "没被认领的 provider 不该被写进模型列表")
        self.assertIn("madmodel", providers, "统一模式应当新建我们自己的 provider")
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
        self.assertIsNone(
            config.get("reasoningEffort"),
            "用户原本没写默认思考档时，同步不能替他新增",
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
            profile="web",
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

    def test_dsh_creates_next_to_ambiguous_local_forward_without_claiming_it(self) -> None:
        """本地转发不能被认领；没有受管 profile 时只新建一份 madmodel。

        旧版本就是凭这个标记把它误认成自己的（`isearcher` / `ollama` 那种悲剧）；
        现在会新建独立 provider，但 thu-local 的原文一个字节都不改。
        """

        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        original = (
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
"""
        )
        profile.write_text(original, encoding="utf-8")
        target = DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        after = profile.read_text(encoding="utf-8")
        self.assertIn("madmodel:", after)
        self.assertIn(
            """      thu-local:
        displayName: 我的中转
        apiKeyEnv: MADMODEL_API_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:8788/v1
        models:
          - id: old-model
            name: 老模型""",
            after,
            "别人的 provider 原文不能被改",
        )

    def test_dsh_only_warns_about_provider_leftover_from_old_versions(self) -> None:
        """被旧版本误写过标记的别人的 provider（isearcher 那种）：只提醒，绝不改它。"""

        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        original = """# 被旧版本误改过的样子：isearcher 身上留了我们的 apiKeyEnv
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      isearcher:
        displayName: Isearcher
        apiKeyEnv: MADMODEL_API_KEY
        api: openai-completions
        reasoning: low
        baseURL: http://127.0.0.1:9527/v1
        models:
          - id: infini-ai
            name: infini-ai
      madmodel:
        displayName: DeepSeek (THU)
        apiKeyEnv: MADMODEL_API_KEY
        api: openai-completions
        baseURL: https://madmodel.cs.tsinghua.edu.cn/v1
        models:
          - id: DeepSeek-V4.1-Flash
            name: DeepSeek-V4.1-Flash (THU)
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: isearcher
    model: infini-ai
"""
        profile.write_text(original, encoding="utf-8")
        before = self._parse_entries(profile)["llm-pi-ai"]["config"]["providers"]["isearcher"]

        # 统一模式也会遍历到这个 profile：isearcher 依然一个字节都不许动
        target = self._dsh_target(unify=True)
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        after = self._parse_entries(profile)["llm-pi-ai"]["config"]["providers"]
        self.assertEqual(after["isearcher"], before, "带残留标记的别人的 provider 被改动了")
        self.assertTrue(
            any("有没有被旧版本改过" in note for note in result.messages),
            f"应当提醒用户这个 provider 可能被旧版本误改过：{result.messages}",
        )
        # 我们自己的 madmodel 仍然照常维护
        self.assertIn("qwen3.8-27b", [model["id"] for model in after["madmodel"]["models"]])

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
            ),
            refresh_sibling_cards=True,  # 这个开关默认是关的；这里专门测"打开后会刷新旧卡片"
            activate=True,
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

    def test_ccswitch_json_keeps_user_style_and_other_keys(self) -> None:
        """改 settings.json / live-state.json 时沿用用户自己的缩进风格，其它键原样保留。"""

        settings_path = self.sandbox.ccswitch_dir / "settings.json"
        settings_path.write_text(
            "{\n"
            '    "showInTray": true,\n'
            '    "currentProviderCodex": "codex-official",\n'
            '    "enableLocalProxy": true,\n'
            '    "用户自己的键": {"嵌套": [1, 2, 3]}\n'
            "}\n",
            encoding="utf-8",
        )
        original = settings_path.read_text(encoding="utf-8")

        target = CcSwitchTarget(
            TargetPaths(
                base_dir=self.sandbox.ccswitch_dir,
                db_file=self.sandbox.ccswitch_dir / "cc-switch.db",
                settings_file=settings_path,
                live_state_file=self.sandbox.ccswitch_dir / "live-state.json",
            ),
            activate=True,
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        after = settings_path.read_text(encoding="utf-8")
        self.assertIn('    "currentProviderCodex": "deepseek-thu"', after, "值该改还是要改")
        self.assertIn('    "用户自己的键": {"嵌套": [1, 2, 3]}', after, "别的键（含紧凑写法）被改动了")
        self.assertIn('    "showInTray": true', after, "缩进风格被换掉了（原来是 4 空格）")
        # 只有那一行的值变了，其它行逐字保持
        self.assertEqual(
            [line for line in after.splitlines() if "currentProvider" not in line],
            [line for line in original.splitlines() if "currentProvider" not in line],
            "除了当前供应商那一行，别的行不该被改写",
        )

    def test_ccswitch_does_not_touch_sibling_cards_by_default(self) -> None:
        """默认不去动「同站点旧卡片」——那属于别人的配置；只有显式打开开关才刷新。"""

        db = self.sandbox.ccswitch_dir / "cc-switch.db"
        target = CcSwitchTarget(
            TargetPaths(
                base_dir=self.sandbox.ccswitch_dir,
                db_file=db,
                settings_file=self.sandbox.ccswitch_dir / "settings.json",
                live_state_file=self.sandbox.ccswitch_dir / "live-state.json",
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        connection = sqlite3.connect(db)
        row = connection.execute(
            "SELECT settings_config FROM providers WHERE id='madmodel-legacy'"
        ).fetchone()
        connection.close()
        self.assertEqual(
            json.loads(row[0])["auth"]["OPENAI_API_KEY"],
            FAKE_OLD_TOKEN,
            "默认不该动同站点旧卡片的令牌（那是别人的卡片）",
        )
        connection = sqlite3.connect(db)
        mine = connection.execute(
            "SELECT settings_config FROM providers WHERE id='deepseek-thu' AND app_type='codex'"
        ).fetchone()
        connection.close()
        self.assertEqual(json.loads(mine[0])["auth"]["OPENAI_API_KEY"], FAKE_NEW_TOKEN)

    def test_ccswitch_card_update_does_not_activate_or_rewrite_routes(self) -> None:
        settings_path = self.sandbox.ccswitch_dir / "settings.json"
        live_path = self.sandbox.ccswitch_dir / "live-state.json"
        before_settings = settings_path.read_bytes()
        before_live = live_path.read_bytes()
        db_path = self.sandbox.ccswitch_dir / "cc-switch.db"
        connection = sqlite3.connect(db_path)
        current_before = connection.execute(
            "SELECT id, is_current FROM providers WHERE app_type='codex' AND is_current=1"
        ).fetchall()
        connection.close()
        target = CcSwitchTarget(
            TargetPaths(
                base_dir=self.sandbox.ccswitch_dir,
                db_file=db_path,
                settings_file=settings_path,
                live_state_file=live_path,
            )
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(settings_path.read_bytes(), before_settings)
        self.assertEqual(live_path.read_bytes(), before_live)

        connection = sqlite3.connect(db_path)
        current = connection.execute(
            "SELECT id, is_current FROM providers WHERE app_type='codex' AND is_current=1"
        ).fetchall()
        connection.close()
        self.assertEqual(current, current_before)

    def test_ccswitch_route_failure_rolls_back_database_and_settings(self) -> None:
        db = self.sandbox.ccswitch_dir / "cc-switch.db"
        settings_path = self.sandbox.ccswitch_dir / "settings.json"
        live_path = self.sandbox.ccswitch_dir / "live-state.json"
        before_settings = settings_path.read_bytes()
        connection = sqlite3.connect(db)
        before_token = json.loads(
            connection.execute(
                "SELECT settings_config FROM providers WHERE id='deepseek-thu'"
            ).fetchone()[0]
        )["auth"]["OPENAI_API_KEY"]
        before_current = connection.execute(
            "SELECT id FROM providers WHERE app_type='codex' AND is_current=1"
        ).fetchall()
        connection.close()
        target = CcSwitchTarget(
            TargetPaths(
                base_dir=self.sandbox.ccswitch_dir,
                db_file=db,
                settings_file=settings_path,
                live_state_file=live_path,
            ),
            activate=True,
        )
        original_write_json = target._write_json

        def fail_live(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            if path == live_path:
                raise OSError("模拟 live-state 写入失败")
            return original_write_json(path, *args, **kwargs)

        target._write_json = fail_live  # type: ignore[assignment]
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertFalse(result.ok)
        self.assertEqual(settings_path.read_bytes(), before_settings)

        connection = sqlite3.connect(db)
        token = connection.execute(
            "SELECT settings_config FROM providers WHERE id='deepseek-thu'"
        ).fetchone()[0]
        current = connection.execute(
            "SELECT id FROM providers WHERE app_type='codex' AND is_current=1"
        ).fetchall()
        connection.close()
        self.assertEqual(json.loads(token)["auth"]["OPENAI_API_KEY"], before_token)
        self.assertEqual(current, before_current)

    def test_ccswitch_only_refreshes_token_fields_in_old_cards(self) -> None:
        """刷新同站点旧卡片时只换 Token 字段，卡片里别的模型的 key 一个字都不动。"""

        db = self.sandbox.ccswitch_dir / "cc-switch.db"
        payload = {
            "auth": {
                "OPENAI_API_KEY": FAKE_OLD_TOKEN,
                "OTHER_MODEL_KEY": "sk-abcdefghijklmnopqrstuvwxyz012345",
            },
            "config": (
                'model_provider = "custom"\n'
                "[model_providers.custom]\n"
                'base_url = "https://madmodel.cs.tsinghua.edu.cn/v1"\n'
            ),
            "meta": {"someToken": "not-a-jwt-but-long-enough-0123456789"},
        }
        connection = sqlite3.connect(db)
        connection.execute(
            "UPDATE providers SET settings_config=? WHERE id='madmodel-legacy'",
            (json.dumps(payload, ensure_ascii=False),),
        )
        connection.commit()
        connection.close()

        target = CcSwitchTarget(
            TargetPaths(
                base_dir=self.sandbox.ccswitch_dir,
                db_file=db,
                settings_file=self.sandbox.ccswitch_dir / "settings.json",
                live_state_file=self.sandbox.ccswitch_dir / "live-state.json",
            ),
            refresh_sibling_cards=True,
        )
        result = target.apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        connection = sqlite3.connect(db)
        row = connection.execute(
            "SELECT settings_config FROM providers WHERE id='madmodel-legacy'"
        ).fetchone()
        connection.close()
        data = json.loads(row[0])
        self.assertEqual(data["auth"]["OPENAI_API_KEY"], FAKE_NEW_TOKEN, "本卡片的 Token 应当刷新")
        self.assertEqual(
            data["auth"]["OTHER_MODEL_KEY"],
            "sk-abcdefghijklmnopqrstuvwxyz012345",
            "别的模型的 key 被改掉了",
        )
        self.assertEqual(
            data["meta"]["someToken"],
            "not-a-jwt-but-long-enough-0123456789",
            "不是 Token 的字段被改掉了",
        )

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

    def test_ccswitch_json_insert_keeps_other_apps(self) -> None:
        """live-state.json 还没有 apps.codex 时：只补出 codex 那一段，
        claude 的排版（紧凑单行）一个字节都不动。"""

        target = CcSwitchTarget(TargetPaths(base_dir=self.sandbox.ccswitch_dir))
        live = self.sandbox.ccswitch_dir / "live-state.json"
        original = (
            "{\n"
            '  "version": 1,\n'
            '  "apps": {\n'
            '    "claude": { "mode": "direct", "note": "紧凑一行" }\n'
            "  },\n"
            '  "updatedAt": 1759999999123\n'
            "}\n"
        )
        live.write_text(original, encoding="utf-8")
        data = json.loads(original)
        data["apps"]["codex"] = {"proxy_route": "deepseek-thu"}

        changed = target._write_json(
            live,
            data,
            self.backup,
            "ccswitch-live-state",
            patch=("proxy_route", None),
            insert=(("apps", "codex"), "proxy_route", "deepseek-thu"),
        )
        self.assertTrue(changed)
        after = live.read_text(encoding="utf-8")
        self.assertIn(
            '"claude": { "mode": "direct", "note": "紧凑一行" }',
            after,
            "别的应用的排版被改写了",
        )
        self.assertEqual(json.loads(after), data)

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

    # —— 只动自己的 provider（别碰 ollama / GLM 这些别人的模型） ——

    OTHER_PROVIDERS_PROFILE = """# 用户自己的多 provider 配置（复刻真机）
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      openaifse:
        displayName: 大模型
        apiKeyEnv: OPENAIFSE_API_KEY
        api: openai-completions
        baseURL: https://llmapi.paratera.com/v1
        models:
          - id: DeepSeek-V4-Pro
            name: DeepSeek-V4-Pro
      glm:
        displayName: GLM
        apiKeyEnv: GLM_API_KEY
        api: openai-completions
        baseURL: https://open.bigmodel.cn/api/coding/paas/v4/
        models:
          - id: glm-5.3-flash
            name: glm-5.3-flash
      ollama:
        displayName: Ollama
        apiKeyEnv: OLLAMA_API_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:11434/v1
        models:
          - id: deepseek-r1:1.5b
            name: deepseek-r1:1.5b
      madmodel:
        displayName: DeepSeek (THU)
        apiKeyEnv: MADMODEL_API_KEY
        api: openai-completions
        baseURL: https://madmodel.cs.tsinghua.edu.cn/v1
        models:
          - id: DeepSeek-V4.1-Flash
            name: DeepSeek-V4.1-Flash (THU)
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: glm
    model: glm-5.3-flash
- id: llm-deepseek
  name: "@deepseek-ai/dsh-llm-deepseek"
  config:
    baseURL: https://ai.paratera.com/v1
"""

    def _write_profile(self, text: str) -> Path:
        profile = self.sandbox.dsh_dir / "profiles" / "desktop" / "cordis.patch.yml"
        profile.write_text(text, encoding="utf-8")
        return profile

    def _dsh_target(self, *, unify: bool = False) -> DshTarget:
        _ = unify
        return DshTarget(
            TargetPaths(
                base_dir=self.sandbox.dsh_dir,
                credentials_file=self.sandbox.dsh_dir / ".credentials.yaml",
                profiles_dir=self.sandbox.dsh_dir / "profiles",
            ),
        )

    @staticmethod
    def _parse_entries(path: Path) -> dict:
        from ruamel.yaml import YAML

        data = YAML(typ="rt").load(path.read_text(encoding="utf-8"))
        return {entry["id"]: entry for entry in data if isinstance(entry, dict)}

    def test_dsh_does_not_touch_other_providers(self) -> None:
        """同步只能写 madmodel，绝不能动 openaifse / glm / ollama 这些别人的模型。"""

        profile = self._write_profile(self.OTHER_PROVIDERS_PROFILE)
        before = self._parse_entries(profile)
        before_providers = before["llm-pi-ai"]["config"]["providers"]

        result = self._dsh_target().apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        after = self._parse_entries(profile)
        after_providers = after["llm-pi-ai"]["config"]["providers"]

        for key in ("openaifse", "glm", "ollama"):
            self.assertEqual(
                after_providers[key],
                before_providers[key],
                f"{key} 是别人的 provider，同步不该动它一个字",
            )
        # 别的条目（默认模型是 GLM、llm-deepseek）同样不该被碰
        self.assertEqual(after["agent-default-model"], before["agent-default-model"], "默认模型被改了")
        self.assertEqual(after["llm-deepseek"], before["llm-deepseek"], "llm-deepseek 条目被改了")

        # 我们自己的 provider 该更新的还是更新了
        ours = after_providers["madmodel"]
        self.assertEqual(ours["apiKeyEnv"], "MADMODEL_API_KEY")
        self.assertIn("qwen3.8-27b", [model["id"] for model in ours["models"]])

    def test_foreign_provider_raw_text_is_untouched(self) -> None:
        """别人的 provider 不只是字段值不变：注释、单引号、流式列表这些**原文**也要一字不动。"""

        profile = self._write_profile(
            """# 顶部注释
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      glm:                      # 别人的：混着注释与单引号
        displayName: 'GLM'      # 单引号风格
        apiKeyEnv: GLM_API_KEY
        api: openai-completions
        baseURL: "https://open.bigmodel.cn/api/coding/paas/v4/"
        models: [{ id: glm-5.3-flash, name: glm-5.3-flash }]   # 流式列表
      madmodel:
        displayName: DeepSeek (THU)
        apiKeyEnv: MADMODEL_API_KEY
        api: openai-completions
        baseURL: https://madmodel.cs.tsinghua.edu.cn/v1
        models:
          - id: DeepSeek-V4.1-Flash
            name: DeepSeek-V4.1-Flash (THU)
"""
        )
        before = profile.read_text(encoding="utf-8")
        result = self._dsh_target().apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        after = profile.read_text(encoding="utf-8")

        def foreign_block(text: str) -> str:
            return text[text.index("      glm:") : text.index("      madmodel:")]

        self.assertEqual(
            foreign_block(after),
            foreign_block(before),
            "别人的 provider 原文（注释/引号/流式写法）被改写了",
        )
        # 我们自己的那段该更新就更新
        self.assertIn("qwen3.8-27b", after)

        # 再跑一次：已存在的 provider 只更新自己的字段，不额外替他创建默认模型条目
        result = self._dsh_target().apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        after_unify = profile.read_text(encoding="utf-8")
        self.assertEqual(
            foreign_block(after_unify),
            foreign_block(before),
            "统一模式下别人的 provider 原文被改写了",
        )
        self.assertNotIn("agent-default-model", after_unify)

    def test_dsh_creates_own_provider_without_claiming_ollama(self) -> None:
        """只有 ollama 时新建独立 madmodel，但绝不认领或改写 ollama。"""

        profile = self._write_profile(
            """- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      ollama:
        displayName: Ollama
        apiKeyEnv: OLLAMA_API_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:11434/v1
        models:
          - id: deepseek-r1:1.5b
            name: deepseek-r1:1.5b
"""
        )
        before = self._parse_entries(profile)
        before_ollama = before["llm-pi-ai"]["config"]["providers"]["ollama"]

        result = self._dsh_target().apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        after = self._parse_entries(profile)
        providers = after["llm-pi-ai"]["config"]["providers"]
        self.assertEqual(providers["ollama"], before_ollama, "别人的本地 provider 被改动了")
        self.assertIn("madmodel", providers, "没有受管 provider 时应自动新建一份")
        self.assertEqual(providers["madmodel"]["apiKeyEnv"], "MADMODEL_API_KEY")
        self.assertEqual(providers["madmodel"]["baseURL"], "https://madmodel.cs.tsinghua.edu.cn/v1")

    def test_dsh_refuses_local_forward_with_foreign_env(self) -> None:
        """madmodel 这个键被别人的本地转发占着时：报错、不写，原文件保持不变。"""

        text = """- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      madmodel:
        displayName: Ollama
        apiKeyEnv: OLLAMA_API_KEY
        api: openai-completions
        baseURL: http://127.0.0.1:11434/v1
        models:
          - id: deepseek-r1:1.5b
            name: deepseek-r1:1.5b
"""
        profile = self._write_profile(text)
        result = self._dsh_target().apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertFalse(result.ok, "占了 madmodel 键的别人配置不该被覆盖")
        self.assertTrue(any("被其它配置占用" in error for error in result.errors), result.errors)
        self.assertEqual(profile.read_text(encoding="utf-8"), text, "文件应当原样不动")

    # —— 备份与还原 ——

    def test_non_madmodel_provider_with_wrong_url_is_not_claimed(self) -> None:
        """非 madmodel 键只提醒、绝不动；需要时另建一份 madmodel。"""

        text = """- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      tsinghua:
        displayName: tsinghua
        api: openai-completions
        baseURL: https://madmodel.cs.tsinghua.edu.cn/v1/chat/completions
        models:
          - id: "1"
            name: "1"
"""
        profile = self._write_profile(text)
        result = self._dsh_target().apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        providers = self._parse_entries(profile)["llm-pi-ai"]["config"]["providers"]
        self.assertIn("tsinghua", providers)
        self.assertEqual(
            providers["tsinghua"]["baseURL"],
            "https://madmodel.cs.tsinghua.edu.cn/v1/chat/completions",
            "不是 madmodel 的 provider 必须保持原样",
        )
        self.assertNotIn("apiKeyEnv", providers["tsinghua"])
        self.assertIn("madmodel", providers)
        self.assertEqual(providers["madmodel"]["baseURL"], "https://madmodel.cs.tsinghua.edu.cn/v1")

    def test_foreign_provider_with_wrong_base_url_is_warned_not_touched(self) -> None:
        """madmodel 已经在场时，旁边那条地址写歪的 tsinghua 一个字节都不许动，
        但必须在消息里提醒用户（提醒不是改动）。"""

        text = """- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      tsinghua:
        displayName: tsinghua
        api: openai-completions
        baseURL: https://madmodel.cs.tsinghua.edu.cn/v1/chat/completions
        models:
          - id: "1"
            name: "1"
      madmodel:
        displayName: DeepSeek (THU)
        apiKeyEnv: MADMODEL_API_KEY
        api: openai-completions
        baseURL: https://madmodel.cs.tsinghua.edu.cn/v1
        models:
          - id: DeepSeek-V4.1-Flash
            name: DeepSeek-V4.1-Flash (THU)
"""
        profile = self._write_profile(text)
        before = profile.read_text(encoding="utf-8")
        result = self._dsh_target().apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)
        after = profile.read_text(encoding="utf-8")

        def tsinghua_block(value: str) -> str:
            return value[value.index("      tsinghua:") : value.index("      madmodel:")]

        self.assertEqual(tsinghua_block(after), tsinghua_block(before), "别人的 provider 原文被改了")
        self.assertTrue(
            any("tsinghua" in note and "路径多了一截" in note for note in result.messages),
            f"应当提醒这条地址写歪了：{result.messages}",
        )

    def test_new_provider_insert_keeps_foreign_raw_text(self) -> None:
        """原先没有 madmodel 的 profile：新 provider 只能插到 providers 块尾部，
        同一文件里别人 provider 的原文（注释/单引号/流式列表）一字都不许动。"""

        text = """- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      glm:                      # 别人的：混着注释与单引号
        displayName: 'GLM'      # 单引号风格
        apiKeyEnv: GLM_API_KEY
        api: openai-completions
        baseURL: "https://open.bigmodel.cn/api/coding/paas/v4/"
        models: [{ id: glm-5.3-flash, name: glm-5.3-flash }]   # 流式列表
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: glm
    model: glm-5.3-flash
"""
        marker = "\n- id: agent-default-model"
        profile = self._write_profile(text)
        result = self._dsh_target(unify=True).apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        after = profile.read_text(encoding="utf-8")
        foreign_before = text[text.index("      glm:") : text.index(marker)]
        foreign_after = after[after.index("      glm:") : after.index("      madmodel:")]
        self.assertEqual(
            foreign_after.rstrip("\n"), foreign_before.rstrip("\n"), "别人的 provider 原文被改写了"
        )

        providers = self._parse_entries(profile)["llm-pi-ai"]["config"]["providers"]
        self.assertEqual(providers["glm"]["apiKeyEnv"], "GLM_API_KEY")
        self.assertIn("madmodel", providers)
        self.assertEqual(
            providers["madmodel"]["baseURL"], "https://madmodel.cs.tsinghua.edu.cn/v1"
        )

    def test_insert_goes_into_our_entry_not_someone_elses_providers(self) -> None:
        """同一文件里别的条目也有 providers 时，新建的 madmodel 必须插进 llm-pi-ai
        自己的 providers 块，绝不能插到别人条目的 providers 里。"""

        text = """- id: llm-deepseek
  name: "@deepseek-ai/dsh-llm-deepseek"
  config:
    providers:
      legacy:                 # 别人条目里的 providers
        apiKeyEnv: LEGACY_KEY
        baseURL: https://ai.paratera.com/v1
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      glm:                    # 我们这个条目里的别人
        apiKeyEnv: GLM_API_KEY
        baseURL: https://open.bigmodel.cn/api/coding/paas/v4/
"""
        profile = self._write_profile(text)
        result = self._dsh_target(unify=True).apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertTrue(result.ok, result.errors)

        entries = self._parse_entries(profile)
        self.assertIn("madmodel", entries["llm-pi-ai"]["config"]["providers"])
        self.assertNotIn(
            "madmodel",
            entries["llm-deepseek"]["config"]["providers"],
            "不能把 provider 插到别人条目的 providers 里",
        )
        after = profile.read_text(encoding="utf-8")
        self.assertIn("      legacy:                 # 别人条目里的 providers", after)

    def test_inline_madmodel_is_not_rewritten_wholesale(self) -> None:
        """madmodel 写成内联 map、旁边还有别人的 provider 时：宁可报错不写，
        也不能靠"整份重写"完成任务（那会把别人的排版一起重排）。"""

        text = """- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      glm:
        displayName: GLM
        apiKeyEnv: GLM_API_KEY
        api: openai-completions
        baseURL: https://open.bigmodel.cn/api/coding/paas/v4/
        models:
          - id: glm-5.3-flash
            name: glm-5.3-flash
      madmodel: {displayName: DeepSeek (THU), apiKeyEnv: MADMODEL_API_KEY, api: openai-completions, baseURL: https://madmodel.cs.tsinghua.edu.cn/v1, models: [{id: DeepSeek-V4.1-Flash, name: DeepSeek-V4.1-Flash}]}
"""
        profile = self._write_profile(text)
        result = self._dsh_target().apply(FAKE_NEW_TOKEN, MODELS, self.backup)
        self.assertFalse(result.ok, "定位不到我们的块时不该硬写")
        self.assertEqual(profile.read_text(encoding="utf-8"), text, "文件应当原样不动")

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
            crypto.unprotect_bytes(Path(oldest.backup).read_bytes()).decode("utf-8"),
            credentials.read_bytes().decode("utf-8"),
            "最早那份必须始终保留",
        )

    def test_original_backup_is_never_pruned(self) -> None:
        """首份（original）备份是"第一次修改前"的状态，备份再多也不能被清掉。"""

        credentials = self.sandbox.dsh_dir / ".credentials.yaml"
        original_text = credentials.read_bytes().decode("utf-8")
        first = self.backup.backup_file(credentials, "original-first-run")
        self.assertIsNotNone(first)
        for _ in range(10):
            credentials.write_text(credentials.read_text(encoding="utf-8") + "# 改动\n", encoding="utf-8")
            self.backup.backup_file(credentials, "auto-sync")

        entries = self.backup.entries()
        originals = [entry for entry in entries if entry.is_original]
        self.assertEqual(len(originals), 1, entries)
        self.assertEqual(
            crypto.unprotect_bytes(Path(originals[0].backup).read_bytes()).decode("utf-8"),
            original_text,
        )

        # 一键还原必须回到最初那份，而不是最近一份
        messages = BackupManager(self.sandbox.backups_dir, keep=5).restore()
        self.assertTrue(any("credentials" in message for message in messages), messages)
        self.assertEqual(credentials.read_bytes().decode("utf-8"), original_text)


if __name__ == "__main__":
    unittest.main()

