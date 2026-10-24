"""站点前端包解析：模型列表、排除名单、思考档与图片输入。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api.token import site_models  # noqa: E402

BUNDLE = """
var x={modelList:[
  {label:"DeepSeek-V4.1-Flash",value:"DeepSeek-V4.1-Flash",supportImage:!0,thinkingParam:"thinking",effortOptions:["low","high","max","off"]},
  {label:"qwen3.8-27b",value:"qwen3.8-27b",supportImage:!0,effortOptions:["low","medium","xhigh"]},
  {label:"DeepSeek-V4-Flash-Vision-Exp",value:"DeepSeek-V4-Flash-Vision-Exp",supportImage:!0,effortOptions:["off"]},
  {label:"DeepSeek-R1-W8A8",value:"DeepSeek-R1-W8A8",supportImage:false,effortOptions:[]},
  {label:"纯文本模型",value:"text-only-model",supportImage:false,effortOptions:[]}
]},y=1;
"""


class SiteModelsTest(unittest.TestCase):
    def test_extract_model_list(self) -> None:
        models = site_models.extract_model_list(BUNDLE)
        self.assertIsNotNone(models)
        assert models is not None
        self.assertEqual([m["id"] for m in models][:2], ["DeepSeek-V4.1-Flash", "qwen3.8-27b"])
        self.assertTrue(models[0]["supportImage"])
        self.assertIn("max", models[0]["efforts"])
        self.assertIn("off", models[0]["efforts"])

    def test_missing_block_returns_none(self) -> None:
        self.assertIsNone(site_models.extract_model_list("var a=1;"))
        self.assertIsNone(site_models.extract_model_list(""))

    def test_quoted_model_list_with_whitespace_is_supported(self) -> None:
        bundle = """
        window.x = { "modelList" : [
          { 'label': 'Model A', 'value': 'model-a', effortOptions: ['low'] }
        ] };
        """
        models = site_models.extract_model_list(bundle)
        self.assertIsNotNone(models)
        assert models is not None
        self.assertEqual(models[0]["id"], "model-a")

    def test_fetch_tries_multiple_asset_bundles(self) -> None:
        page = """
        <script src="/assets/js/vendor.js"></script>
        <script src="/assets/js/app.js"></script>
        """

        def fake_get(url: str, _max_bytes: int, timeout: int = 25) -> str | None:
            _ = timeout
            if url.endswith("app.js"):
                return BUNDLE
            if url.endswith("vendor.js"):
                return "var vendor=1;"
            return page

        original = site_models._get_text
        site_models._get_text = fake_get  # type: ignore[assignment]
        try:
            models = site_models.fetch_site_models()
        finally:
            site_models._get_text = original  # type: ignore[assignment]
        self.assertIsNotNone(models)
        assert models is not None
        self.assertIn("DeepSeek-V4.1-Flash", [item["id"] for item in models])

    def test_to_provider_models_filters_and_shapes(self) -> None:
        provider_models = site_models.to_provider_models(site_models.extract_model_list(BUNDLE))
        self.assertIsNotNone(provider_models)
        assert provider_models is not None
        ids = [m["id"] for m in provider_models]
        self.assertEqual(ids, ["DeepSeek-V4.1-Flash", "qwen3.8-27b", "text-only-model"])

        first = provider_models[0]
        self.assertEqual(first["name"], "DeepSeek-V4.1-Flash (THU)")
        self.assertEqual(first["contextWindow"], 1000000)
        self.assertEqual(first["input"], ["text", "image"])
        self.assertEqual(first["reasoningEfforts"], {"low": "low", "high": "high", "max": "max"})

        plain = provider_models[2]
        self.assertEqual(plain["input"], ["text"])
        self.assertNotIn("reasoningEfforts", plain)

    def test_corrupt_cache_is_ignored_not_fatal(self) -> None:
        """缓存文件被写坏（at 不是数字 / 条目不是字典）时，不能把整次同步搞挂。"""

        import json
        import tempfile

        from auto_thu_deepseek_api import paths

        with tempfile.TemporaryDirectory() as tmp:
            original_root = paths.data_root
            original_fetch = site_models.fetch_site_models
            paths.data_root = lambda: Path(tmp)  # type: ignore[assignment]
            site_models.fetch_site_models = lambda: None  # type: ignore[assignment]
            try:
                cache = Path(tmp) / "model-cache.json"
                for payload in (
                    {"at": "不是数字", "models": [{"id": "m"}]},
                    {"at": None, "models": [{"id": "m"}]},
                    {"at": 1e18, "models": ["脏数据", {"id": "只留字典"}]},
                ):
                    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    models = site_models.resolve_models(None)
                    self.assertTrue(models, "坏缓存应当被忽略并回退到内置名单")
                    self.assertTrue(
                        all(isinstance(item, dict) and item.get("id") for item in models),
                        f"回退结果里不该有脏数据：{models}",
                    )
            finally:
                site_models.fetch_site_models = original_fetch  # type: ignore[assignment]
                paths.data_root = original_root  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()

