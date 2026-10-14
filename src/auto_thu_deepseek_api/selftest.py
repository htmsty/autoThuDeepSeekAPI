"""自检：用本地网页验证“内置浏览器能否真的抓到 Token”。

打包后跑一次 `autoThuDeepSeekAPI.exe --selftest`，能确认 QtWebEngine 随包可用：
起一个本地小 HTTP 服务，页面把一枚假 JWT 写进 localStorage，看捕获链路能不能取到。
"""

from __future__ import annotations

import http.server
import logging
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .token.capture import TokenCaptureSession, build_profile

logger = logging.getLogger(__name__)

FAKE_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJjb2RlIjoiMCIsImlhdCI6MTc5MTc5NjU0MSwiZXhwIjoxNzk5MDAwMDAwfQ."
    "T9YmGc_4DL7X_DB7uN8DWY00UuhkLOwlAP9MfkYuXkc"
)
PAGE = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>selftest</title></head>
<body>
<p>autoThuDeepSeekAPI self test</p>
<script>
  localStorage.setItem("madmodel_token", "{FAKE_TOKEN}");
  sessionStorage.setItem("another", "x");
</script>
</body></html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D102
        pass


def _start_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def run(profile_dir: Path, timeout_seconds: int = 30) -> int:
    server, port = _start_server()
    url = f"http://127.0.0.1:{port}/"
    result = {"ok": False, "detail": "超时，没有抓到 Token"}

    application = QApplication.instance() or QApplication(sys.argv[:1])
    profile = build_profile(profile_dir)
    session = TokenCaptureSession(profile, silent=True, timeout_seconds=timeout_seconds, url=url)

    def on_captured(token: str) -> None:
        result["ok"] = token == FAKE_TOKEN
        result["detail"] = "抓到 Token" if result["ok"] else f"抓到的内容不符合预期：{token[:24]}…"
        application.quit()

    def on_failed(message: str) -> None:
        result["detail"] = message
        application.quit()

    session.captured.connect(on_captured)
    session.failed.connect(on_failed)

    QTimer.singleShot(0, session.start)
    QTimer.singleShot((timeout_seconds + 10) * 1000, application.quit)
    application.exec()
    server.shutdown()
    server.server_close()

    print(("自检通过：" if result["ok"] else "自检失败：") + str(result["detail"]))
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    from . import paths

    argv = sys.argv if argv is None else argv
    paths.ensure_runtime_dirs()
    profile_dir = Path(argv[1]) if len(argv) > 1 and not argv[1].startswith("-") else paths.browser_profile_dir() / "selftest"
    return run(profile_dir)

