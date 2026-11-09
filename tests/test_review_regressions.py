"""2.9.2 regressions: secrets, snapshots, network latency and safe shutdown."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import constants, crypto, http_tls, paths, sync
from auto_thu_deepseek_api.backup import BackupManager
from auto_thu_deepseek_api.settings import AppSettings
from auto_thu_deepseek_api.state_store import AppState
from auto_thu_deepseek_api.token import site_models, verifier


class NetworkSafetyTest(unittest.TestCase):
    def test_login_credentials_never_reach_lookalike_or_http_pages(self):
        from types import SimpleNamespace
        from PySide6.QtCore import QUrl
        from auto_thu_deepseek_api.token.capture import TokenCaptureSession

        for url, allowed in (
            ("https://id.tsinghua.edu.cn/login", True),
            ("https://tsinghua.edu.cn/login", True),
            ("https://eviltsinghua.edu.cn/login", False),
            ("https://tsinghua.edu.cn.evil.example/login", False),
            ("http://id.tsinghua.edu.cn/login", False),
            ("https://id.tsinghua.edu.cn:444/login", False),
        ):
            page = mock.Mock()
            page.url.return_value = QUrl(url)
            session = SimpleNamespace(_finished=False, _auto_submit_enabled=True, _page=page,
                                      _auto_username="fake-user", _auto_password="fake-password",
                                      _on_auto_login_result=lambda value: None)
            with self.subTest(url=url):
                TokenCaptureSession._on_load_finished(session, True)
                self.assertEqual(page.runJavaScript.called, allowed)

    def test_redirect_never_sends_token_to_another_origin(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{self.server.server_port}/sink")
                    self.end_headers()
                else:
                    received.append(self.headers.get("Authorization"))
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), http_tls.SameOriginRedirectHandler()
            )
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/start",
                headers={"Authorization": "Bearer fake-secret"},
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(request, timeout=2)
            error.exception.close()
            self.assertEqual(received, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_same_origin_redirect_preserves_authentication(self):
        request = urllib.request.Request("https://example.com/start", headers={"Authorization": "Bearer fake"})
        redirected = http_tls.SameOriginRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://example.com:443/next"
        )
        self.assertEqual(redirected.get_header("Authorization"), "Bearer fake")

    def test_https_downgrade_and_other_ports_are_rejected(self):
        for destination in ("http://example.com/next", "https://example.com:444/next"):
            with self.subTest(destination=destination), self.assertRaises(urllib.error.HTTPError):
                http_tls.SameOriginRedirectHandler().redirect_request(
                    urllib.request.Request("https://example.com/start"), None, 302, "Found", {}, destination
                )

    def test_refused_token_skips_model_download_and_target_writes(self):
        with mock.patch.object(sync, "verify_token", return_value=verifier.VerifyResult(False, "refused")), \
             mock.patch.object(sync.site_models, "resolve_models") as fetch, \
             mock.patch.object(sync, "build_targets", return_value=[]):
            report = sync.run_sync(AppSettings(), "fake")
        self.assertFalse(report.ok)
        fetch.assert_not_called()


class ModelPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch = mock.patch.object(paths, "data_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_failed_lookup_is_cached_for_five_minutes_then_retried(self):
        with mock.patch.object(site_models, "fetch_site_models", return_value=None) as fetch, \
             mock.patch.object(site_models, "fetch_models", return_value=None), \
             mock.patch.object(site_models.time, "time", return_value=1000):
            first = site_models.resolve_models("fake")
            first[0]["id"] = "changed-by-caller"
            second = site_models.resolve_models("fake")
            self.assertNotEqual(second[0]["id"], "changed-by-caller")
            self.assertEqual(fetch.call_count, 1)
        with mock.patch.object(site_models.time, "time", return_value=1301), \
             mock.patch.object(site_models, "fetch_site_models", return_value=None) as fetch:
            site_models.resolve_models()
            fetch.assert_called_once()

    def test_non_finite_cache_timestamp_is_ignored(self):
        for timestamp in (float("nan"), float("inf")):
            (self.root / "model-cache.json").write_text(
                json.dumps({"at": timestamp, "models": constants.DEFAULT_MODELS}), encoding="utf-8"
            )
            self.assertIsNone(site_models._load_cache())

    def test_no_more_asset_requests_after_fetch_budget(self):
        page = '<script src="/assets/js/app.js"></script>'
        with mock.patch.object(site_models.time, "monotonic", side_effect=[0, 26]), \
             mock.patch.object(site_models, "_get_text", return_value=page) as fetch:
            self.assertIsNone(site_models.fetch_site_models())
            self.assertEqual(fetch.call_count, 1)

    def test_foreign_asset_is_skipped(self):
        page = '<script src="https://outside.example/assets/js/app.js"></script>'
        with mock.patch.object(site_models, "_get_text", return_value=page) as fetch:
            self.assertIsNone(site_models.fetch_site_models())
            self.assertEqual(fetch.call_count, 1)

    def test_quoted_model_capabilities_are_preserved(self):
        models = site_models.extract_model_list(
            '''{"modelList": [{"value": "model-a", "supportImage": true, "effortOptions": ["high"]}]}'''
        )
        provider = site_models.to_provider_models(models)[0]
        self.assertEqual(provider["input"], ["text", "image"])
        self.assertEqual(provider["reasoningEfforts"], {"high": "high"})


class BackupIntegrityTest(unittest.TestCase):
    def test_packaged_sqlite_selftest_path_is_isolated(self):
        from auto_thu_deepseek_api import selftest

        with mock.patch.object(paths, "data_root", side_effect=AssertionError("real private accessed")):
            selftest.check_sqlite_backup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = BackupManager(self.root / "backups", keep=1)

    def tearDown(self):
        self.tmp.cleanup()

    def source(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_one_regular_backup_plus_original_is_bounded(self):
        source = self.source("a.txt", "original")
        original = self.manager.backup_file(source, "original-first-run")
        for i in range(4):
            source.write_text(str(i), encoding="utf-8")
            self.manager.backup_file(source, "auto")
        entries = self.manager.entries()
        self.assertEqual(len(entries), 2)
        self.assertTrue(original.exists())
        self.assertEqual(crypto.unprotect_bytes(Path(entries[-1].backup).read_bytes()), b"3")

    def test_shared_snapshot_retained_file_is_not_deleted(self):
        a, b = self.source("a.txt", "a"), self.source("b.txt", "b")
        self.manager.backup_file(a, "original")
        self.manager.backup_file(b, "original")
        self.manager.begin_snapshot("manual")
        old_a = self.manager.backup_file(a, "manual")
        kept_b = self.manager.backup_file(b, "manual")
        fresh = BackupManager(self.manager.root, keep=1)
        fresh.backup_file(a, "auto")
        self.assertTrue(kept_b.exists())
        self.assertNotIn(str(old_a), [e.backup for e in fresh.entries()])
        self.assertTrue(all(Path(e.backup).exists() for e in fresh.entries()))

    def test_rebuild_preserves_each_source_with_duplicate_filenames(self):
        a, b = self.source("a/config.yml", "one"), self.source("b/config.yml", "two")
        self.manager.begin_snapshot("original-first-run")
        self.manager.backup_file(a, "original-first-run")
        self.manager.backup_file(b, "original-first-run")
        self.manager.index_path.unlink()
        rebuilt = BackupManager(self.manager.root).entries()
        self.assertEqual({e.source for e in rebuilt}, {str(a), str(b)})
        self.assertTrue(all(e.is_original for e in rebuilt))
        a.write_text("changed", encoding="utf-8")
        b.write_text("changed", encoding="utf-8")
        BackupManager(self.manager.root).restore()
        self.assertEqual(a.read_text(), "one")
        self.assertEqual(b.read_text(), "two")

    def test_ambiguous_legacy_snapshot_is_skipped(self):
        snapshot = self.manager.root / "old-original"
        snapshot.mkdir(parents=True)
        (snapshot / "a.txt").write_bytes(b"a")
        (snapshot / "b.txt").write_bytes(b"b")
        (snapshot / "source.txt").write_text(str(self.root / "b.txt"))
        self.assertEqual(self.manager.entries(), [])

    def test_corrupt_backup_never_overwrites_source(self):
        source = self.source("a.txt", "original")
        backup = self.manager.backup_file(source, "original")
        backup.write_bytes(b"corrupted")
        source.write_text("keep-current", encoding="utf-8")
        messages = self.manager.restore()
        self.assertEqual(source.read_text(), "keep-current")
        self.assertTrue(any("校验和" in message for message in messages))

    def test_sqlite_special_filename_and_wal_snapshot(self):
        source = self.root / "中文 # config.db"
        db = sqlite3.connect(source)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE data(value)")
            db.execute("INSERT INTO data VALUES ('latest')")
            db.commit()
            backup = self.manager.backup_sqlite(source, "original")
            restored = self.root / "restored.db"
            restored.write_bytes(crypto.unprotect_bytes(backup.read_bytes()))
            check = sqlite3.connect(restored)
            try:
                self.assertEqual(check.execute("SELECT value FROM data").fetchone()[0], "latest")
            finally:
                check.close()
        finally:
            db.close()

    def test_sqlite_failure_does_not_copy_main_file_or_leave_plaintext(self):
        source = self.source("broken.db", "not a database")
        with self.assertRaises(OSError):
            self.manager.backup_sqlite(source, "test")
        self.assertEqual(list(self.manager.root.rglob("*.db")), [])

    def test_corrupt_state_field_types_do_not_crash_window(self):
        with mock.patch.object(paths, "data_root", return_value=self.root):
            (self.root / "state.json").write_text(json.dumps({
                "last_token": [1], "last_token_at": {}, "last_status": [],
                "main_window_size": [float("inf"), 600], "targets_note_ack": "false",
            }))
            state = AppState.load()
            self.assertEqual(state.last_token, "")
            self.assertEqual(state.last_token_at, "")
            self.assertEqual(state.last_status, "")
            self.assertFalse(state.targets_note_ack)
            self.assertEqual(state.main_window_size, [])

    def test_old_index_upgrades_shared_snapshot_sources(self):
        a, b = self.source("a/config.yml", "a"), self.source("b/config.yml", "b")
        self.manager.begin_snapshot("original-first-run")
        first = self.manager.backup_file(a, "original-first-run")
        self.manager.backup_file(b, "original-first-run")
        manifest = first.parent / "sources.json"
        manifest.unlink()
        BackupManager(self.manager.root).entries()
        self.assertEqual(set(json.loads(manifest.read_text()).values()), {str(a), str(b)})
        self.manager.index_path.unlink()
        self.assertEqual(len(BackupManager(self.manager.root).entries()), 2)

    def test_encrypted_backups_are_not_reread_on_every_index_load(self):
        source = self.source("a.txt", "secret")
        self.manager.backup_file(source, "original")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unnecessary payload read")):
            self.assertEqual(len(BackupManager(self.manager.root).entries()), 1)

    def test_malformed_index_items_are_ignored(self):
        source = self.source("a.txt", "content")
        self.manager.backup_file(source, "original")
        raw = json.loads(self.manager.index_path.read_text())
        raw.extend([None, "junk", {"source": [], "backup": 42}])
        self.manager.index_path.write_text(json.dumps(raw))
        self.assertEqual(len(BackupManager(self.manager.root).entries()), 1)

    def test_corrupt_settings_use_safe_types_and_keep_activation_disabled(self):
        with mock.patch.object(paths, "data_root", return_value=self.root):
            (self.root / "settings.json").write_text(json.dumps({
                "settings_version": float("inf"), "default_model_id": [], "dsh_dir": {},
                "sync_interval_minutes": float("inf"), "backup_keep": float("inf"),
                "auto_login_enabled": "false", "enable_ccswitch_activate": "false",
            }))
            settings = AppSettings.load()
            self.assertEqual(settings.default_model_id, "")
            self.assertEqual(settings.dsh_dir, "")
            self.assertEqual(settings.sync_interval_minutes, 60)
            self.assertFalse(settings.auto_login_enabled)
            self.assertFalse(settings.enable_ccswitch_activate)

    def test_sqlite_backup_deadline_stops_without_plaintext_file(self):
        source = self.root / "locked.db"
        connection = sqlite3.connect(source)
        connection.execute("CREATE TABLE x(value)")
        connection.commit()
        connection.close()
        with mock.patch("auto_thu_deepseek_api.backup.time.monotonic", side_effect=[0, 11]):
            with self.assertRaises(OSError):
                self.manager.backup_sqlite(source, "test")
        self.assertEqual(list(self.manager.root.rglob("*.db")), [])


class ShutdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_exit_waits_responsively_for_real_writer_thread(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        from auto_thu_deepseek_api.ui.main_window import MainWindow
        from auto_thu_deepseek_api.ui.workers import SyncWorker

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(paths, "data_root", return_value=Path(tmp)):
            entered, release = threading.Event(), threading.Event()
            window = MainWindow(AppSettings(), AppState())
            output = Path(tmp) / "completed.txt"

            def write(*args, **kwargs):
                entered.set()
                release.wait(3)
                output.write_text("complete")
                return sync.SyncReport()

            with mock.patch("auto_thu_deepseek_api.ui.workers.run_sync", side_effect=write), \
                 mock.patch.object(window, "_persist_settings", return_value=True), \
                 mock.patch.object(window, "_persist_state", return_value=True), \
                 mock.patch("auto_thu_deepseek_api.ui.main_window.QApplication.quit") as quit_app:
                worker = SyncWorker(AppSettings(), "fake", window)
                window._sync_worker = worker
                worker.start()
                self.assertTrue(entered.wait(2))
                ticks = []
                QTimer.singleShot(0, lambda: ticks.append(True))
                try:
                    started = time.monotonic()
                    window.quit_app()
                    self.assertLess(time.monotonic() - started, 0.5)
                    QTest.qWait(30)
                    self.assertEqual(ticks, [True])
                    self.assertFalse(quit_app.called)
                    self.assertTrue(worker._cancel.is_set())
                    window.start_sync()  # Quitting must not reopen login.
                    self.assertIsNone(window._capture)
                    release.set()
                    self.assertTrue(worker.wait(2000))
                    window._finish_quit_when_idle()
                    self.assertTrue(quit_app.called)
                    self.assertEqual(output.read_text(), "complete")
                finally:
                    release.set()
                    worker.wait(3000)
                    window._exit_timer.stop()
                    window.close()

    def test_slow_network_probe_does_not_block_gui_or_duplicate_capture(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        from auto_thu_deepseek_api.ui.main_window import MainWindow

        entered, release = threading.Event(), threading.Event()

        def probe():
            entered.set()
            release.wait(3)
            return True

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(paths, "data_root", return_value=Path(tmp)), \
             mock.patch("auto_thu_deepseek_api.ui.workers.site_reachable", side_effect=probe):
            window = MainWindow(AppSettings(), AppState())
            opened = []
            window._open_capture = lambda silent: opened.append(silent)
            window._should_capture_silently = lambda interactive: True
            try:
                start = time.monotonic()
                window.start_sync(interactive=False)
                self.assertLess(time.monotonic() - start, 0.5)
                worker = window._network_worker
                self.assertTrue(entered.wait(2))
                ticks = []
                QTimer.singleShot(0, lambda: ticks.append(True))
                window.start_sync(interactive=False)
                self.assertIs(window._network_worker, worker)
                QTest.qWait(30)
                self.assertEqual(ticks, [True])
                release.set()
                self.assertTrue(worker.wait(2000))
                self.app.processEvents()
                self.assertEqual(opened, [True])
                self.assertIsNone(window._network_worker)
            finally:
                release.set()
                if window._network_worker is not None:
                    window._network_worker.wait(3000)
                window._quitting = True
                window.close()

    def test_failed_sync_does_not_hide_error_window(self):
        from auto_thu_deepseek_api.targets.base import ApplyResult
        from auto_thu_deepseek_api.ui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(paths, "data_root", return_value=Path(tmp)):
            window = MainWindow(AppSettings(minimize_after_sync=True), AppState())
            try:
                with mock.patch.object(window, "hide") as hide, mock.patch.object(window, "notify"):
                    window._on_sync_report(sync.SyncReport(results=[ApplyResult("dsh", "DSH", ok=False)]))
                    hide.assert_not_called()
            finally:
                window._quitting = True
                window.close()


if __name__ == "__main__":
    unittest.main()
