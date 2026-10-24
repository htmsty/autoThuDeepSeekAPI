"""Force the packaged-CA fallback and probe the MadModel HTTPS endpoint."""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import constants, http_tls  # noqa: E402


def main() -> int:
    http_tls._windows_certificate_context.cache_clear()
    with mock.patch.object(
        http_tls.ssl,
        "create_default_context",
        side_effect=FileNotFoundError("simulated missing CA file"),
    ):
        context = http_tls.create_verified_context()

    request = urllib.request.Request(f"{constants.MADMODEL_ORIGIN}/", method="HEAD")
    request.add_header("User-Agent", "autoThuDeepSeekAPI TLS fallback probe")
    with urllib.request.urlopen(request, timeout=20, context=context) as response:
        print(f"TLS fallback OK: HTTP {response.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
