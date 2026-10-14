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
from auto_thu_deepseek_api.settings import (  # noqa: E402
    CCSWITCH_POLICY_DIRECT,
    CCSWITCH_POLICY_PROMPT,
    AppSettings,
    reset_to_defaults,
)
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

    def test_pending_migration_keeps_marker_when_something_is_left(self) -> None:
        """旧位置还有删不掉的东西（被占用）时，重试标记要留着，下次启动继续清。"""

        (self.private / "settings.json").write_text("{}", encoding="utf-8")
        (self.private / "browser-profile").mkdir()
        (self.private / "browser-profile" / "Cookies").write_text("x", encoding="utf-8")
        target = self.root / "new-place"

        original_delete = paths.delete_path

        def fake_delete(path):  # noqa: ANN001
            if Path(path).name == "browser-profile":
                return False, "模拟被浏览器占着"
            return original_delete(path)

        paths.delete_path = fake_delete  # type: ignore[assignment]
        try:
            ok, message = paths.switch_private_root(target)
            self.assertTrue(ok, message)
            self.assertTrue((self.private / "browser-profile").exists(), "模拟的残留应当还在")

            paths.data_root = lambda: target  # type: ignore[assignment]
            paths.finish_pending_migration()
            location = paths.read_private_override()
            assert location is not None
            self.assertEqual(location.migrated_from, self.private, "还有残留就不能丢掉重试标记")
        finally:
            paths.delete_path = original_delete  # type: ignore[assignment]

        # 残留清掉之后再跑一次：这次要清干净并把标记去掉
        paths.finish_pending_migration()
        location = paths.read_private_override()
        assert location is not None
        self.assertIsNone(location.migrated_from)
        self.assertFalse(self.private.exists(), "没残留时旧目录应当被删掉")

    # —— 卸载 ——

    def test_move_aborts_and_keeps_old_data_when_a_file_fails(self) -> None:
        """搬运部分失败时必须中止：指针不切、旧数据一个字节不动、新位置的半成品收回。"""

        import shutil as shutil_mod
        from unittest import mock

        (self.private / "settings.json").write_text('{"interval": 42}', encoding="utf-8")
        (self.private / "state.json").write_text('{"last_token": "x"}', encoding="utf-8")
        target = self.root / "newplace"
        real_copy2 = shutil_mod.copy2

        def flaky_copy2(src, dst, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            if Path(src).name == "settings.json":
                raise OSError("模拟磁盘错误")
            return real_copy2(src, dst, *args, **kwargs)

        with mock.patch.object(paths.shutil, "copy2", side_effect=flaky_copy2):
            ok, message = paths.switch_private_root(target)

        self.assertFalse(ok, message)
        self.assertIn("搬运失败", message)
        self.assertEqual(
            (self.private / "settings.json").read_text(encoding="utf-8"),
            '{"interval": 42}',
            "没搬成功的文件必须留在原位置",
        )
        self.assertEqual(
            (self.private / "state.json").read_text(encoding="utf-8"),
            '{"last_token": "x"}',
            "旧位置的数据不该被清理",
        )
        self.assertIsNone(paths.read_private_override(), "失败时绝不能切位置指针")

    def test_clean_uninstall_keeps_empty_private_dir(self) -> None:
        """干净卸载只删程序管理的条目，不整个删目录（用户口径）。"""

        (self.private / "settings.json").write_text("{}", encoding="utf-8")
        ok, lines = uninstall.clean_uninstall(AppSettings(), restore_targets=False)
        self.assertTrue(ok, lines)
        self.assertFalse((self.private / "settings.json").exists())
        self.assertTrue(self.private.is_dir(), "只删条目、不删目录本身")

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
            ccswitch_running_policy=CCSWITCH_POLICY_PROMPT,
            launch_at_startup=True,
            last_sync_at="2026-01-01T00:00:00",
            last_sync_summary="上次同步的摘要",
        )
        reset_to_defaults(settings)
        self.assertEqual(settings.sync_interval_minutes, 180)
        self.assertTrue(settings.auto_sync_enabled)
        self.assertTrue(settings.unify_all_profiles)
        self.assertEqual(
            settings.ccswitch_running_policy,
            CCSWITCH_POLICY_DIRECT,
            "CC Switch 运行策略要回到默认值（不提示、直接改）",
        )
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


class DeferredCreationTest(unittest.TestCase):
    """启动早期不能创建 private：不然桌面提醒还没弹，文件夹已经在桌面上了。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.local = self.root / "localappdata"
        self.local.mkdir()
        self._old_local = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(self.local)
        self._old_desktop = paths.desktop_dir
        self._old_app_dir = paths.app_dir
        self.desktop = self.root / "Desktop"
        self.desktop.mkdir()
        paths.desktop_dir = lambda: self.desktop  # type: ignore[assignment]
        paths.app_dir = lambda: self.desktop  # type: ignore[assignment]
        from auto_thu_deepseek_api import startup as startup_module

        self._old_startup_desktop = startup_module.desktop_dir
        startup_module.desktop_dir = lambda: self.desktop  # type: ignore[assignment]
        paths.reset_data_root_cache()

    def tearDown(self) -> None:
        paths.defer_creation(False)
        paths.desktop_dir = self._old_desktop  # type: ignore[assignment]
        paths.app_dir = self._old_app_dir  # type: ignore[assignment]
        from auto_thu_deepseek_api import startup as startup_module

        startup_module.desktop_dir = self._old_startup_desktop  # type: ignore[assignment]
        if self._old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old_local
        paths.reset_data_root_cache()
        self._tmp.cleanup()

    def test_planned_root_is_not_created_while_deferred(self) -> None:
        from auto_thu_deepseek_api import startup

        self.assertTrue(startup.is_app_on_desktop(), "假的桌面上应当被判成在桌面")
        paths.defer_creation(True)
        try:
            planned = paths.data_root()
            paths.state_file()  # 只读地取路径，也不该创建
            self.assertFalse(planned.exists(), "延迟期间不该真的建目录")
            self.assertFalse((self.desktop / "private").exists(), "桌面上不该先冒出 private")
        finally:
            paths.defer_creation(False)
        paths.reset_data_root_cache()
        created = paths.data_root()
        self.assertEqual(created, planned)
        self.assertTrue(created.is_dir(), "恢复正常后才创建")


class RecycleDeleteTest(unittest.TestCase):
    def test_delete_path_uses_recycle_bin_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x.txt"
            target.write_text("hi", encoding="utf-8")
            ok, message = paths.delete_path(target)
            self.assertTrue(ok, message)
            self.assertFalse(target.exists())
            if sys.platform == "win32":
                self.assertIn("回收站", message, "Windows 上应当先走回收站")

    def test_remove_managed_data_removes_dir_when_only_ours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private"
            root.mkdir()
            (root / "settings.json").write_text("{}", encoding="utf-8")
            (root / "logs").mkdir()
            (root / "logs" / "a.log").write_text("x", encoding="utf-8")
            messages = paths.remove_managed_data(root)
            self.assertFalse(root.exists(), "只剩程序自己的东西时，目录也该删掉")
            self.assertTrue(messages, "要有说明")

    def test_remove_managed_data_keeps_dir_with_user_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private"
            root.mkdir()
            (root / "settings.json").write_text("{}", encoding="utf-8")
            (root / "mine.txt").write_text("用户自己的东西", encoding="utf-8")
            paths.remove_managed_data(root)
            self.assertTrue(root.is_dir(), "还有用户文件时不能删目录")
            self.assertTrue((root / "mine.txt").exists())


class InstanceLockTest(unittest.TestCase):
    def test_release_lets_the_next_process_in(self) -> None:
        from PySide6.QtCore import QLockFile

        from auto_thu_deepseek_api import instance

        with tempfile.TemporaryDirectory() as tmp:
            original = paths.data_root
            paths.data_root = lambda: Path(tmp)  # type: ignore[assignment]
            instance.release()
            try:
                self.assertTrue(instance.acquire(), "第一次取锁应当成功")
                other = QLockFile(str(Path(tmp) / "app.lock"))
                other.setStaleLockTime(0)
                self.assertFalse(other.tryLock(50), "别的实例应当拿不到")
                # 重启/卸载前主动让出：新进程马上就能起来（修"第一次自动退出"）
                instance.release()
                self.assertTrue(other.tryLock(200), "让出锁之后别人应当能拿到")
                other.unlock()
            finally:
                instance.release()
                paths.data_root = original  # type: ignore[assignment]


class DesktopNoticeTest(unittest.TestCase):
    def test_notice_text_and_ack(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QMessageBox

        import auto_thu_deepseek_api.data_location as data_location

        application = QApplication.instance() or QApplication(sys.argv[:1])
        self.assertIsNotNone(application)

        labels: list[str] = []
        original_add = QMessageBox.addButton
        original_exec = QMessageBox.exec
        original_clicked = QMessageBox.clickedButton
        QMessageBox.addButton = lambda self, text, *a: (labels.append(text), original_add(self, text, *a))[1]  # type: ignore[assignment]
        QMessageBox.exec = lambda self: 0  # type: ignore[assignment]
        QMessageBox.clickedButton = lambda self: None  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as tmp:
            original_root = paths.data_root
            paths.data_root = lambda: Path(tmp) / "private"  # type: ignore[assignment]
            try:
                from auto_thu_deepseek_api.state_store import AppState

                state = AppState()
                self.assertTrue(data_location.desktop_notice(None, state))
            finally:
                paths.data_root = original_root  # type: ignore[assignment]
                QMessageBox.addButton = original_add  # type: ignore[assignment]
                QMessageBox.exec = original_exec  # type: ignore[assignment]
                QMessageBox.clickedButton = original_clicked  # type: ignore[assignment]

        self.assertIn("退出程序去换本程序位置", labels)
        self.assertIn("选择 private 位置…", labels)
        self.assertIn("仍然继续", labels)

    def test_esc_close_does_not_mark_handled(self) -> None:
        """按 ESC / 点 × 关掉提醒：不能偷偷记成「已处理」，下次启动还要提醒。"""

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QMessageBox

        import auto_thu_deepseek_api.data_location as data_location
        from auto_thu_deepseek_api.state_store import AppState

        application = QApplication.instance() or QApplication(sys.argv[:1])
        self.assertIsNotNone(application)

        original_exec = QMessageBox.exec
        original_clicked = QMessageBox.clickedButton
        QMessageBox.exec = lambda self: 0  # type: ignore[assignment]
        QMessageBox.clickedButton = lambda self: None  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                original_root = paths.data_root
                paths.data_root = lambda: Path(tmp) / "private"  # type: ignore[assignment]
                try:
                    state = AppState()
                    self.assertTrue(data_location.desktop_notice(None, state))
                    self.assertFalse(state.desktop_note_ack, "没点任何按钮就不该记「已处理」")
                finally:
                    paths.data_root = original_root  # type: ignore[assignment]
        finally:
            QMessageBox.exec = original_exec  # type: ignore[assignment]
            QMessageBox.clickedButton = original_clicked  # type: ignore[assignment]


class LogHandleTest(unittest.TestCase):
    def test_repeated_setup_logging_does_not_leak_old_handles(self) -> None:
        """重复配置日志（启动、DEBUG、换位置各一次）不能把旧文件句柄留着。

        留着就会出现"logs 目录删不掉"（用户报的 WinError 32）——那是本轮的真根因。
        """

        import logging

        from auto_thu_deepseek_api import logging_setup

        with tempfile.TemporaryDirectory() as tmp:
            original = paths.data_root
            try:
                paths.data_root = lambda: Path(tmp) / "p1"  # type: ignore[assignment]
                first = logging_setup.setup_logging()
                logging.getLogger("atd-test").info("写一行，确保第一个文件真的被打开")

                paths.data_root = lambda: Path(tmp) / "p2"  # type: ignore[assignment]
                second = logging_setup.setup_logging()  # 相当于 DEBUG / 换位置时再配一次

                if sys.platform == "win32":
                    ok, message = paths.delete_path(first)
                    self.assertTrue(ok, f"换配置后旧日志文件不该还被占着：{message}")

                logging_setup.close_file_handlers()
                ok, message = paths.delete_path(second)
                self.assertTrue(ok, message)
            finally:
                logging_setup.close_file_handlers()
                paths.data_root = original  # type: ignore[assignment]
                logging_setup.setup_logging()

    def test_closing_file_handlers_unlocks_log_for_deletion(self) -> None:
        """用户遇到的 WinError 32：日志文件被占着删不掉。关掉句柄后必须能删/能搬。"""

        import logging

        from auto_thu_deepseek_api import logging_setup

        with tempfile.TemporaryDirectory() as tmp:
            original = paths.data_root
            paths.data_root = lambda: Path(tmp) / "private"  # type: ignore[assignment]
            try:
                log_file = logging_setup.setup_logging()
                logging.getLogger("atd-test").info("写一行，确保文件真的被打开")
                if sys.platform == "win32":
                    blocked, _ = paths.delete_path(log_file)
                    self.assertFalse(blocked, "日志开着时就该删不掉（这正是 WinError 32）")
                closed = logging_setup.close_file_handlers()
                self.assertGreaterEqual(closed, 1)
                ok, message = paths.delete_path(log_file)
                self.assertTrue(ok, message)
            finally:
                logging_setup.close_file_handlers()
                paths.data_root = original  # type: ignore[assignment]
                logging_setup.setup_logging()  # 把日志恢复到正常位置，别影响后面的用例


class NewVsOldInteractionTest(unittest.TestCase):
    """新功能别把旧逻辑搞坏：自动同步、维护动作、恢复默认之间的交互。"""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.application = QApplication.instance() or QApplication(sys.argv[:1])

    def _window(self):  # noqa: ANN202
        from auto_thu_deepseek_api.state_store import AppState
        from auto_thu_deepseek_api.ui.main_window import MainWindow

        return MainWindow(AppSettings(), AppState())

    def test_auto_sync_never_pops_the_force_dialog(self) -> None:
        """自动同步撞上"还在忙"应当安静跳过，不能弹「强行终止并重试」打扰用户。"""

        window = self._window()
        window._busy = lambda: True  # type: ignore[assignment]

        def boom() -> bool:  # pragma: no cover - 走到这里就是 bug
            raise AssertionError("自动同步不该弹强行终止对话框")

        window._ask_force_retry = boom  # type: ignore[assignment]
        try:
            window.start_sync(interactive=False)  # 不抛异常就对了
        finally:
            window._quitting = True
            window.close()

    def test_sync_is_skipped_while_maintenance_runs(self) -> None:
        window = self._window()
        window._maintenance = True
        window._busy = lambda: (_ for _ in ()).throw(AssertionError("维护期间不该继续走同步流程"))  # type: ignore[assignment]
        try:
            window.start_sync(interactive=True)  # 直接返回
        finally:
            window._maintenance = False
            window._quitting = True
            window.close()

    def test_busy_while_a_worker_object_still_exists(self) -> None:
        """线程跑完但 finished 还没派发时也算忙，别让两轮同步并排跑。"""

        window = self._window()
        try:
            self.assertFalse(window._busy())
            window._sync_worker = object()  # type: ignore[assignment]  # 只占位，模拟"还没收尾"
            self.assertTrue(window._busy(), "线程对象还在就该算忙")
        finally:
            window._sync_worker = None
            window._quitting = True
            window.close()

    def test_stale_worker_finished_does_not_clobber_new_one(self) -> None:
        """老线程的 finished 晚到时，不能把新线程的状态清掉。"""

        window = self._window()
        old_worker = object()
        new_worker = object()
        try:
            window._sync_worker = new_worker  # type: ignore[assignment]
            window._on_sync_thread_finished(old_worker)
            self.assertIs(window._sync_worker, new_worker, "新线程的引用被老线程清掉了")
        finally:
            window._sync_worker = None
            window._quitting = True
            window.close()

    def test_moving_private_pauses_auto_sync(self) -> None:
        window = self._window()
        try:
            window._prepare_for_data_move()
            self.assertTrue(window._maintenance)
            self.assertFalse(window._auto_timer.isActive())
            window._after_data_move()
            self.assertFalse(window._maintenance)
            self.assertTrue(window._auto_timer.isActive(), "搬完要恢复自动同步计时器")
        finally:
            window._quitting = True
            window.close()

    def test_restore_defaults_reset_position_uses_safe_hooks(self) -> None:
        """恢复默认设置的"还原 private 位置"必须和更改位置走同一条安全流程。"""

        from PySide6.QtWidgets import QMessageBox

        import auto_thu_deepseek_api.data_location as data_location
        import auto_thu_deepseek_api.ui.settings_dialog as settings_dialog

        window = self._window()
        dialog = settings_dialog.SettingsDialog(window.settings, window)
        captured: dict[str, object] = {}
        original_move = data_location.move_private_dir
        original_read = paths.read_private_override
        original_question = QMessageBox.question
        paths.read_private_override = lambda: paths.PrivateLocation(path=paths.data_root())  # type: ignore[assignment]
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]

        def fake_move(parent, target, *, prepare=None, after=None):  # noqa: ANN001
            captured["prepare"] = prepare
            captured["after"] = after
            captured["target"] = target
            return True, "（测试）已经搬好"

        data_location.move_private_dir = fake_move  # type: ignore[assignment]
        try:
            dialog._offer_reset_private_dir()
        finally:
            data_location.move_private_dir = original_move  # type: ignore[assignment]
            paths.read_private_override = original_read  # type: ignore[assignment]
            QMessageBox.question = original_question  # type: ignore[assignment]
            dialog.close()
            window._quitting = True
            window.close()

        self.assertEqual(captured.get("target"), paths.default_private_dir())
        self.assertIsNotNone(captured.get("prepare"), "必须带上「放开占用」的准备动作")
        self.assertIsNotNone(captured.get("after"), "必须带上搬完之后的收尾动作")

    def test_prune_uses_recycle_bin(self) -> None:
        """清理旧备份也应当走回收站，而不是永久删除。"""

        from auto_thu_deepseek_api.backup import BackupManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "backups"
            root.mkdir()
            source = Path(tmp) / "file.txt"
            source.write_text("x", encoding="utf-8")
            manager = BackupManager(root, keep=2)
            original_prune = manager.prune
            manager.prune = lambda: None  # type: ignore[assignment]  # 先攒够旧备份，最后手动清
            for _ in range(4):
                manager.backup_file(source, "auto-sync")
            manager.prune = original_prune  # type: ignore[assignment]

            calls: list[Path] = []
            original = paths.delete_path

            def fake_delete(path):  # noqa: ANN001
                calls.append(Path(path))
                return original(path)

            paths.delete_path = fake_delete  # type: ignore[assignment]
            try:
                manager.prune()
            finally:
                paths.delete_path = original  # type: ignore[assignment]
            self.assertTrue(calls, "prune 应当通过 paths.delete_path（回收站）删除")

    def test_repeated_backups_get_their_own_snapshot_dir(self) -> None:
        """同一个标签连续备份也要各占一个快照目录。

        否则旧备份全堆在一个目录里，prune 既清不掉、文件也会越积越多（2.5 的回归）。
        """

        from auto_thu_deepseek_api.backup import BackupManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "backups"
            root.mkdir()
            source = Path(tmp) / "file.txt"
            source.write_text("x", encoding="utf-8")
            manager = BackupManager(root, keep=5)
            for _ in range(3):
                manager.backup_file(source, "dsh-credentials")

            entries = manager.entries()
            dirs = {Path(entry.backup).parent for entry in entries}
            self.assertEqual(len(dirs), 3, f"三次备份应当是三个快照目录：{dirs}")

            manager.keep = 2
            manager.prune()
            left = {path for path in root.iterdir() if path.is_dir()}
            self.assertLessEqual(len(left), 2, f"清完最多剩 2 个目录：{left}")

    def test_explicit_snapshot_still_groups_files(self) -> None:
        """显式开的会话（首次运行那一批）仍然要把多份文件放进同一个目录。"""

        from auto_thu_deepseek_api.backup import BackupManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "backups"
            root.mkdir()
            first = Path(tmp) / "a.yaml"
            second = Path(tmp) / "b.yaml"
            first.write_text("a", encoding="utf-8")
            second.write_text("b", encoding="utf-8")
            manager = BackupManager(root, keep=5)
            manager.begin_snapshot("original-first-run")
            manager.backup_file(first, "original-first-run")
            manager.backup_file(second, "original-first-run")
            dirs = {Path(entry.backup).parent for entry in manager.entries()}
            self.assertEqual(len(dirs), 1, f"同一批应当共用一个目录：{dirs}")


class LockWaitTest(unittest.TestCase):
    def test_restart_waits_longer_than_normal_launch(self) -> None:
        from auto_thu_deepseek_api.app import _lock_wait_seconds

        self.assertGreater(_lock_wait_seconds(["--restarted"]), 5.0)
        self.assertLessEqual(_lock_wait_seconds([]), 1.0, "正常双击启动不该让用户干等")


class DesktopNoticeNoCreateTest(unittest.TestCase):
    """提醒弹出时不能已经在桌面把 private 建出来（连取 data_root() 都不行）。"""

    def test_notice_does_not_create_private_before_choice(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QMessageBox

        import auto_thu_deepseek_api.data_location as data_location

        application = QApplication.instance() or QApplication(sys.argv[:1])
        self.assertIsNotNone(application)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desktop = root / "Desktop"
            desktop.mkdir()
            private = desktop / "private"
            local = root / "localappdata"
            local.mkdir()
            original_local = os.environ.get("LOCALAPPDATA")
            os.environ["LOCALAPPDATA"] = str(local)
            original_desktop = paths.desktop_dir
            original_app_dir = paths.app_dir
            original_local_app_dir = paths.local_app_dir
            original_add = QMessageBox.addButton
            original_exec = QMessageBox.exec
            original_clicked = QMessageBox.clickedButton
            buttons: list[object] = []
            paths.reset_data_root_cache()
            paths.desktop_dir = lambda: desktop  # type: ignore[assignment]
            paths.app_dir = lambda: desktop  # type: ignore[assignment]
            paths.local_app_dir = lambda: local / "autoThuDeepSeekAPI"  # type: ignore[assignment]
            QMessageBox.addButton = lambda self, text, *a: (buttons.append(text), original_add(self, text, *a))[1]  # type: ignore[assignment]
            QMessageBox.exec = lambda self: 0  # type: ignore[assignment]
            # 模拟用户点「退出程序去换本程序位置」：按文字找那个按钮
            def fake_clicked(self):  # noqa: ANN001, ANN202
                for button in self.buttons():
                    if "退出程序" in button.text():
                        return button
                return None

            QMessageBox.clickedButton = fake_clicked  # type: ignore[assignment]
            continued = True
            # 和 app.py 一样：提醒期间保持"只判断、不落盘"
            try:
                from auto_thu_deepseek_api.state_store import AppState

                state = AppState()
                paths.defer_creation(True)
                try:
                    continued = data_location.desktop_notice(None, state)
                finally:
                    paths.defer_creation(False)
            finally:
                paths.desktop_dir = original_desktop  # type: ignore[assignment]
                paths.app_dir = original_app_dir  # type: ignore[assignment]
                paths.local_app_dir = original_local_app_dir  # type: ignore[assignment]
                QMessageBox.addButton = original_add  # type: ignore[assignment]
                QMessageBox.exec = original_exec  # type: ignore[assignment]
                QMessageBox.clickedButton = original_clicked  # type: ignore[assignment]
                if original_local is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = original_local
                paths.reset_data_root_cache()

            self.assertFalse(continued, "选退出就不该继续启动")
            self.assertIn("退出程序去换本程序位置", buttons)
            self.assertFalse(private.exists(), "提醒阶段不该在桌面创建 private")


if __name__ == "__main__":
    unittest.main()
