"""2.5：private 位置可改、干净卸载、启动提醒判据、安全终止同步。"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import paths, procinfo, sync, uninstall  # noqa: E402
from auto_thu_deepseek_api.settings import AppSettings, reset_to_defaults  # noqa: E402
from auto_thu_deepseek_api.sync import both_targets_missing, run_sync  # noqa: E402

from .sandbox import FAKE_NEW_TOKEN, Sandbox  # noqa: E402


class PrivateLocationTest(unittest.TestCase):
    """把 LOCALAPPDATA / data_root / 桌面都指到临时目录，别碰真机。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.local = self.root / "localappdata"
        self.local.mkdir()
        self._old_local = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(self.local)
        self._old_data_root = paths.data_root
        self._old_desktop = paths.desktop_dir
        self._old_gettempdir = tempfile.gettempdir
        # 校验逻辑会把"临时目录"当成危险位置，这里换个假的临时目录，好让被测目录通过
        tempfile.gettempdir = lambda: str(self.root / "fake-system-temp")  # type: ignore[assignment]
        paths.reset_data_root_cache()
        self.desktop = self.root / "Desktop"
        self.private = self.root / "data" / "private"
        self.private.mkdir(parents=True)
        paths.data_root = lambda: self.private  # type: ignore[assignment]
        paths.desktop_dir = lambda: self.desktop  # type: ignore[assignment]

    def tearDown(self) -> None:
        paths.data_root = self._old_data_root  # type: ignore[assignment]
        paths.desktop_dir = self._old_desktop  # type: ignore[assignment]
        tempfile.gettempdir = self._old_gettempdir  # type: ignore[assignment]
        if self._old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old_local
        paths.reset_data_root_cache()
        self._tmp.cleanup()

    # —— 指针文件 ——

    def test_pointer_round_trip(self) -> None:
        target = self.root / "moved"
        paths.write_private_override(target, migrated_from=self.private)
        location = paths.read_private_override()
        assert location is not None
        self.assertEqual(location.path, target)
        self.assertEqual(location.migrated_from, self.private)
        # 指针在 %LOCALAPPDATA%\autoThuDeepSeekAPI\ 下，不在 private 里
        self.assertEqual(paths.private_pointer_file().parent, paths.local_app_dir())
        self.assertTrue(paths.private_pointer_file().exists())

        paths.clear_private_override()
        self.assertIsNone(paths.read_private_override())

    def test_data_root_honours_pointer(self) -> None:
        target = self.root / "elsewhere" / "private"
        paths.write_private_override(target)
        # 这一条要测真实的 data_root()（其它用例把它换成了假的），所以临时换回来
        paths.data_root = self._old_data_root  # type: ignore[assignment]
        try:
            paths.reset_data_root_cache()
            self.assertEqual(paths.data_root(), target)
        finally:
            paths.data_root = lambda: self.private  # type: ignore[assignment]

    # —— 位置校验 ——

    def test_validate_rejects_dangerous_places(self) -> None:
        dangerous = [
            Path.cwd().anchor,  # 盘符根，例如 D:\
            Path(r"C:\Windows\System32\atd"),
            Path(r"C:\Program Files\atd"),
            Path(r"C:\ProgramData\atd"),
            Path(tempfile.gettempdir()) / "atd",
            self.desktop,
            self.desktop / "sub",
        ]
        for candidate in dangerous:
            ok, message = paths.validate_private_dir(candidate)
            self.assertFalse(ok, f"{candidate} 不该通过：{message}")

    def test_validate_accepts_normal_folder(self) -> None:
        ok, message = paths.validate_private_dir(self.root / "data" / "private-ok")
        self.assertTrue(ok, message)

    # —— 换位置 ——

    def test_switch_moves_managed_files_and_keeps_user_files(self) -> None:
        (self.private / "settings.json").write_text("{}", encoding="utf-8")
        (self.private / "logs").mkdir()
        (self.private / "logs" / "app.log").write_text("x", encoding="utf-8")
        (self.private / "my-notes.txt").write_text("我的东西", encoding="utf-8")
        target = self.root / "new-place"

        ok, message = paths.switch_private_root(target)
        self.assertTrue(ok, message)
        self.assertTrue((target / "settings.json").exists())
        self.assertTrue((target / "logs" / "app.log").exists())
        location = paths.read_private_override()
        assert location is not None
        self.assertEqual(location.path, target)
        self.assertEqual(location.migrated_from, self.private)
        # 旧位置：程序自己的东西清掉，用户自己的文件留着，也不整个删目录
        self.assertFalse((self.private / "settings.json").exists())
        self.assertTrue((self.private / "my-notes.txt").exists())
        self.assertTrue(self.private.is_dir())

        # 下次启动会把旧位置剩下的（日志/锁）补删干净
        paths.data_root = lambda: target  # type: ignore[assignment]
        paths.finish_pending_migration()
        self.assertFalse((self.private / "logs").exists())
        location = paths.read_private_override()
        assert location is not None
        self.assertIsNone(location.migrated_from)

    def test_switch_refuses_nested_or_same_folder(self) -> None:
        ok, message = paths.switch_private_root(self.private)
        self.assertFalse(ok, message)
        ok, message = paths.switch_private_root(self.private / "inside")
        self.assertFalse(ok, message)

    # —— 卸载 ——

    def test_clean_uninstall_removes_only_program_files(self) -> None:
        (self.private / "settings.json").write_text("{}", encoding="utf-8")
        (self.private / "state.json").write_text("{}", encoding="utf-8")
        (self.private / "browser-profile").mkdir()
        (self.private / "browser-profile" / "Cookies").write_text("x", encoding="utf-8")
        (self.private / "keep-me.txt").write_text("留着", encoding="utf-8")
        paths.write_private_override(self.root / "moved")

        ok, lines = uninstall.clean_uninstall(AppSettings(), restore_targets=False)
        self.assertTrue(ok, lines)
        self.assertFalse((self.private / "settings.json").exists())
        self.assertFalse((self.private / "browser-profile").exists())
        self.assertTrue((self.private / "keep-me.txt").exists(), "用户自己的文件不能动")
        self.assertFalse(paths.private_pointer_file().exists(), "位置指针也要清掉")

    def test_clean_uninstall_can_restore_first_backup(self) -> None:
        calls: list[object] = []
        original = uninstall.backup_ops.restore_original

        def fake_restore(settings):  # noqa: ANN001
            calls.append(settings)
            return ["已还原 DSH 凭据"]

        uninstall.backup_ops.restore_original = fake_restore  # type: ignore[assignment]
        try:
            ok, lines = uninstall.clean_uninstall(AppSettings(), restore_targets=True)
        finally:
            uninstall.backup_ops.restore_original = original  # type: ignore[assignment]
        self.assertTrue(ok, lines)
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("已还原" in line for line in lines), lines)


class ResetDefaultsTest(unittest.TestCase):
    def test_reset_restores_defaults_but_keeps_system_state(self) -> None:
        settings = AppSettings(
            sync_interval_minutes=45,
            auto_sync_enabled=False,
            unify_all_profiles=False,
            suppress_ccswitch_running_warning=False,
            launch_at_startup=True,
            last_sync_at="2026-01-01T00:00:00",
            last_sync_summary="上次同步的摘要",
        )
        reset_to_defaults(settings)
        self.assertEqual(settings.sync_interval_minutes, 180)
        self.assertTrue(settings.auto_sync_enabled)
        self.assertTrue(settings.unify_all_profiles)
        self.assertTrue(settings.suppress_ccswitch_running_warning)
        self.assertTrue(settings.launch_at_startup, "开机启动项不该被恢复默认动到")
        self.assertEqual(settings.last_sync_summary, "上次同步的摘要")


class TargetTraceTest(unittest.TestCase):
    def test_both_missing_when_nothing_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = AppSettings(
                dsh_dir=str(root / "no-dsh"), ccswitch_dir=str(root / "no-ccswitch")
            )
            original_procs = procinfo.running_process_names
            original_like = sync._installed_like_dir
            procinfo.running_process_names = lambda refresh=False: set()  # type: ignore[assignment]
            sync._installed_like_dir = lambda keywords: None  # type: ignore[assignment]
            try:
                missing, traces = both_targets_missing(settings)
            finally:
                procinfo.running_process_names = original_procs  # type: ignore[assignment]
                sync._installed_like_dir = original_like  # type: ignore[assignment]
            self.assertTrue(missing, traces)
            self.assertEqual(traces, {"dsh": "", "ccswitch": ""})

    def test_directory_counts_as_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dsh = root / ".dsh"
            dsh.mkdir()
            settings = AppSettings(
                dsh_dir=str(dsh), ccswitch_dir=str(root / "no-ccswitch")
            )
            original_procs = procinfo.running_process_names
            original_like = sync._installed_like_dir
            procinfo.running_process_names = lambda refresh=False: set()  # type: ignore[assignment]
            sync._installed_like_dir = lambda keywords: None  # type: ignore[assignment]
            try:
                missing, traces = both_targets_missing(settings)
            finally:
                procinfo.running_process_names = original_procs  # type: ignore[assignment]
                sync._installed_like_dir = original_like  # type: ignore[assignment]
            self.assertFalse(missing, traces)
            self.assertTrue(traces["dsh"])
            self.assertEqual(traces["ccswitch"], "")


class SyncCancelTest(unittest.TestCase):
    def test_cancel_before_start_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Sandbox.create(Path(tmp))
            settings = AppSettings(
                dsh_dir=str(sandbox.dsh_dir),
                ccswitch_dir=str(sandbox.ccswitch_dir),
                enable_ccswitch=False,
            )
            credentials = sandbox.dsh_dir / ".credentials.yaml"
            before = credentials.read_text(encoding="utf-8")
            event = threading.Event()
            event.set()

            report = run_sync(
                settings,
                FAKE_NEW_TOKEN,
                verify=False,
                fetch_model_list=False,
                cancel_event=event,
            )
            self.assertTrue(report.cancelled)
            self.assertEqual(report.results, [], "被终止时不该写任何目标")
            self.assertFalse(report.ok)
            self.assertIn("已被用户终止", report.summary())
            self.assertEqual(credentials.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
