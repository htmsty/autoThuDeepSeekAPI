"""内置浏览器捕获 Token。

默认仍由用户手动登录。用户显式开启自动登录并保存凭据后，程序才在清华域名下
自动填充并提交；遇到验证码或二次验证会立即停下并打开窗口请用户接手。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal, QObject
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget

from .. import constants
from .jwt_utils import JwtCandidate, choose_jwt, looks_like_jwt

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

# 页面脚本：扫描浏览器存储、网络响应和页面文本中的多个 JWT 候选。
CAPTURE_SCRIPT = r"""
(function () {
  if (window.__atdInstalled) { return; }
  window.__atdInstalled = true;
  window.__atdToken = '';
  window.__atdCandidates = [];
  var JWT = /eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}/g;

  function take(value, source, key) {
    if (typeof value !== 'string' || !value) { return false; }
    if (value.length > 2 * 1024 * 1024) { return false; }
    var found = false;
    var match;
    JWT.lastIndex = 0;
    while ((match = JWT.exec(value)) !== null) {
      var token = match[0];
      found = true;
      window.__atdToken = token;
      var exists = window.__atdCandidates.some(function (item) {
        return item.token === token;
      });
      if (!exists) {
        window.__atdCandidates.push({
          token: token,
          source: source || 'text',
          key: key || '',
          url: location.href || ''
        });
      }
      if (window.__atdCandidates.length > 80) { window.__atdCandidates.shift(); }
    }
    return found;
  }

  function scanStores() {
    try {
      for (var i = 0; i < localStorage.length; i++) {
        var key = localStorage.key(i);
        take(localStorage.getItem(key), 'localStorage', key);
        if (key) { take(key, 'localStorageKey', key); }
      }
    } catch (e) { }
    try {
      for (var j = 0; j < sessionStorage.length; j++) {
        var key2 = sessionStorage.key(j);
        take(sessionStorage.getItem(key2), 'sessionStorage', key2);
      }
    } catch (e) { }
  }

  function scanText() {
    try {
      if (document.body) { take(document.body.innerText || '', 'text', ''); }
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
            var contentType = response.headers.get('content-type') || '';
            var contentLength = Number(response.headers.get('content-length') || 0);
            if (contentLength > 2 * 1024 * 1024 ||
                !/json|text|javascript/i.test(contentType)) {
              return;
            }
            response.clone().text().then(function (text) {
              take(text, 'network', response.url || '');
            }, function () { });
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
            try {
              if (this.responseText.length <= 2 * 1024 * 1024) {
                take(this.responseText, 'network', this.responseURL || '');
              }
            } catch (e) { }
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

AUTO_LOGIN_SCRIPT = r"""
(function (username, password) {
  var host = (location.hostname || '').toLowerCase();
  if (!(host === 'tsinghua.edu.cn' || host.endsWith('.tsinghua.edu.cn'))) {
    return {action: 'ignore'};
  }
  var bodyText = document.body ? (document.body.innerText || '') : '';
  var manualMarkers = [
    '二次验证', '双因素', '动态口令', '动态验证码', '手机验证码',
    '身份验证器', 'MFA', '两步验证', '请完成验证'
  ];
  var captcha = document.querySelector(
    'input[name*="captcha" i], input[id*="captcha" i], img[src*="captcha" i], .captcha'
  );
  var manual = captcha !== null || manualMarkers.some(function (marker) {
    return bodyText.indexOf(marker) >= 0;
  });
  if (manual) {
    return {action: 'manual', reason: '检测到验证码或二次验证，请手动完成'};
  }

  var passwordInput = document.querySelector('input[type="password"]');
  if (!passwordInput || passwordInput.offsetParent === null) {
    return {action: 'none'};
  }
  var scope = passwordInput.form || document;
  var candidates = Array.prototype.slice.call(
    scope.querySelectorAll('input[type="text"], input[type="email"], input:not([type])')
  );
  var usernameInput = candidates.find(function (item) {
    var marker = ((item.name || '') + ' ' + (item.id || '') + ' ' +
      (item.autocomplete || '')).toLowerCase();
    return /user|account|mail|login|学号|账号/.test(marker);
  }) || candidates.find(function (item) { return item.offsetParent !== null; });
  if (!usernameInput) {
    return {action: 'none'};
  }

  function setValue(input, value) {
    var descriptor = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(input), 'value'
    );
    if (descriptor && descriptor.set) {
      descriptor.set.call(input, value);
    } else {
      input.value = value;
    }
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
  }
  setValue(usernameInput, username);
  setValue(passwordInput, password);

  var button = scope.querySelector('button[type="submit"], input[type="submit"]');
  if (!button) {
    var buttons = Array.prototype.slice.call(scope.querySelectorAll('button'));
    button = buttons.find(function (item) {
      return /登录|登陆|sign\s*in|log\s*in/i.test(item.innerText || item.value || '');
    });
  }
  if (!button) {
    return {action: 'filled-no-submit'};
  }
  setTimeout(function () { button.click(); }, 250);
  return {action: 'submitted'};
})(__ATD_USERNAME__, __ATD_PASSWORD__)
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
    manual_intervention_required = Signal(str)

    def __init__(
        self,
        profile: QWebEngineProfile,
        *,
        silent: bool = False,
        timeout_seconds: int | None = None,
        url: str | None = None,
        auto_username: str = "",
        auto_password: str = "",
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
        self._poll_count = 0
        self._auto_username = auto_username
        self._auto_password = auto_password
        self._manual_prompted = False
        self._auto_submit_enabled = bool(auto_username and auto_password)

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
        if self._auto_username and self._auto_password:
            self._page.loadFinished.connect(self._on_load_finished)

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

    def _ensure_window(self) -> QMainWindow:
        if self._window is not None:
            return self._window
        self._window, self._view = self._build_window()
        if isinstance(self._window, CaptureWindow):
            self._window.user_closed.connect(self._on_window_closed)
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()
        return self._window

    def _on_load_finished(self, ok: bool) -> None:
        if self._finished or not ok or not self._auto_submit_enabled:
            return
        if not self._page.url().host().lower().endswith("tsinghua.edu.cn"):
            return
        script = AUTO_LOGIN_SCRIPT.replace(
            "__ATD_USERNAME__", json.dumps(self._auto_username, ensure_ascii=False)
        ).replace("__ATD_PASSWORD__", json.dumps(self._auto_password, ensure_ascii=False))
        self._page.runJavaScript(script, self._on_auto_login_result)

    def _on_auto_login_result(self, value: object) -> None:
        if self._finished or not isinstance(value, dict):
            return
        action = value.get("action")
        if action != "manual":
            return
        reason = str(value.get("reason") or "需要人工完成登录验证")
        self._auto_submit_enabled = False
        self._ensure_window()
        self._silent = False
        self._deadline.start(DEFAULT_INTERACTIVE_TIMEOUT_S * 1000)
        logger.warning("自动登录暂停：%s", reason)
        if not self._manual_prompted:
            self._manual_prompted = True
            self.manual_intervention_required.emit(reason)

    def finish(self) -> None:
        self._finished = True
        self._auto_password = ""
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
        self._poll_count += 1
        self._page.runJavaScript(
            "JSON.stringify(window.__atdCandidates || [])", self._on_js_result
        )

    def _on_js_result(self, value: object) -> None:
        if self._finished:
            return
        candidates: list[JwtCandidate] = []
        if isinstance(value, str) and value:
            try:
                raw = json.loads(value)
            except ValueError:
                raw = []
            if isinstance(raw, list):
                for item in raw:
                    if not isinstance(item, dict) or not isinstance(item.get("token"), str):
                        continue
                    candidates.append(
                        JwtCandidate(
                            token=item["token"],
                            source=str(item.get("source") or ""),
                            key=str(item.get("key") or ""),
                            url=str(item.get("url") or ""),
                        )
                    )
        page_url = self._page.url().toString()
        candidates.extend(
            JwtCandidate(token=token, source="cookie", key="cookie", url=page_url)
            for token in self._cookie_values
        )
        token = choose_jwt(candidates)
        if token:
            self._succeed(token)
        elif len({item.token for item in candidates}) > 1 and self._poll_count % 10 == 0:
            logger.info("页面里同时存在多个 JWT，候选证据不足，继续等待网络响应")

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

