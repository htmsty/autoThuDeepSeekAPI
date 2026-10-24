"""TLS setup regressions for packaged Python runtimes."""

from __future__ import annotations

import ssl
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import http_tls  # noqa: E402
from auto_thu_deepseek_api.token import site_models, verifier  # noqa: E402


class TlsContextTest(unittest.TestCase):
    def setUp(self) -> None:
        http_tls.create_verified_context.cache_clear()
        http_tls._windows_certificate_context.cache_clear()

    def tearDown(self) -> None:
        http_tls._windows_certificate_context.cache_clear()
        http_tls.create_verified_context.cache_clear()

    def test_default_context_is_preferred(self) -> None:
        sentinel = mock.MagicMock(spec=ssl.SSLContext)
        with mock.patch.object(
            http_tls.ssl,
            "create_default_context",
            return_value=sentinel,
        ) as factory:
            self.assertIs(http_tls.create_verified_context(), sentinel)
            self.assertIs(http_tls.create_verified_context(), sentinel)
            factory.assert_called_once_with()

    def test_raw_context_factory_still_returns_full_ssl_context(self) -> None:
        context = http_tls._new_ssl_context()
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertTrue(hasattr(context, "wrap_socket"))

    def test_missing_ca_file_falls_back_to_windows_store(self) -> None:
        class FakeContext:
            def __init__(self, protocol) -> None:  # noqa: ANN001
                self.protocol = protocol
                self.loaded: list[bytes] = []
                self.verify_mode = None
                self.check_hostname = False

            def load_verify_locations(self, *, cadata) -> None:  # noqa: ANN001
                self.loaded.append(cadata)

        with (
            mock.patch.object(
                http_tls.ssl,
                "create_default_context",
                side_effect=FileNotFoundError("codex-rejected-cas.pem"),
            ),
            mock.patch.object(
                http_tls,
                "_new_ssl_context",
                return_value=FakeContext(ssl.PROTOCOL_TLS_CLIENT),
            ),
            mock.patch.object(
                http_tls.ssl,
                "enum_certificates",
                return_value=[
                    (b"trusted-server-auth", "x509_asn", True),
                    (b"not-for-server-auth", "x509_asn", False),
                ],
            ),
        ):
            context = http_tls.create_verified_context()

        self.assertIsInstance(context, FakeContext)
        self.assertEqual(context.protocol, ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.loaded, [b"trusted-server-auth"])

    def test_windows_fallback_never_disables_verification(self) -> None:
        class FakeContext:
            def __init__(self, protocol) -> None:  # noqa: ANN001
                self.verify_mode = None
                self.check_hostname = False

            def load_verify_locations(self, *, cadata) -> None:  # noqa: ANN001
                pass

        with (
            mock.patch.object(
                http_tls.ssl,
                "create_default_context",
                side_effect=FileNotFoundError("missing CA"),
            ),
            mock.patch.object(
                http_tls,
                "_new_ssl_context",
                return_value=FakeContext(ssl.PROTOCOL_TLS_CLIENT),
            ),
            mock.patch.object(http_tls.ssl, "enum_certificates", return_value=[]),
        ):
            with self.assertRaises(ssl.SSLError):
                http_tls.create_verified_context()

    def test_model_download_turns_tls_setup_failure_into_fallback(self) -> None:
        with mock.patch.object(
            site_models,
            "create_verified_context",
            side_effect=FileNotFoundError("missing CA"),
        ):
            self.assertIsNone(
                site_models._get_text("https://madmodel.cs.tsinghua.edu.cn/", 4096)
            )

    def test_token_request_turns_tls_setup_failure_into_error_result(self) -> None:
        with mock.patch.object(
            verifier,
            "create_verified_context",
            side_effect=FileNotFoundError("missing CA"),
        ):
            status, body, error = verifier._request(
                "https://madmodel.cs.tsinghua.edu.cn/",
                "token",
            )
        self.assertIsNone(status)
        self.assertEqual(body, b"")
        self.assertIn("FileNotFoundError", error)


if __name__ == "__main__":
    unittest.main()
