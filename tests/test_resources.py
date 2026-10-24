"""资源占用回归：日志保留、浏览器缓存上限与取 Token 会话释放。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import logging_setup  # noqa: E402
from auto_thu_deepseek_api.token import capture  # noqa: E402


class LogRetentionTest(unittest.TestCase):
    def test_only_old_program_logs_are_recycled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            today = date.today()
            active = root / f"app-{today:%Y%m%d}.log"
            old = root / f"app-{today - timedelta(days=8):%Y%m%d}.log.2"
            custom = root / f"app-{today - timedelta(days=30):%Y%m%d}.log.user"
            active.write_text("active", encoding="utf-8")
            old.write_text("old", encoding="utf-8")
            custom.write_text("keep", encoding="utf-8")

            removed_paths: list[Path] = []

            def fake_delete(path: Path) -> tuple[bool, str]:
                removed_paths.append(path)
                return True, "moved"

            with mock.patch.object(logging_setup.paths, "delete_path", side_effect=fake_delete):
                removed = logging_setup.prune_old_logs(
                    root,
                    7,
                    active_path=active,
                )

            self.assertEqual(removed, 1)
            self.assertEqual(removed_paths, [old])


class BrowserResourceTest(unittest.TestCase):
    def test_profile_has_bounded_disk_cache(self) -> None:
        class FakeScripts:
            def __init__(self) -> None:
                self.items = []

            def insert(self, script) -> None:  # noqa: ANN001
                self.items.append(script)

        class FakeScript:
            class InjectionPoint:
                DocumentCreation = 0

            class ScriptWorldId:
                MainWorld = 0

            def setName(self, value) -> None:  # noqa: ANN001
                self.name = value

            def setInjectionPoint(self, value) -> None:  # noqa: ANN001
                self.point = value

            def setWorldId(self, value) -> None:  # noqa: ANN001
                self.world = value

            def setRunsOnSubFrames(self, value) -> None:  # noqa: ANN001
                self.subframes = value

            def setSourceCode(self, value) -> None:  # noqa: ANN001
                self.source = value

        class FakeProfile:
            class HttpCacheType:
                DiskHttpCache = 1

            class PersistentCookiesPolicy:
                ForcePersistentCookies = 1

            def __init__(self, name) -> None:  # noqa: ANN001
                self.name = name
                self._scripts = FakeScripts()

            def setPersistentStoragePath(self, value) -> None:  # noqa: ANN001
                self.storage = value

            def setCachePath(self, value) -> None:  # noqa: ANN001
                self.cache = value

            def setHttpCacheType(self, value) -> None:  # noqa: ANN001
                self.cache_type = value

            def setHttpCacheMaximumSize(self, value) -> None:  # noqa: ANN001
                self.cache_max = value

            def setPersistentCookiesPolicy(self, value) -> None:  # noqa: ANN001
                self.cookies = value

            def scripts(self):  # noqa: ANN201
                return self._scripts

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(capture, "QWebEngineProfile", FakeProfile),
                mock.patch.object(capture, "QWebEngineScript", FakeScript),
            ):
                profile = capture.build_profile(Path(tmp))

            self.assertEqual(profile.cache_max, 32 * 1024 * 1024)
            self.assertEqual(profile.cache_type, 1)
            self.assertEqual(len(profile._scripts.items), 1)

    def test_finished_capture_deletes_page_and_clears_secrets(self) -> None:
        class FakeSignal:
            def connect(self, _slot) -> None:  # noqa: ANN001
                pass

            def disconnect(self, _slot) -> None:  # noqa: ANN001
                pass

        class FakeTimer:
            def __init__(self, _parent=None) -> None:
                self.timeout = FakeSignal()

            def setInterval(self, _value) -> None:  # noqa: ANN001
                pass

            def setSingleShot(self, _value) -> None:  # noqa: ANN001
                pass

            def start(self, _value=None) -> None:  # noqa: ANN001
                pass

            def stop(self) -> None:
                pass

        class FakePage:
            def __init__(self, _profile, _parent) -> None:
                self.loadFinished = FakeSignal()
                self.deleted = False

            def deleteLater(self) -> None:
                self.deleted = True

        class FakeCookieStore:
            cookieAdded = FakeSignal()

        class FakeProfile:
            def __init__(self) -> None:
                self.store = FakeCookieStore()

            def cookieStore(self):  # noqa: ANN201
                return self.store

        page = FakePage(None, None)
        with (
            mock.patch.object(capture, "QWebEnginePage", return_value=page),
            mock.patch.object(capture, "QTimer", FakeTimer),
        ):
            session = capture.TokenCaptureSession(
                FakeProfile(),
                silent=True,
                timeout_seconds=10,
                auto_username="user",
                auto_password="secret",
            )
            session._cookie_values.append("jwt")
            session.deleteLater = mock.Mock()  # type: ignore[method-assign]
            session.finish()

        self.assertTrue(page.deleted)
        self.assertEqual(session._auto_password, "")
        self.assertEqual(session._auto_username, "")
        self.assertEqual(session._cookie_values, [])
        session.deleteLater.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
