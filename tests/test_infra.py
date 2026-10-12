"""基础设施：原子写、数据目录缓存、同步报告语义、开机启动命令。"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_thu_deepseek_api import paths, startup  # noqa: E402
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


class StartupCommandTest(unittest.TestCase):
    def test_source_mode_points_at_existing_run_dev(self) -> None:
        if paths.is_frozen():
            self.skipTest("打包环境下不适用")
        command = startup._command()
        match = re.search(r'"([^"]+run_dev\.py)"', command)
        self.assertIsNotNone(match, f"命令里找不到 run_dev.py：{command}")
        assert match is not None
        self.assertTrue(Path(match.group(1)).is_file(), f"开机启动指向的脚本不存在：{match.group(1)}")


if __name__ == "__main__":
    unittest.main()
