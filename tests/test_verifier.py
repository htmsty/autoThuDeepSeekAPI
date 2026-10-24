"""Token 校验的分支：先走会话接口，给不出结论才打模型请求。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api.token import verifier  # noqa: E402


class VerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self._original = verifier._request

    def tearDown(self) -> None:
        verifier._request = self._original

    def _stub(self, responses: list[tuple[int | None, bytes, str]]) -> None:
        def fake_request(url, token, payload=None, timeout=30):  # noqa: ANN001
            self.calls.append((url, payload))
            return responses[min(len(self.calls) - 1, len(responses) - 1)]

        verifier._request = fake_request

    def test_success_from_session_endpoint(self) -> None:
        self._stub([(200, json.dumps({"success": True, "data": "x"}).encode(), "")])
        result = verifier.verify_token("tok", allow_chat_fallback=True)
        self.assertTrue(result.ok)
        self.assertEqual(len(self.calls), 1, "会话接口给了结论就不该再打模型请求")
        self.assertIn("auth-login", self.calls[0][0])

    def test_site_reachable_true_when_site_answers(self) -> None:
        """站点能回话（哪怕 4xx）就算可达：开机时用它判断网络是否就绪。"""

        from unittest import mock

        class FakeResponse:
            status = 200

            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, *args) -> bool:  # noqa: ANN002
                return False

        with mock.patch.object(
            verifier.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            self.assertTrue(verifier.site_reachable(timeout=1))

    def test_site_reachable_false_when_network_is_down(self) -> None:
        from unittest import mock

        with mock.patch.object(
            verifier.urllib.request, "urlopen", side_effect=OSError("网络还没就绪")
        ):
            self.assertFalse(verifier.site_reachable(timeout=1))

    def test_refused_token(self) -> None:
        self._stub([(401, b"", "")])
        result = verifier.verify_token("tok")
        self.assertFalse(result.ok)
        self.assertEqual(len(self.calls), 1)

    def test_success_false_is_refused(self) -> None:
        self._stub([(200, json.dumps({"success": False}).encode(), "")])
        result = verifier.verify_token("tok")
        self.assertFalse(result.ok)

    def test_network_error_stays_unknown_without_model_call(self) -> None:
        self._stub([(None, b"", "TimeoutError: timed out")])
        result = verifier.verify_token("tok")
        self.assertIsNone(result.ok)
        self.assertEqual(len(self.calls), 1, "连不上站点时不该再等一次模型请求")
        self.assertIn("未能连接站点校验", result.detail)

    def test_falls_back_to_chat_when_endpoint_shape_is_unknown(self) -> None:
        self._stub(
            [
                (200, b"not json at all", ""),  # 不是 JSON，但也不是网页 → 值得再核对一次
                (200, b'{"choices": []}', ""),
            ]
        )
        result = verifier.verify_token("tok", allow_chat_fallback=True)
        self.assertTrue(result.ok)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("/v1/chat/completions", self.calls[1][0])

    def test_unknown_endpoint_does_not_spend_quota_by_default(self) -> None:
        self._stub([(200, b"not json at all", "")])
        result = verifier.verify_token("tok")
        self.assertIsNone(result.ok)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("没有发送最小对话校验", result.detail)

    def test_html_reply_skips_the_slow_chat_probe(self) -> None:
        """站点回网页（没登录 / 接口变了）时别再打一次模型请求白等 30 秒。"""

        self._stub([(200, b"<!doctype html><html><body>login</body></html>", "")])
        result = verifier.verify_token("tok")
        self.assertIsNone(result.ok)
        self.assertTrue(result.looks_like_html)
        self.assertEqual(len(self.calls), 1, "回网页时不该再打模型请求")


if __name__ == "__main__":
    unittest.main()

