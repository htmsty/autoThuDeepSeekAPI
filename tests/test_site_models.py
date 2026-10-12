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

    def test_models_equal_ignores_other_fields(self) -> None:
        a = [{"id": "m", "name": "M (THU)", "contextWindow": 1000000, "input": ["text"], "extra": 1}]
        b = [{"id": "m", "name": "M (THU)", "contextWindow": 1000000, "input": ["text"]}]
        self.assertTrue(site_models.models_equal(a, b))
        c = [{"id": "m", "name": "其他名字", "contextWindow": 1000000, "input": ["text"]}]
        self.assertFalse(site_models.models_equal(a, c))


if __name__ == "__main__":
    unittest.main()

