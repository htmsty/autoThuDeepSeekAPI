"""基础设施：原子写、数据目录缓存、同步报告语义、开机启动命令。"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import paths, procinfo, startup  # noqa: E402
from auto_thu_deepseek_api.sync import SyncReport  # noqa: E402
from auto_thu_deepseek_api.targets.base import ApplyResult  # noqa: E402
from auto_thu_deepseek_api.token.verifier import VerifyResult  # noqa: E402


class AtomicWriteTest(unittest.TestCase):
    def test_writes_and_replaces_without_leaving_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "file.txt"
            paths.write_text_atomic(target, "第一次")
            self.assertEqual(target.read_text(encoding="utf-8"), "第一次")
            paths.write_text_atomic(target, "第二次")
            self.assertEqual(target.read_text(encoding="utf-8"), "第二次")
            self.assertEqual(list(target.parent.glob("*.tmp")), [], "不该留下临时文件")


class DataRootTest(unittest.TestCase):
    def test_root_is_cached_and_resettable(self) -> None:
        paths.reset_data_root_cache()
        first = paths.data_root()
        self.assertTrue(first.is_dir())
        self.assertIs(paths.data_root(), first, "同一次运行里不该重复探测（共享目录上很慢）")
        paths.reset_data_root_cache()
        self.assertTrue(paths.data_root().is_dir(), "重置之后仍应拿到一个可用目录")


class RuntimeDirectoryTest(unittest.TestCase):
    def test_startup_can_defer_acl_hardening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "private"
            with (
                mock.patch.object(paths, "data_root", return_value=root),
                mock.patch.object(paths, "restrict_to_current_user") as restrict,
            ):
                paths.ensure_runtime_dirs(secure=False)
                self.assertTrue((root / "logs").is_dir())
                self.assertTrue((root / "backups").is_dir())
                self.assertTrue((root / "browser-profile").is_dir())
                restrict.assert_not_called()

                paths.harden_runtime_dirs()
                self.assertEqual(restrict.call_count, 3)


class SyncReportTest(unittest.TestCase):
    def test_rejected_token_is_not_ok(self) -> None:
        report = SyncReport(verified=VerifyResult(False, "站点拒绝该 Token"))
        self.assertFalse(report.ok)
        self.assertIn("Token 被站点拒绝", report.summary())

    def test_nothing_written_is_not_ok(self) -> None:
        report = SyncReport(verified=VerifyResult(True, "ok"))
        report.skipped.append("CC Switch（未安装）")
        self.assertFalse(report.ok, "一个目标都没写成，不能报成功")

    def test_all_targets_ok(self) -> None:
        report = SyncReport(verified=VerifyResult(True, "ok"))
        report.results.append(ApplyResult("dsh", "DSH", ok=True))
        self.assertTrue(report.ok)

    def test_one_target_failed(self) -> None:
        report = SyncReport(verified=VerifyResult(True, "ok"))
        report.results.append(ApplyResult("dsh", "DSH", ok=False))
        self.assertFalse(report.ok)


class StartupProbeWorkerTest(unittest.TestCase):
    def test_hardening_and_backup_finish_before_target_probe(self) -> None:
        from auto_thu_deepseek_api.settings import AppSettings
        from auto_thu_deepseek_api.ui import workers

        order: list[str] = []
        results = []

        def step(name: str, value=0):  # noqa: ANN001, ANN202
            def run(*_args, **_kwargs):  # noqa: ANN002, ANN003
                order.append(name)
                return value

            return run

        with (
            mock.patch.object(workers.paths, "harden_runtime_dirs", side_effect=step("acl")),
            mock.patch.object(
                workers,
                "auto_backup_on_first_run",
                side_effect=step("backup", 2),
            ),
            mock.patch.object(workers, "detect_targets", side_effect=step("statuses", [])),
            mock.patch.object(
                workers,
                "both_targets_missing",
                side_effect=step("missing", (False, {})),
            ),
        ):
            worker = workers.StartupProbeWorker(AppSettings())
            worker.ready.connect(results.append)
            worker.run()

        self.assertEqual(order, ["acl", "backup", "statuses", "missing"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].backups_done, 2)
        self.assertFalse(results[0].targets_missing)

    def test_cancel_skips_probe_only_after_first_run_backup(self) -> None:
        """手动同步可以打断探测，但不能把首次原始备份一起跳过。"""

        from auto_thu_deepseek_api.settings import AppSettings
        from auto_thu_deepseek_api.ui import workers

        order: list[str] = []
        results = []

        def step(name: str, value=0):  # noqa: ANN001, ANN202
            def run(*_args, **_kwargs):  # noqa: ANN002, ANN003
                order.append(name)
                return value

            return run

        with (
            mock.patch.object(workers.paths, "harden_runtime_dirs", side_effect=step("acl")),
            mock.patch.object(
                workers,
                "auto_backup_on_first_run",
                side_effect=step("backup", 2),
            ),
            mock.patch.object(workers, "detect_targets", side_effect=step("statuses")),
            mock.patch.object(
                workers,
                "both_targets_missing",
                side_effect=step("missing"),
            ),
        ):
            worker = workers.StartupProbeWorker(AppSettings())
            worker.ready.connect(results.append)
            worker.request_cancel()
            worker.run()

        self.assertEqual(order, ["acl", "backup"])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].interrupted)
        self.assertEqual(results[0].backups_done, 2)


class ProcessProbeTest(unittest.TestCase):
    def test_ccswitch_refresh_sees_a_process_started_after_cache(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows Toolhelp snapshot only")
        original = procinfo._CACHE
        try:
            procinfo._CACHE = {"old.exe"}
            with mock.patch.object(
                procinfo,
                "_windows_process_names",
                return_value={"cc-switch.exe"},
            ):
                self.assertEqual(
                    procinfo.any_running("cc-switch", refresh=True),
                    "cc-switch.exe",
                )
        finally:
            procinfo._CACHE = original


class StartupCommandTest(unittest.TestCase):
    def test_source_mode_points_at_existing_run_dev(self) -> None:
        if paths.is_frozen():
            self.skipTest("打包环境下不适用")
        command = startup._command()
        match = re.search(r'"([^"]+run_dev\.py)"', command)
        self.assertIsNotNone(match, f"命令里找不到 run_dev.py：{command}")
        assert match is not None
        self.assertTrue(Path(match.group(1)).is_file(), f"开机启动指向的脚本不存在：{match.group(1)}")

    def test_only_exact_current_command_counts_as_enabled(self) -> None:
        current = startup._command()
        with mock.patch.object(startup, "current_command", return_value=current):
            self.assertTrue(startup.enabled_for_current_app())
        with mock.patch.object(
            startup,
            "current_command",
            return_value='"D:\\other\\autoThuDeepSeekAPI.exe" --minimized',
        ):
            self.assertFalse(
                startup.enabled_for_current_app(),
                "另一份便携副本的启动项不属于当前程序，不能自动认领",
            )

    def test_desktop_shortcut_is_created_in_target_dir(self) -> None:
        """真的走一遍 WScript.Shell，但桌面目录换成临时目录，不碰用户桌面。"""

        if sys.platform != "win32":
            self.skipTest("只有 Windows 有 .lnk 快捷方式")
        with tempfile.TemporaryDirectory() as tmp:
            original = startup.desktop_dir
            startup.desktop_dir = lambda: Path(tmp)  # type: ignore[assignment]
            try:
                ok, message = startup.create_desktop_shortcut("AutoTHU 测试快捷方式")
            finally:
                startup.desktop_dir = original  # type: ignore[assignment]
            self.assertTrue(ok, message)
            self.assertTrue((Path(tmp) / "AutoTHU 测试快捷方式.lnk").exists())

    def test_app_on_desktop_only_accepts_desktop_root(self) -> None:
        """2.5：只认 exe 直接在桌面根目录，桌面下的子文件夹不算。"""

        from auto_thu_deepseek_api import paths as paths_module

        with tempfile.TemporaryDirectory() as tmp:
            desktop = Path(tmp) / "Desktop"
            desktop.mkdir()
            original_desktop = startup.desktop_dir
            original_app_dir = paths_module.app_dir
            startup.desktop_dir = lambda: desktop  # type: ignore[assignment]
            try:
                paths_module.app_dir = lambda: desktop  # type: ignore[assignment]
                self.assertTrue(startup.is_app_on_desktop())
                paths_module.app_dir = lambda: desktop / "autoThuDeepSeekAPI"  # type: ignore[assignment]
                self.assertFalse(startup.is_app_on_desktop(), "桌面下的子文件夹不算")
            finally:
                startup.desktop_dir = original_desktop  # type: ignore[assignment]
                paths_module.app_dir = original_app_dir  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
