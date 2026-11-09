"""自检：用本地网页验证“内置浏览器能否真的抓到 Token”。

打包后跑一次 `autoThuDeepSeekAPI.exe --selftest`，能确认 QtWebEngine 随包可用：
起一个本地小 HTTP 服务，页面把一枚假 JWT 写进 localStorage，看捕获链路能不能取到。
"""

from __future__ import annotations

import http.server
import logging
import sys
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication

from .http_tls import create_verified_context
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


def check_sqlite_backup() -> None:
    """Exercise the packaged SQLite/DPAPI path using only disposable fake data."""

    from . import crypto
    from .backup import BackupManager

    with tempfile.TemporaryDirectory(prefix="atd-sqlite-selftest-") as tmp:
        root = Path(tmp)
        source = root / "source.db"
        connection = sqlite3.connect(source)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE checks(value)")
            connection.execute("INSERT INTO checks VALUES ('selftest')")
            connection.commit()
            saved = BackupManager(root / "backups").backup_sqlite(source, "original")
        finally:
            connection.close()
        if saved is None:
            raise RuntimeError("SQLite 备份未生成")
        restored = root / "restored.db"
        restored.write_bytes(crypto.unprotect_bytes(saved.read_bytes()))
        check = sqlite3.connect(restored)
        try:
            if check.execute("SELECT value FROM checks").fetchone() != ("selftest",):
                raise RuntimeError("SQLite 备份内容不一致")
        finally:
            check.close()


def run(profile_dir: Path, timeout_seconds: int = 30) -> int:
    try:
        check_sqlite_backup()
        backup_ok, backup_detail = True, "SQLite/DPAPI 备份通过"
    except Exception as exc:
        backup_ok, backup_detail = False, f"SQLite/DPAPI 备份失败：{exc}"
    try:
        create_verified_context()
        tls_ok = True
        tls_detail = "HTTPS 证书上下文可用"
    except Exception as exc:  # noqa: BLE001
        tls_ok = False
        tls_detail = f"HTTPS 证书上下文失败：{type(exc).__name__}: {exc}"

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

    # `QApplication.quit()` only leaves the event loop. In one-file builds the Chromium
    # renderer can outlive the temporary `_MEI...` directory briefly unless the page and
    # profile are explicitly destroyed and DeferredDelete events are processed.
    try:
        session.cancel()
        session.deleteLater()
    except RuntimeError:
        # `TokenCaptureSession.finish()` may already have completed its DeferredDelete.
        pass
    profile.deleteLater()
    for _ in range(30):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        application.processEvents()
        time.sleep(0.03)

    server.shutdown()
    server.server_close()

    ok = bool(result["ok"]) and tls_ok and backup_ok
    print(
        ("自检通过：" if ok else "自检失败：")
        + str(result["detail"])
        + "；"
        + tls_detail
        + "；"
        + backup_detail
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    if len(argv) > 1 and not argv[1].startswith("-"):
        return run(Path(argv[1]))
    # 自检不读取 private 指针，也不收紧真实运行目录权限。
    with tempfile.TemporaryDirectory(prefix="atd-browser-selftest-") as tmp:
        return run(Path(tmp) / "profile")

