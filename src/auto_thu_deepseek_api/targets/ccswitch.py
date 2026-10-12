"""CC Switch 配置写入：供应商卡片、当前路由、同站点旧卡片令牌。"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from pathlib import Path

from .. import constants
from ..backup import BackupManager
from ..paths import TargetPaths
from ..token.jwt_utils import looks_like_jwt
from .base import ApplyResult, TargetStatus

logger = logging.getLogger(__name__)


def is_running() -> bool:
    """CC Switch 是否正在运行（Windows）。"""

    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {constants.CCSWITCH_PROCESS_NAME}.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return constants.CCSWITCH_PROCESS_NAME in completed.stdout.lower()


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path.name} 解析失败：{exc}") from exc
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict, backup: BackupManager, label: str) -> bool:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if original == new_text:
        return False
    if path.exists():
        backup.backup_file(path, label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return True


class CcSwitchTarget:
    key = "ccswitch"
    label = "CC Switch"

    def __init__(self, paths: TargetPaths) -> None:
        self.paths = paths

    # —— 探测 ——

    def detect(self) -> TargetStatus:
        db = self.paths.db_file
        if db and db.exists():
            size_mb = db.stat().st_size / (1 << 20)
            return TargetStatus(self.key, self.label, True, self.paths.base_dir, f"找到数据库：{db}（{size_mb:.1f} MB）")
        return TargetStatus(self.key, self.label, False, self.paths.base_dir, f"未找到 CC Switch 数据库：{db}")

    # —— 写入 ——

    def apply(self, token: str, models: list[dict], backup: BackupManager) -> ApplyResult:
        result = ApplyResult(self.key, self.label)
        db = self.paths.db_file
        if not db or not db.exists():
            result.note(f"未找到数据库，跳过：{db}")
            return result

        try:
            self._write_database(db, token, models, backup, result)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                result.fail("数据库被占用：请先完全退出 CC Switch 再重试（或把策略改为「直接改」）")
            else:
                result.fail(f"数据库写入失败：{exc}")
            return result
        except Exception as exc:  # noqa: BLE001
            result.fail(f"数据库写入失败：{exc}")
            return result

        try:
            self._write_routing(token, backup, result)
        except Exception as exc:  # noqa: BLE001
            result.fail(f"当前路由写入失败：{exc}")
        return result

    # —— 数据库 ——

    def _write_database(
        self,
        db: Path,
        token: str,
        models: list[dict],
        backup: BackupManager,
        result: ApplyResult,
    ) -> None:
        connection = sqlite3.connect(str(db), timeout=15)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            columns = [row[1] for row in connection.execute("PRAGMA table_info(providers)")]
            has_current = "is_current" in columns

            row = connection.execute(
                "SELECT settings_config, meta, name, is_current FROM providers WHERE id = ? AND app_type = ?",
                (constants.CCSWITCH_PROVIDER_ID, constants.CCSWITCH_APP_TYPE),
            ).fetchone()

            # 先把"要写什么"算清楚，确实有变化再去备份和写库：
            # CC Switch 运行中时，能不碰它的数据库就不碰。
            if row is None:
                settings_config = self._new_settings_config(token, models)
                meta = self._new_meta()
                card_needs_write = True
            else:
                raw_settings, raw_meta, name, is_current = row
                settings_config = self._json_object(raw_settings)
                meta = self._json_object(raw_meta)
                changed = self._update_settings_config(settings_config, token, models)
                changed = self._update_meta(meta) or changed
                if name != constants.PROVIDER_DISPLAY_NAME:
                    changed = True
                if has_current and is_current != 1:
                    changed = True
                card_needs_write = changed

            current_fix = bool(has_current and self._other_current_cards(connection))
            refresh_targets = self._collect_refresh_targets(connection, token)
            if not card_needs_write and not current_fix and not refresh_targets:
                result.note("卡片与同站点旧卡片都已是最新，未改动 CC Switch 数据库")
                return

            backup.backup_sqlite(db, "ccswitch-db")
            connection.execute("BEGIN IMMEDIATE")
            try:
                if row is None:
                    connection.execute(
                        "INSERT INTO providers (id, app_type, name, settings_config, website_url, category,"
                        " created_at, sort_index, meta, is_current)"
                        " VALUES (?, ?, ?, ?, ?, ?, strftime('%s','now')*1000, ?, ?, 1)",
                        (
                            constants.CCSWITCH_PROVIDER_ID,
                            constants.CCSWITCH_APP_TYPE,
                            constants.PROVIDER_DISPLAY_NAME,
                            json.dumps(settings_config, ensure_ascii=False),
                            constants.MADMODEL_ORIGIN,
                            "custom",
                            0,
                            json.dumps(meta, ensure_ascii=False),
                        ),
                    )
                    result.note("已新建 CC Switch 供应商卡片 DeepSeek (THU)")
                elif card_needs_write:
                    assignments = ["settings_config = ?", "meta = ?", "name = ?"]
                    values: list[object] = [
                        json.dumps(settings_config, ensure_ascii=False),
                        json.dumps(meta, ensure_ascii=False),
                        constants.PROVIDER_DISPLAY_NAME,
                    ]
                    if has_current:
                        assignments.append("is_current = 1")
                    connection.execute(
                        f"UPDATE providers SET {', '.join(assignments)} WHERE id = ? AND app_type = ?",
                        (*values, constants.CCSWITCH_PROVIDER_ID, constants.CCSWITCH_APP_TYPE),
                    )
                    result.note("已更新供应商卡片 DeepSeek (THU)")

                if current_fix:
                    connection.execute(
                        "UPDATE providers SET is_current = 0 WHERE app_type = ? AND id <> ?",
                        (constants.CCSWITCH_APP_TYPE, constants.CCSWITCH_PROVIDER_ID),
                    )

                for payload, provider_id, app_type in refresh_targets:
                    connection.execute(
                        "UPDATE providers SET settings_config = ? WHERE id = ? AND app_type = ?",
                        (payload, provider_id, app_type),
                    )

                connection.commit()
            except Exception:
                connection.rollback()
                raise

            result.changed = True
            if refresh_targets:
                result.note(f"已刷新 {len(refresh_targets)} 张同站点旧卡片的令牌")
        finally:
            connection.close()

    @staticmethod
    def _json_object(raw: object) -> dict:
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _other_current_cards(self, connection: sqlite3.Connection) -> list[tuple[str, str]]:
        return connection.execute(
            "SELECT id, app_type FROM providers WHERE app_type = ? AND id <> ? AND is_current = 1",
            (constants.CCSWITCH_APP_TYPE, constants.CCSWITCH_PROVIDER_ID),
        ).fetchall()

    @staticmethod
    def _new_meta() -> dict:
        return {
            "commonConfigEnabled": True,
            "endpointAutoSelect": True,
            "apiFormat": constants.CCSWITCH_API_FORMAT,
        }

    def _new_settings_config(self, token: str, models: list[dict]) -> dict:
        catalog = self._catalog(models)
        model_id = catalog[0]["model"] if catalog else str(constants.DEFAULT_MODELS[0]["id"])
        config = (
            'model_provider = "custom"\n'
            f'model = "{model_id}"\n'
            'model_reasoning_effort = "high"\n'
            "disable_response_storage = true\n"
            "\n"
            "[model_providers.custom]\n"
            f'name = "{constants.PROVIDER_DISPLAY_NAME}"\n'
            f'base_url = "{constants.MADMODEL_API_BASE}"\n'
            'wire_api = "responses"\n'
            "requires_openai_auth = true\n"
        )
        settings_config: dict = {"auth": {"OPENAI_API_KEY": token}, "config": config}
        if catalog:
            settings_config["modelCatalog"] = {"models": catalog}
        return settings_config

    @staticmethod
    def _catalog(models: list[dict]) -> list[dict]:
        source = models or [dict(model) for model in constants.DEFAULT_MODELS]
        catalog: list[dict] = []
        for item in source:
            model_id = item.get("id")
            if not model_id:
                continue
            catalog.append(
                {
                    "model": str(model_id),
                    "displayName": str(item.get("name") or f"{model_id} (THU)"),
                    "contextWindow": int(item.get("contextWindow") or 1000000),
                    "inputModalities": list(item.get("input") or ["text", "image"]),
                }
            )
        return catalog

    def _update_settings_config(self, settings_config: dict, token: str, models: list[dict]) -> bool:
        changed = False
        auth = settings_config.get("auth")
        if not isinstance(auth, dict):
            auth = {}
            settings_config["auth"] = auth
        if auth.get("OPENAI_API_KEY") != token:
            auth["OPENAI_API_KEY"] = token
            changed = True

        catalog = self._catalog(models)
        if catalog and settings_config.get("modelCatalog", {}).get("models") != catalog:
            settings_config["modelCatalog"] = {"models": catalog}
            changed = True
        return changed

    @staticmethod
    def _update_meta(meta: dict) -> bool:
        changed = False
        if meta.get("apiFormat") != constants.CCSWITCH_API_FORMAT:
            meta["apiFormat"] = constants.CCSWITCH_API_FORMAT
            changed = True
        meta.setdefault("commonConfigEnabled", True)
        meta.setdefault("endpointAutoSelect", True)
        return changed

    def _collect_refresh_targets(
        self, connection: sqlite3.Connection, token: str
    ) -> list[tuple[str, str, str]]:
        """同站点（madmodel）的旧卡片也刷成新令牌；只读，返回 [(新配置, id, app_type)]。"""

        targets: list[tuple[str, str, str]] = []
        rows = connection.execute(
            "SELECT id, app_type, settings_config FROM providers WHERE id <> ? OR app_type <> ?",
            (constants.CCSWITCH_PROVIDER_ID, constants.CCSWITCH_APP_TYPE),
        ).fetchall()
        for provider_id, app_type, raw in rows:
            if not isinstance(raw, str) or constants.MADMODEL_HOST not in raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            if not self._replace_tokens(data, token):
                continue
            targets.append((json.dumps(data, ensure_ascii=False), provider_id, app_type))
            logger.info("准备刷新同站点卡片令牌：%s / %s", app_type, provider_id)
        return targets

    @classmethod
    def _replace_tokens(cls, node: object, token: str) -> bool:
        """把 JSON 里看起来是令牌的字符串换成新令牌。"""

        changed = False
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    if cls._is_token_value(key, value):
                        if value != token:
                            node[key] = token
                            changed = True
                elif cls._replace_tokens(value, token):
                    changed = True
        elif isinstance(node, list):
            for index, value in enumerate(node):
                if isinstance(value, str):
                    if looks_like_jwt(value) and value != token:
                        node[index] = token
                        changed = True
                elif cls._replace_tokens(value, token):
                    changed = True
        return changed

    @staticmethod
    def _is_token_value(key: str, value: str) -> bool:
        if not value:
            return False
        if looks_like_jwt(value):
            return True
        lowered = key.lower()
        return any(word in lowered for word in ("apikey", "api_key", "token")) and len(value) > 20

    # —— 路由 ——

    def _write_routing(self, token: str, backup: BackupManager, result: ApplyResult) -> None:
        settings_path = self.paths.settings_file
        if settings_path and settings_path.exists():
            data = _load_json(settings_path)
            key = f"currentProvider{constants.CCSWITCH_APP_TYPE.capitalize()}"
            if data.get(key) != constants.CCSWITCH_PROVIDER_ID:
                data[key] = constants.CCSWITCH_PROVIDER_ID
                if _write_json(settings_path, data, backup, "ccswitch-settings"):
                    result.changed = True
                    result.note(f"已把 settings.json 的 {key} 指向 DeepSeek (THU)")
            else:
                result.note("settings.json 的当前供应商已指向 DeepSeek (THU)")
        else:
            result.note(f"未找到 settings.json，跳过：{settings_path}")

        live_path = self.paths.live_state_file
        if live_path and live_path.exists():
            data = _load_json(live_path)
            apps = data.get("apps")
            if not isinstance(apps, dict):
                apps = {}
                data["apps"] = apps
            app_state = apps.get(constants.CCSWITCH_APP_TYPE)
            if not isinstance(app_state, dict):
                app_state = {}
                apps[constants.CCSWITCH_APP_TYPE] = app_state
            if app_state.get("proxy_route") != constants.CCSWITCH_PROVIDER_ID:
                app_state["proxy_route"] = constants.CCSWITCH_PROVIDER_ID
                if _write_json(live_path, data, backup, "ccswitch-live-state"):
                    result.changed = True
                    result.note("已把 live-state.json 的代理路由指向 DeepSeek (THU)")
                if "contract" in app_state:
                    result.note("注：live-state.json 里的 contract 未改动，CC Switch 下次启动会自行校准")
            else:
                result.note("live-state.json 的代理路由已指向 DeepSeek (THU)")
        else:
            result.note(f"未找到 live-state.json，跳过：{live_path}")

