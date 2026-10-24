"""Security-focused tests for local secrets and login automation guards."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import credentials, crypto, paths  # noqa: E402
from auto_thu_deepseek_api.state_store import AppState  # noqa: E402
from auto_thu_deepseek_api.token.capture import AUTO_LOGIN_SCRIPT  # noqa: E402
from auto_thu_deepseek_api.token.jwt_utils import JwtCandidate, choose_jwt  # noqa: E402

from .sandbox import FAKE_OLD_TOKEN  # noqa: E402


class SecretStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old_root = paths.data_root
        paths.data_root = lambda: self.root  # type: ignore[assignment]

    def tearDown(self) -> None:
        paths.data_root = self._old_root  # type: ignore[assignment]
        self._tmp.cleanup()

    def test_state_token_is_dpapi_encrypted(self) -> None:
        state = AppState()
        state.remember_token(FAKE_OLD_TOKEN)
        raw = paths.state_file().read_text(encoding="utf-8")
        self.assertNotIn(FAKE_OLD_TOKEN, raw)
        payload = json.loads(raw)
        self.assertNotIn("last_token", payload)
        self.assertTrue(payload["last_token_encrypted"])
        self.assertEqual(AppState.load().last_token, FAKE_OLD_TOKEN)

    def test_legacy_plaintext_token_is_migrated_atomically(self) -> None:
        paths.state_file().write_text(
            json.dumps({"last_token": FAKE_OLD_TOKEN}),
            encoding="utf-8",
        )
        state = AppState.load()
        self.assertEqual(state.last_token, FAKE_OLD_TOKEN)
        migrated = paths.state_file().read_text(encoding="utf-8")
        self.assertNotIn(FAKE_OLD_TOKEN, migrated)
        self.assertNotIn('"last_token"', migrated)

    def test_dpapi_round_trip(self) -> None:
        payload = b"secret\x00bytes"
        encrypted = crypto.protect_bytes(payload)
        self.assertTrue(crypto.is_encrypted(encrypted))
        self.assertNotIn(b"secret", encrypted)
        self.assertEqual(crypto.unprotect_bytes(encrypted), payload)

    def test_credential_manager_round_trip(self) -> None:
        target = f"autoThuDeepSeekAPI/test-{uuid.uuid4().hex}"
        try:
            credentials.save_login("tester", "not-a-real-password", target=target)
            loaded = credentials.load_login(target=target)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.username, "tester")
            self.assertEqual(loaded.password, "not-a-real-password")
        finally:
            credentials.delete_login(target=target)


class LoginGuardTest(unittest.TestCase):
    def test_auto_login_is_limited_to_tsinghua_and_stops_for_2fa(self) -> None:
        self.assertIn("endsWith('.tsinghua.edu.cn')", AUTO_LOGIN_SCRIPT)
        self.assertIn("二次验证", AUTO_LOGIN_SCRIPT)
        self.assertIn("captcha", AUTO_LOGIN_SCRIPT)
        self.assertIn("action: 'manual'", AUTO_LOGIN_SCRIPT)

    def test_jwt_candidate_tie_is_rejected(self) -> None:
        self.assertIsNone(
            choose_jwt(
                [
                    JwtCandidate(FAKE_OLD_TOKEN, source="localStorage", key="token"),
                    JwtCandidate(FAKE_OLD_TOKEN[:-1] + "x", source="localStorage", key="token"),
                ]
            )
        )

    def test_network_candidate_beats_weak_text(self) -> None:
        weak = FAKE_OLD_TOKEN[:-1] + "a"
        strong = FAKE_OLD_TOKEN
        self.assertEqual(
            choose_jwt(
                [
                    JwtCandidate(weak, source="text", key=""),
                    JwtCandidate(
                        strong,
                        source="network",
                        key="https://madmodel.cs.tsinghua.edu.cn/api",
                        url="https://madmodel.cs.tsinghua.edu.cn/",
                    ),
                ]
            ),
            strong,
        )


if __name__ == "__main__":
    unittest.main()
