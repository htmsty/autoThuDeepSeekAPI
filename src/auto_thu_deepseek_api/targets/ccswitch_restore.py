"""Field-level restore adapter for CC Switch."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import constants, paths
from ..backup import BackupManager
from ..token.jwt_utils import looks_like_jwt
from .base import write_with_rollback
from .ccswitch import CcSwitchTarget, _load_json


def _get_nested(data: object, keys: list[str]) -> tuple[bool, object]:
    current: object = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return False, None
        current = current[key]
    return True, current


def _set_nested(data: dict, keys: list[str], present: bool, value: object) -> bool:
    if not keys:
        return False
    current: object = data
    for key in keys[:-1]:
        if not isinstance(current, dict):
            return False
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    if not isinstance(current, dict):
        return False
    if present:
        if current.get(keys[-1]) == value:
            return False
        current[keys[-1]] = value
    else:
        if keys[-1] not in current:
            return False
        current.pop(keys[-1], None)
    return True


def _restore_routing_record(record: dict) -> list[str]:
    path = Path(str(record.get("path") or ""))
    keys = [str(item) for item in record.get("json_path") or []]
    after_value = record.get("after_value")
    if not path.is_file() or not keys:
        return [f"CC Switch 路由文件不存在，跳过：{path}"]
    data = _load_json(path)
    present, current_value = _get_nested(data, keys)
    if not present or current_value != after_value:
        return [f"CC Switch 路由还原冲突：{path.name} 的 {'.'.join(keys)} 已被修改"]
    changed = _set_nested(
        data,
        keys,
        bool(record.get("before_present")),
        record.get("before_value"),
    )
    if not changed:
        return [f"CC Switch 路由无需还原：{path}"]
    backup = BackupManager(paths.backups_dir(), keep=10)
    ok, message = write_with_rollback(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        backup,
        "pre-field-restore",
    )
    if not ok:
        return [f"CC Switch 路由还原失败：{message}"]
    return [f"已按字段还原 CC Switch 路由：{path}"]


def _restore_token_fields(current: object, before: object, after: object) -> bool:
    changed = False
    if isinstance(current, dict) and isinstance(before, dict) and isinstance(after, dict):
        for key, value in list(current.items()):
            if key not in after:
                continue
            after_value = after[key]
            if isinstance(value, str) and isinstance(after_value, str):
                if (
                    CcSwitchTarget._is_token_value(str(key), value)
                    and value == after_value
                    and before.get(key) != value
                ):
                    if key in before:
                        current[key] = before[key]
                    else:
                        current.pop(key, None)
                    changed = True
            elif key in before and _restore_token_fields(value, before[key], after_value):
                changed = True
    elif isinstance(current, list) and isinstance(before, list) and isinstance(after, list):
        for index, value in enumerate(list(current)):
            if index >= len(after):
                continue
            if isinstance(value, str) and isinstance(after[index], str):
                if (
                    looks_like_jwt(value)
                    and value == after[index]
                    and (index >= len(before) or before[index] != value)
                ):
                    if index < len(before):
                        current[index] = before[index]
                    else:
                        current.pop(index)
                    changed = True
            elif index < len(before) and _restore_token_fields(
                value, before[index], after[index]
            ):
                changed = True
    return changed


def _restore_card_record(record: dict) -> list[str]:
    path = Path(str(record.get("path") or ""))
    if not path.is_file():
        return [f"CC Switch 数据库不存在，跳过：{path}"]
    after_settings = record.get("after_settings")
    after_meta = record.get("after_meta")
    before_settings = record.get("before_settings")
    before_meta = record.get("before_meta")
    if not isinstance(after_settings, dict) or not isinstance(after_meta, dict):
        return ["CC Switch 卡片还原失败：变更日志结构不完整"]
    before_settings = before_settings if isinstance(before_settings, dict) else {}
    before_meta = before_meta if isinstance(before_meta, dict) else {}

    connection = sqlite3.connect(str(path), timeout=15)
    messages: list[str] = []
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(providers)")]
        has_current = "is_current" in columns
        current_expr = "is_current" if has_current else "0 AS is_current"
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                f"SELECT settings_config, meta, name, {current_expr} "
                "FROM providers WHERE id = ? AND app_type = ?",
                (constants.CCSWITCH_PROVIDER_ID, constants.CCSWITCH_APP_TYPE),
            ).fetchone()
            if row is None:
                connection.rollback()
                return ["CC Switch 卡片已不存在，无需还原"]
            raw_settings, raw_meta, name, _is_current = row
            settings = CcSwitchTarget._json_object(raw_settings)
            meta = CcSwitchTarget._json_object(raw_meta)
            changed = False
            deleted_own_card = False

            if not bool(record.get("before_exists")):
                if (
                    settings == after_settings
                    and meta == after_meta
                    and name == str(record.get("after_name") or constants.PROVIDER_DISPLAY_NAME)
                ):
                    connection.execute(
                        "DELETE FROM providers WHERE id = ? AND app_type = ?",
                        (constants.CCSWITCH_PROVIDER_ID, constants.CCSWITCH_APP_TYPE),
                    )
                    changed = True
                    deleted_own_card = True
                    messages.append("已删除本程序自动创建的 CC Switch 卡片")
                    row = None
            if row is not None:
                auth = settings.get("auth")
                before_auth = before_settings.get("auth")
                before_auth = before_auth if isinstance(before_auth, dict) else {}
                if isinstance(auth, dict):
                    after_auth = after_settings.get("auth")
                    after_key = (
                        after_auth.get("OPENAI_API_KEY")
                        if isinstance(after_auth, dict)
                        else None
                    )
                    if (
                        after_key is not None
                        and auth.get("OPENAI_API_KEY") == after_key
                        and before_auth.get("OPENAI_API_KEY") != after_key
                    ):
                        if "OPENAI_API_KEY" in before_auth:
                            auth["OPENAI_API_KEY"] = before_auth["OPENAI_API_KEY"]
                        else:
                            auth.pop("OPENAI_API_KEY", None)
                        changed = True
                    elif after_key is not None and auth.get("OPENAI_API_KEY") != after_key:
                        messages.append("CC Switch 卡片 Token 已被其他程序修改，保留当前值")
                after_catalog = after_settings.get("modelCatalog")
                if "modelCatalog" in settings and settings.get("modelCatalog") == after_catalog:
                    if "modelCatalog" in before_settings:
                        settings["modelCatalog"] = before_settings["modelCatalog"]
                    else:
                        settings.pop("modelCatalog", None)
                    changed = True
                for key in ("apiFormat", "commonConfigEnabled", "endpointAutoSelect"):
                    if key in after_meta and meta.get(key) == after_meta[key]:
                        if key in before_meta:
                            meta[key] = before_meta[key]
                        else:
                            meta.pop(key, None)
                        changed = True
                if name == str(record.get("after_name") or constants.PROVIDER_DISPLAY_NAME):
                    before_name = str(record.get("before_name") or "")
                    if before_name and name != before_name:
                        name = before_name
                        changed = True
                if changed:
                    connection.execute(
                        "UPDATE providers SET settings_config = ?, meta = ?, name = ? "
                        "WHERE id = ? AND app_type = ?",
                        (
                            json.dumps(settings, ensure_ascii=False),
                            json.dumps(meta, ensure_ascii=False),
                            name,
                            constants.CCSWITCH_PROVIDER_ID,
                            constants.CCSWITCH_APP_TYPE,
                        ),
                    )

            if bool(record.get("activation_changed")) and has_current:
                current_state = [
                    {"id": str(item[0]), "current": bool(item[1])}
                    for item in connection.execute(
                        "SELECT id, is_current FROM providers WHERE app_type = ?",
                        (constants.CCSWITCH_APP_TYPE,),
                    ).fetchall()
                ]
                current_map = {
                    str(item.get("id") or ""): bool(item.get("current"))
                    for item in current_state
                }
                after_map = {
                    str(item.get("id") or ""): bool(item.get("current"))
                    for item in record.get("current_state_after") or []
                    if isinstance(item, dict)
                }
                if deleted_own_card:
                    after_map[constants.CCSWITCH_PROVIDER_ID] = False
                state_matches = all(
                    current_map.get(provider_id, False)
                    == after_map.get(provider_id, False)
                    for provider_id in set(current_map) | set(after_map)
                )
                if state_matches:
                    for item in record.get("current_state_before") or []:
                        connection.execute(
                            "UPDATE providers SET is_current = ? WHERE id = ? AND app_type = ?",
                            (
                                1 if item.get("current") else 0,
                                str(item.get("id") or ""),
                                constants.CCSWITCH_APP_TYPE,
                            ),
                        )
                    changed = True
                else:
                    messages.append("CC Switch 当前供应商选择已被修改，未还原激活状态")

            for sibling in record.get("sibling_changes") or []:
                if not isinstance(sibling, dict):
                    continue
                sibling_row = connection.execute(
                    "SELECT settings_config FROM providers WHERE id = ? AND app_type = ?",
                    (sibling.get("id"), sibling.get("app_type")),
                ).fetchone()
                if sibling_row is None:
                    continue
                current = CcSwitchTarget._json_object(sibling_row[0])
                if _restore_token_fields(current, sibling.get("before"), sibling.get("after")):
                    connection.execute(
                        "UPDATE providers SET settings_config = ? WHERE id = ? AND app_type = ?",
                        (
                            json.dumps(current, ensure_ascii=False),
                            sibling.get("id"),
                            sibling.get("app_type"),
                        ),
                    )
                    changed = True

            if changed:
                BackupManager(paths.backups_dir(), keep=10).backup_sqlite(
                    path, "pre-field-restore"
                )
                connection.commit()
                messages.append("已按字段还原 CC Switch 卡片")
            else:
                connection.rollback()
                messages.append("CC Switch 卡片无需还原或存在冲突")
            return messages
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()


def restore_journal_record(record: dict) -> list[str]:
    kind = str(record.get("kind") or "")
    if kind == "ccswitch_card":
        return _restore_card_record(record)
    if kind == "ccswitch_routing":
        return _restore_routing_record(record)
    return [f"不支持的 CC Switch 变更类型：{kind}"]
