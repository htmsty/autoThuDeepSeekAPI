"""2.2 加固：索引自愈、写入回滚、旧库结构、WAL、中文/空格路径、Token 时间语义。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import auto_thu_deepseek_api.targets.base as targets_base  # noqa: E402
from auto_thu_deepseek_api.backup import BackupManager  # noqa: E402
from auto_thu_deepseek_api.paths import TargetPaths  # noqa: E402
from auto_thu_deepseek_api.targets.ccswitch import CcSwitchTarget  # noqa: E402
from auto_thu_deepseek_api.targets.dsh import DshTarget  # noqa: E402
from auto_thu_deepseek_api.token import jwt_utils  # noqa: E402

from .sandbox import FAKE_NEW_TOKEN, Sandbox  # noqa: E402

MODELS = [
    {
        "id": "DeepSeek-V4.1-Flash",
        "name": "DeepSeek-V4.1-Flash (THU)",
        "contextWindow": 1000000,
        "input": ["text", "image"],
    },
    {
        "id": "qwen3.8-27b",
        "name": "qwen3.8-27b (THU)",
        "contextWindow": 1000000,
        "input": ["text", "image"],
    },
]


def _dsh_target(sandbox: Sandbox, unify: bool = False) -> DshTarget:
    return DshTarget(
        TargetPaths(
            base_dir=sandbox.dsh_dir,
            credentials_file=sandbox.dsh_dir / ".credentials.yaml",
            profiles_dir=sandbox.dsh_dir / "profiles",
        ),
        unify_all_profiles=unify,
    )


class BackupSelfHealTest(unittest.TestCase):
    def test_index_is_rebuilt_from_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sandbox = Sandbox.create(root)
            backups = root / "private" / "backups"
            manager = BackupManager(backups, keep=5)
            credentials = sandbox.dsh_dir / ".credentials.yaml"
            manager.backup_file(credentials, "dsh-credentials")
            index = backups / "index.json"
            self.assertTrue(index.exists())
            index.unlink()  # 模拟索引丢失

            rebuilt = BackupManager(backups, keep=5).entries()
            self.assertEqual(len(rebuilt), 1)
            self.assertEqual(rebuilt[0].source, str(credentials))
            self.assertTrue(Path(rebuilt[0].backup).exists())

            # 自愈出来的索引要能真的还原
            credentials.write_text("broken\n", encoding="utf-8")
            messages = BackupManager(backups, keep=5).restore()
            self.assertTrue(any("credentials" in message for message in messages), messages)
            self.assertNotEqual(credentials.read_text(encoding="utf-8"), "broken\n")


class WriteRollbackTest(unittest.TestCase):
    def test_failed_write_restores_the_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sandbox = Sandbox.create(root)
            credentials = sandbox.dsh_dir / ".credentials.yaml"
            original = credentials.read_text(encoding="utf-8")
            manager = BackupManager(sandbox.backups_dir, keep=5)
            target = _dsh_target(sandbox)

            original_writer = targets_base.write_text_atomic

            def boom(path, text, encoding="utf-8"):
                raise OSError("磁盘满了")

            targets_base.write_text_atomic = boom  # type: ignore[assignment]
            try:
                result = target.apply(FAKE_NEW_TOKEN, MODELS, manager)
            finally:
                targets_base.write_text_atomic = original_writer  # type: ignore[assignment]

            self.assertFalse(result.ok, result.messages)
            self.assertTrue(any("回滚" in error or "写入失败" in error for error in result.errors), result.errors)
            self.assertEqual(credentials.read_text(encoding="utf-8"), original, "失败后必须保持原样")


class CcSwitchCompatTest(unittest.TestCase):
    def _make_old_db(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE providers (
                id TEXT NOT NULL,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                settings_config TEXT NOT NULL,
                website_url TEXT,
                category TEXT,
                created_at INTEGER,
                sort_index INTEGER,
                meta TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (id, app_type)
            );
            """
        )
        connection.execute(
            "INSERT INTO providers (id, app_type, name, settings_config, category, created_at, sort_index, meta)"
            " VALUES (?, ?, ?, ?, 'custom', 1, 0, ?)",
            (
                "deepseek-thu",
                "codex",
                "DeepSeek (THU)",
                json.dumps({"auth": {"OPENAI_API_KEY": "old"}, "config": "model_provider = \"custom\"\n"}),
                "{}",
            ),
        )
        connection.commit()
        connection.close()

    def test_old_schema_without_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cc = root / ".cc-switch"
            cc.mkdir(parents=True)
            self._make_old_db(cc / "cc-switch.db")
            manager = BackupManager(root / "private" / "backups", keep=5)
            target = CcSwitchTarget(
                TargetPaths(base_dir=cc, db_file=cc / "cc-switch.db", settings_file=cc / "settings.json", live_state_file=cc / "live-state.json")
            )
            result = target.apply(FAKE_NEW_TOKEN, MODELS, manager)
            self.assertTrue(result.ok, result.errors)
            connection = sqlite3.connect(cc / "cc-switch.db")
            row = connection.execute(
                "SELECT settings_config FROM providers WHERE id='deepseek-thu'"
            ).fetchone()
            connection.close()
            self.assertEqual(json.loads(row[0])["auth"]["OPENAI_API_KEY"], FAKE_NEW_TOKEN)

    def test_wal_database_backup_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sandbox = Sandbox.create(root)
            db = sandbox.ccswitch_dir / "cc-switch.db"
            connection = sqlite3.connect(db)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("UPDATE providers SET name='WAL 里的改动' WHERE id='deepseek-thu'")
            connection.commit()
            connection.close()

            manager = BackupManager(sandbox.backups_dir, keep=5)
            backup_path = manager.backup_sqlite(db, "ccswitch-db")
            self.assertIsNotNone(backup_path)
            assert backup_path is not None

            check = sqlite3.connect(backup_path)
            name = check.execute("SELECT name FROM providers WHERE id='deepseek-thu'").fetchone()[0]
            check.close()
            self.assertEqual(name, "WAL 里的改动", "WAL 里未落盘的改动也要进备份")


class PathEdgeCaseTest(unittest.TestCase):
    def test_chinese_and_space_in_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "中文 目录"
            root.mkdir()
            sandbox = Sandbox.create(root)
            manager = BackupManager(sandbox.backups_dir, keep=5)
            dsh_result = _dsh_target(sandbox, unify=True).apply(FAKE_NEW_TOKEN, MODELS, manager)
            self.assertTrue(dsh_result.ok, dsh_result.errors)
            cc_result = CcSwitchTarget(
                TargetPaths(
                    base_dir=sandbox.ccswitch_dir,
                    db_file=sandbox.ccswitch_dir / "cc-switch.db",
                    settings_file=sandbox.ccswitch_dir / "settings.json",
                    live_state_file=sandbox.ccswitch_dir / "live-state.json",
                )
            ).apply(FAKE_NEW_TOKEN, MODELS, manager)
            self.assertTrue(cc_result.ok, cc_result.errors)

    def test_long_nested_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp)
            for index in range(12):
                deep = deep / f"层{index}-{'x' * 24}"
            deep.mkdir(parents=True)
            sandbox = Sandbox.create(deep)
            manager = BackupManager(sandbox.backups_dir, keep=5)
            result = _dsh_target(sandbox, unify=True).apply(FAKE_NEW_TOKEN, MODELS, manager)
            self.assertTrue(result.ok, result.errors)

    def test_read_only_data_dir_falls_back(self) -> None:
        """程序目录不可写时要退到 LOCALAPPDATA，而不是启动即崩。"""

        from auto_thu_deepseek_api import paths

        with tempfile.TemporaryDirectory() as tmp:
            readonly = Path(tmp) / "readonly"
            readonly.mkdir()
            original_app_dir = paths.app_dir
            original_local = os.environ.get("LOCALAPPDATA")
            paths.reset_data_root_cache()
            paths.app_dir = lambda: readonly  # type: ignore[assignment]
            os.environ["LOCALAPPDATA"] = str(Path(tmp) / "local")
            try:
                # 让探测认为程序目录不可写
                original_writable = paths._is_writable
                paths._is_writable = lambda p: str(p) != str(readonly / "private")  # type: ignore[assignment]
                try:
                    root = paths.data_root()
                finally:
                    paths._is_writable = original_writable  # type: ignore[assignment]
            finally:
                paths.app_dir = original_app_dir  # type: ignore[assignment]
                if original_local is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = original_local
                paths.reset_data_root_cache()
            self.assertIn("local", str(root).lower())


class ProfileLifecycleTest(unittest.TestCase):
    def test_removed_profile_is_not_resurrected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sandbox = Sandbox.create(root)
            manager = BackupManager(sandbox.backups_dir, keep=5)
            target = _dsh_target(sandbox, unify=True)
            self.assertTrue(target.apply(FAKE_NEW_TOKEN, MODELS, manager).ok)

            # 用户把 web profile 删了，下一轮同步不该把它"变回来"
            web_dir = sandbox.dsh_dir / "profiles" / "web"
            for child in web_dir.iterdir():
                child.unlink()
            web_dir.rmdir()

            second = target.apply(FAKE_NEW_TOKEN, MODELS, manager)
            self.assertTrue(second.ok, second.errors)
            self.assertFalse(web_dir.exists(), "被删掉的 profile 不该被重新创建")


class TokenTimeTest(unittest.TestCase):
    def test_expiry_and_skew(self) -> None:
        import base64
        import json as jsonlib

        def make_token(exp: datetime) -> str:
            header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
            payload = (
                base64.urlsafe_b64encode(
                    jsonlib.dumps({"exp": int(exp.timestamp())}).encode()
                )
                .decode()
                .rstrip("=")
            )
            return f"{header}.{payload}.signaturepart"

        now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        future = make_token(now + timedelta(hours=3))
        past = make_token(now - timedelta(minutes=1))
        really_expired = make_token(datetime.now(timezone.utc) - timedelta(minutes=5))

        self.assertFalse(jwt_utils.is_expired(future, now=now))
        self.assertTrue(jwt_utils.is_expired(past, now=now))
        self.assertTrue(jwt_utils.is_expired(really_expired), "五分钟前就过期的 Token 应当算过期")
        self.assertIsNone(jwt_utils.is_expired("not-a-jwt", now=now))

        # 界面用的是"有效至 …"（到期时刻），不是"剩余多少"
        soon = make_token(datetime.now(timezone.utc) + timedelta(hours=2, minutes=30))
        self.assertIn("有效至", jwt_utils.describe_expiry(soon, "2026-10-14T00:00:00"))

    def test_expiry_moment_prefers_jwt_exp(self) -> None:
        import base64
        import json as jsonlib

        exp = datetime(2030, 1, 2, 3, 4, tzinfo=timezone.utc)
        payload = (
            base64.urlsafe_b64encode(jsonlib.dumps({"exp": int(exp.timestamp())}).encode())
            .decode()
            .rstrip("=")
        )
        token = f"eyJhbGciOiJIUzI1NiJ9.{payload}.signaturepart"
        moment = jwt_utils.expiry_moment(token, "2026-10-13T00:00:00")
        assert moment is not None
        self.assertEqual(int(moment.timestamp()), int(exp.timestamp()))
        self.assertIn("有效至", jwt_utils.describe_expiry(token, "2026-10-13T00:00:00"))

    def test_expiry_moment_falls_back_to_five_hours(self) -> None:
        """Token 没带 exp 时，按"取回时刻 + 5 小时"估算到期时刻。"""

        no_exp = "eyJhbGciOiJIUzI1NiJ9.eyJjb2RlIjoiMCJ9.signaturepart"
        moment = jwt_utils.expiry_moment(no_exp, "2026-10-13T20:00:00")
        assert moment is not None
        self.assertEqual(moment.strftime("%Y-%m-%d %H:%M"), "2026-10-14 01:00")
        self.assertEqual(jwt_utils.expiry_moment(no_exp, ""), None)


if __name__ == "__main__":
    unittest.main()
