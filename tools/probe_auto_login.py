"""One-off QtWebEngine probe for the auto-login script."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402

from auto_thu_deepseek_api.token.capture import AUTO_LOGIN_SCRIPT  # noqa: E402


HTML = """
<!doctype html>
<html><body>
<form id="theform">
  <input id="i_user" name="i_user" type="text">
  <input id="i_pass" type="password">
  <div style="display:none"><input id="i_code" name="i_captcha"></div>
  <a id="login" onclick="window.__clicked=true; return false;">登录</a>
</form>
</body></html>
"""


def main() -> int:
    app = QApplication(sys.argv[:1])
    # A default profile is off-the-record, so this probe leaves no browser data behind.
    profile = QWebEngineProfile()
    page = QWebEnginePage(profile)
    state: dict[str, object] = {"ok": False}

    def finish() -> None:
        script = """
        JSON.stringify({
          user: document.getElementById('i_user').value,
          pass: document.getElementById('i_pass').value,
          clicked: Boolean(window.__clicked)
        })
        """
        page.runJavaScript(script, inspect)

    def inspect(value: object) -> None:
        data = json.loads(str(value))
        state["ok"] = (
            data["user"] == "tester"
            and data["pass"] == "secret"
            and data["clicked"] is True
        )
        print(json.dumps(data, ensure_ascii=False))
        app.quit()

    def loaded(ok: bool) -> None:
        if not ok:
            app.quit()
            return
        script = AUTO_LOGIN_SCRIPT.replace("__ATD_USERNAME__", '"tester"').replace(
            "__ATD_PASSWORD__", '"secret"'
        )
        page.runJavaScript(script, check_action)
        QTimer.singleShot(2500, finish)

    def check_action(value: object) -> None:
        action = json.loads(str(value))
        state["action"] = action
        print(json.dumps(action, ensure_ascii=False))

    page.loadFinished.connect(loaded)
    page.setHtml(HTML, QUrl("https://id.tsinghua.edu.cn/do/off/ui/auth/login/form"))
    app.exec()
    return 0 if state["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
