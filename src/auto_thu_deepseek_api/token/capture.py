"""内置浏览器捕获 Token。

登录永远由用户本人在窗口里完成：程序不模拟点击、不注入点击脚本。
页面脚本只做三件事：扫浏览器存储、钩网络响应、扫一次页面文本，命中 JWT 即交给 Python。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal, QObject
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget

from .. import constants
from .jwt_utils import find_jwt, looks_like_jwt

logger = logging.getLogger(__name__)

POLL_INTERVAL_MS = 1200
DEFAULT_INTERACTIVE_TIMEOUT_S = 300
DEFAULT_SILENT_TIMEOUT_S = 40


class CaptureWindow(QMainWindow):
    """登录窗口。用户自己点 X 关掉时要能让上层知道（否则状态会一直卡在"正在取 Token"）。"""

    user_closed = Signal()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 命名
        super().closeEvent(event)
        if event.isAccepted():
            self.user_closed.emit()

# 页面脚本：只读，不点击。
CAPTURE_SCRIPT = r"""
(function () {
  if (window.__atdInstalled) { return; }
  window.__atdInstalled = true;
  window.__atdToken = '';
  var JWT = /eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}/;

  function take(value) {
    if (typeof value !== 'string' || !value) { return false; }
    var m = value.match(JWT);
    if (m) { window.__atdToken = m[0]; return true; }
    return false;
  }

  function scanStores() {
    try {
      for (var i = 0; i < localStorage.length; i++) {
        var key = localStorage.key(i);
        if (take(localStorage.getItem(key))) { return; }
        if (key && JWT.test(key)) { window.__atdToken = key.match(JWT)[0]; return; }
      }
    } catch (e) { }
    try {
      for (var j = 0; j < sessionStorage.length; j++) {
        var key2 = sessionStorage.key(j);
        if (take(sessionStorage.getItem(key2))) { return; }
      }
    } catch (e) { }
  }

  function scanText() {
    try {
      if (document.body) { take(document.body.innerText || ''); }
    } catch (e) { }
  }

  function hookFetch() {
    if (!window.fetch) { return; }
    var original = window.fetch;
    window.fetch = function () {
      var result = original.apply(this, arguments);
      try {
        result.then(function (response) {
          try {
            response.clone().text().then(take, function () { });
          } catch (e) { }
        }, function () { });
      } catch (e) { }
      return result;
    };
  }

  function hookXhr() {
    try {
      var open = XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open = function () {
        try {
          this.addEventListener('load', function () {
            try { take(this.responseText); } catch (e) { }
          });
        } catch (e) { }
        return open.apply(this, arguments);
      };
    } catch (e) { }
  }

  hookFetch();
  hookXhr();
  setInterval(function () { scanStores(); scanText(); }, 1000);
  scanStores();
  scanText();
})();
"""


def build_profile(profile_dir: Path) -> QWebEngineProfile:
    """带持久化存储的独立浏览器档案（登录会话保存在这里）。"""

    profile_dir.mkdir(parents=True, exist_ok=True)
    profile = QWebEngineProfile(constants.APP_NAME)
    profile.setPersistentStoragePath(str(profile_dir))
    profile.setCachePath(str(profile_dir / "cache"))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)

    script = QWebEngineScript()
    script.setName("atd-capture")
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(True)
    script.setSourceCode(CAPTURE_SCRIPT)
    profile.scripts().insert(script)
    return profile


class TokenCaptureSession(QObject):
    """一次取 Token 的过程。silent=True 时不显示窗口（用于自动同步）。"""

    captured = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        profile: QWebEngineProfile,
        *,
        silent: bool = False,
        timeout_seconds: int | None = None,
        url: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._silent = silent
        self._finished = False
        self._profile = profile
        self._url = url or constants.MADMODEL_LOGIN_URL
        self._timeout_s = timeout_seconds or (
            DEFAULT_SILENT_TIMEOUT_S if silent else DEFAULT_INTERACTIVE_TIMEOUT_S
        )
        self._cookie_values: list[str] = []

        self._page = QWebEnginePage(profile, self)
        self._window: QMainWindow | None = None
        self._view: QWebEngineView | None = None
        if not silent:
            self._window, self._view = self._build_window()
            if isinstance(self._window, CaptureWindow):
                self._window.user_closed.connect(self._on_window_closed)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._on_timeout)

        profile.cookieStore().cookieAdded.connect(self._on_cookie_added)

    # —— 界面 ——

    def _build_window(self) -> tuple[QMainWindow, QWebEngineView]:
        window = CaptureWindow()
        window.setWindowTitle(f"登录清华 MadModel — {constants.APP_DISPLAY_NAME}")
        window.resize(980, 760)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        hint = QLabel("请在本窗口完成清华统一身份认证登录；登录成功后程序会自动获取 Token 并关闭本窗口。")
        hint.setWordWrap(True)
        hint.setStyleSheet("padding: 8px 12px; background: #F3F4F6; color: #111827;")
        layout.addWidget(hint)

        view = QWebEngineView()
        view.setPage(self._page)
        layout.addWidget(view, 1)
        window.setCentralWidget(central)
        return window, view

    def _on_window_closed(self) -> None:
        """用户点 X 关掉了登录窗口：按"取消本次取 Token"处理。

        以前这里什么都不做，`_capture` 会一直留着，之后每次同步都被当成"还在忙"忽略掉。
        """

        if self._finished:
            return
        logger.info("用户关闭了登录窗口，取消本次取 Token")
        self.finish()
        self.cancelled.emit()

    # —— 生命周期 ——

    def start(self) -> None:
        self._timer.start()
        self._deadline.start(self._timeout_s * 1000)
        if self._window is not None:
            self._window.show()
            self._window.raise_()
            self._window.activateWindow()
        self._page.load(QUrl(self._url))
        logger.info("已打开登录窗口（%s 模式）：%s", "静默" if self._silent else "交互", self._url)

    def finish(self) -> None:
        self._finished = True
        self._timer.stop()
        self._deadline.stop()
        try:
            self._profile.cookieStore().cookieAdded.disconnect(self._on_cookie_added)
        except (RuntimeError, TypeError):
            pass
        if self._window is not None:
            self._window.close()
            self._window.deleteLater()
            self._window = None

    def close_window(self) -> None:
        if self._window is not None:
            self._window.close()

    # —— 捕获 ——

    def _on_cookie_added(self, cookie) -> None:  # noqa: ANN001 - Qt 类型
        try:
            raw = cookie.value()
            if isinstance(raw, str):
                value = raw
            elif isinstance(raw, (bytes, bytearray)):
                value = raw.decode("utf-8", "replace")
            else:  # QByteArray 之类
                value = bytes(raw).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return
        if looks_like_jwt(value):
            self._cookie_values.append(value.strip())

    def _poll(self) -> None:
        if self._finished:
            return
        self._page.runJavaScript("window.__atdToken || ''", self._on_js_result)

    def _on_js_result(self, value: object) -> None:
        if self._finished:
            return
        token = find_jwt(value if isinstance(value, str) else "")
        if token is None and self._cookie_values:
            token = self._cookie_values[-1]
        if token:
            self._succeed(token)

    def _succeed(self, token: str) -> None:
        logger.info("已捕获 Token（长度 %d）", len(token))
        self.finish()
        self.captured.emit(token)

    def _on_timeout(self) -> None:
        if self._finished:
            return
        self.finish()
        if self._silent:
            self.failed.emit("静默取 Token 超时：多半需要重新登录")
        else:
            self.failed.emit("等待登录超时")

    def cancel(self) -> None:
        if self._finished:
            return
        self.finish()
        self.cancelled.emit()

