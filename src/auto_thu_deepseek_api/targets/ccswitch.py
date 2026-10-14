"""CC Switch 配置写入：供应商卡片、当前路由、同站点旧卡片令牌。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from pathlib import Path

from .. import constants
from ..backup import BackupManager
from ..paths import TargetPaths
from ..token.jwt_utils import looks_like_jwt
from .base import ApplyResult, RenderedFile, TargetStatus, emit, write_with_rollback

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


class CcSwitchTarget:
    key = "ccswitch"
    label = "CC Switch"

    def __init__(self, paths: TargetPaths, *, refresh_sibling_cards: bool = False) -> None:
        self.paths = paths
        # 刷新"同站点旧卡片"会改动别的卡片，默认关（要的用户在设置里显式打开）
        self.refresh_sibling_cards = bool(refresh_sibling_cards)
        # 干跑：只渲染将要写入的内容，不备份、不写库（预检用）
        self.dry_run = False
        self.rendered: list[RenderedFile] = []

    def _write_json(
        self,
        path: Path,
        data: dict,
        backup: BackupManager,
        label: str,
        *,
        patch: tuple[str, object, object] | None = None,
    ) -> bool:
        """写一个 JSON 文件。

        `patch=(key, 旧值, 新值)` 时，优先只替换那**一行**，其它行（含用户自己的键、
        缩进风格、紧凑写法）逐字不动；替换结果必须能解析且等于 data，否则退回整份重排。
        """

        original = path.read_text(encoding="utf-8") if path.exists() else ""
        if original:
            try:
                if json.loads(original) == data:
                    # 值没变：连排版都不动（原来会因为缩进风格不同而整份重写）
                    return False
            except ValueError:
                pass
        new_text = self._json_dump(data, original)
        if patch is not None and original:
            spliced = self._splice_json_line(original, new_text, *patch)
            if spliced is not None:
                try:
                    if json.loads(spliced) == data:
                        new_text = spliced
                except ValueError:  # pragma: no cover - 拼坏了就退回整份
                    pass
        if original == new_text:
            return False
        emit(self, path, new_text, label, "json")
        if self.dry_run:
            return True
        ok, message = write_with_rollback(path, new_text, backup, label)
        if not ok:
            raise OSError(message)
        return True

    @staticmethod
    def _splice_json_line(
        original_text: str, new_text: str, key: str, old_value: object, new_value: object
    ) -> str | None:
        """把 `"key": 旧值` 那一行换成新版本，其余行原样保留；不唯一就返回 None。

        只在"这个键在原文和新文里都只出现一次、且旧值正好等于 old_value"时才动手，
        免得误替换同名键（例如 live-state.json 里 claude / codex 各有一份 proxy_route）。
        """

        key_pattern = re.compile(rf'^\s*"{re.escape(key)}"\s*:')
        old_literal = json.dumps(old_value, ensure_ascii=False)
        target_pattern = re.compile(
            rf'^(\s*"{re.escape(key)}"\s*:\s*)' + re.escape(old_literal) + r'(\s*,?\s*)$'
        )
        original_lines = original_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)

        old_hits = [i for i, line in enumerate(original_lines) if key_pattern.match(line)]
        new_hits = [i for i, line in enumerate(new_lines) if key_pattern.match(line)]
        if len(old_hits) != 1 or len(new_hits) != 1:
            return None
        if not target_pattern.match(original_lines[old_hits[0]].rstrip("\r\n")):
            return None
        result = list(original_lines)
        result[old_hits[0]] = new_lines[new_hits[0]]
        return "".join(result)

    @staticmethod
    def _json_dump(data: dict, original_text: str) -> str:
        """按原文件的风格输出 JSON：单行还是单行，缩进宽度也照抄原文件。"""

        stripped = original_text.strip()
        if stripped and "\n" not in stripped:
            return json.dumps(data, ensure_ascii=False) + "\n"
        indent = 2
        for line in original_text.splitlines():
            body = line.lstrip(" ")
            if body and body != line:
                width = len(line) - len(body)
                if 0 < width <= 8:
                    indent = width
                break
        return json.dumps(data, ensure_ascii=False, indent=indent) + "\n"

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
            # 旧版 CC Switch 没有 is_current 这一列，查询里不能直接写它
            current_expr = "is_current" if has_current else "0 AS is_current"

            row = connection.execute(
                f"SELECT settings_config, meta, name, {current_expr} FROM providers WHERE id = ? AND app_type = ?",
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
            refresh_targets = self._refresh_targets(connection, token)

            if self.dry_run:
                # 预检：只把"将要写进卡片的内容"渲染出来，别碰数据库
                payload = json.dumps(
                    {
                        "name": constants.PROVIDER_DISPLAY_NAME,
                        "settings_config": settings_config,
                        "meta": meta,
                        "is_current": bool(has_current),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                emit(self, db, payload, "ccswitch-card", "sqlite-json")
                result.note("（预检）已渲染 CC Switch 卡片内容（未写库）")
                return

            if not card_needs_write and not current_fix and not refresh_targets:
                result.note("卡片已是最新，未改动 CC Switch 数据库")
                return

            backup.backup_sqlite(db, "ccswitch-db")
            connection.execute("BEGIN IMMEDIATE")
            try:
                # 拿到写锁后再核对一次：预判与真正开写之间，别人可能刚改过这张卡片。
                fresh = connection.execute(
                    f"SELECT settings_config, meta, name, {current_expr} FROM providers WHERE id = ? AND app_type = ?",
                    (constants.CCSWITCH_PROVIDER_ID, constants.CCSWITCH_APP_TYPE),
                ).fetchone()
                insert_mode = fresh is None
                if insert_mode:
                    card_needs_write = True
                else:
                    raw_settings, raw_meta, name, is_current = fresh
                    settings_config = self._json_object(raw_settings)
                    meta = self._json_object(raw_meta)
                    # 注意：这里必须分开调用，不能写成 a() or b() ——
                    # 短路会让 b() 根本不执行，meta（apiFormat）就会一直漏写。
                    changed_settings = self._update_settings_config(settings_config, token, models)
                    changed_meta = self._update_meta(meta)
                    card_needs_write = (
                        changed_settings
                        or changed_meta
                        or name != constants.PROVIDER_DISPLAY_NAME
                        or (has_current and is_current != 1)
                    )
                current_fix = bool(has_current and self._other_current_cards(connection))
                refresh_targets = self._refresh_targets(connection, token)
                if not card_needs_write and not current_fix and not refresh_targets:
                    connection.rollback()
                    result.note("加锁后发现已经是最新，未改动 CC Switch 数据库")
                    return

                if insert_mode:
                    insert_columns = [
                        "id",
                        "app_type",
                        "name",
                        "settings_config",
                        "website_url",
                        "category",
                        "created_at",
                        "sort_index",
                        "meta",
                    ]
                    insert_values: list[object] = [
                        constants.CCSWITCH_PROVIDER_ID,
                        constants.CCSWITCH_APP_TYPE,
                        constants.PROVIDER_DISPLAY_NAME,
                        json.dumps(settings_config, ensure_ascii=False),
                        constants.MADMODEL_ORIGIN,
                        "custom",
                        0,
                        json.dumps(meta, ensure_ascii=False),
                    ]
                    placeholders = ["?", "?", "?", "?", "?", "?", "strftime('%s','now')*1000", "?", "?"]
                    if has_current:
                        # 旧版 CC Switch 没有 is_current 这一列，有才写
                        insert_columns.append("is_current")
                        placeholders.append("1")
                    connection.execute(
                        f"INSERT INTO providers ({', '.join(insert_columns)})"
                        f" VALUES ({', '.join(placeholders)})",
                        tuple(insert_values),
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

    def _refresh_targets(
        self, connection: sqlite3.Connection, token: str
    ) -> list[tuple[str, str, str]]:
        """要不要顺带刷新别人卡片的令牌。

        **默认不刷**：那属于"改用户的其它配置"，只有用户在设置里明确打开才做。
        """

        if not self.refresh_sibling_cards:
            return []
        return self._collect_refresh_targets(connection, token)

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
        """这个字段是不是"该被换成新 Token"的字段。

        只认两类，避免误改同一张卡片里别的模型的 key：
        · 值本身就是 JWT（站点发的那种）；
        · 字段名**正好**是各家卡片放 Token 的名字（OPENAI_API_KEY / api_key / token 等），
          且值够长——用来兼容偶尔出现的非 JWT 旧 Token。
        """

        if not value:
            return False
        if looks_like_jwt(value):
            return True
        lowered = (key or "").lower()
        exact_names = {"openai_api_key", "anthropic_api_key", "apikey", "api_key", "token", "auth_token"}
        return lowered in exact_names and len(value) > 20

    # —— 路由 ——

    def _write_routing(self, token: str, backup: BackupManager, result: ApplyResult) -> None:
        settings_path = self.paths.settings_file
        if settings_path and settings_path.exists():
            data = _load_json(settings_path)
            key = f"currentProvider{constants.CCSWITCH_APP_TYPE.capitalize()}"
            previous = data.get(key)
            if previous != constants.CCSWITCH_PROVIDER_ID:
                data[key] = constants.CCSWITCH_PROVIDER_ID
                if self._write_json(
                    settings_path,
                    data,
                    backup,
                    "ccswitch-settings",
                    patch=(key, previous, constants.CCSWITCH_PROVIDER_ID),
                ):
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
                previous_route = app_state.get("proxy_route")
                app_state["proxy_route"] = constants.CCSWITCH_PROVIDER_ID
                if self._write_json(
                    live_path,
                    data,
                    backup,
                    "ccswitch-live-state",
                    patch=("proxy_route", previous_route, constants.CCSWITCH_PROVIDER_ID),
                ):
                    result.changed = True
                    result.note("已把 live-state.json 的代理路由指向 DeepSeek (THU)")
                if "contract" in app_state:
                    result.note("注：live-state.json 里的 contract 未改动，CC Switch 下次启动会自行校准")
            else:
                result.note("live-state.json 的代理路由已指向 DeepSeek (THU)")
        else:
            result.note(f"未找到 live-state.json，跳过：{live_path}")

