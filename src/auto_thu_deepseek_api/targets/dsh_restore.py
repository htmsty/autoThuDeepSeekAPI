"""Field-level restore adapter for DeepSeek Harness."""

from __future__ import annotations

import copy
import re
from pathlib import Path

from .. import constants, paths
from ..backup import BackupManager
from .base import write_with_rollback
from .dsh import DshTarget, _yaml


def _restore_credentials_record(record: dict) -> list[str]:
    path = Path(str(record.get("path") or ""))
    key = str(record.get("key") or constants.DSH_CREDENTIAL_KEY)
    after = str(record.get("after_value") or "")
    before = str(record.get("before_value") or "")
    before_present = bool(record.get("before_present"))
    if not path.is_file():
        return [f"DSH 凭据已不存在，跳过：{path}"]
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    matched = False
    for index, line in enumerate(lines):
        if not re.match(rf"^\s*{re.escape(key)}\s*:", line):
            continue
        if after not in line:
            return [f"DSH 凭据还原冲突：{key} 已被其他程序修改，保留当前值"]
        matched = True
        if before_present:
            replaced = DshTarget._replace_ref_line(text, key, before)
            if replaced is None:
                return [f"DSH 凭据还原失败：无法只替换 {key} 这一行"]
            text = replaced
        else:
            del lines[index]
            text = "".join(lines)
        break
    if not matched:
        return [f"DSH 凭据无需还原或字段已不存在：{path}"]
    backup = BackupManager(paths.backups_dir(), keep=10)
    ok, message = write_with_rollback(path, text, backup, "pre-field-restore")
    if not ok:
        return [f"DSH 凭据还原失败：{message}"]
    return [f"已按字段还原 DSH 凭据 {key}：{path}"]


def _restore_simple_fields(
    current: dict,
    before: dict | None,
    after: dict | None,
    fields: list[str],
) -> tuple[bool, list[str]]:
    changed = False
    conflicts: list[str] = []
    after = after if isinstance(after, dict) else {}
    before = before if isinstance(before, dict) else {}
    for field in fields:
        if field not in after and field not in before:
            continue
        current_value = current.get(field)
        after_value = after.get(field)
        if current_value != after_value:
            conflicts.append(field)
            continue
        if field in before:
            if current.get(field) != before[field]:
                current[field] = copy.deepcopy(before[field])
                changed = True
        elif field in current:
            current.pop(field, None)
            changed = True
    return changed, conflicts


def _restore_profile_record(record: dict) -> list[str]:
    path = Path(str(record.get("path") or ""))
    if not path.is_file():
        return [f"DSH profile 已不存在，跳过：{path}"]
    original = path.read_text(encoding="utf-8")
    data = _yaml(original).load(original)
    if not isinstance(data, list):
        return [f"DSH profile 还原失败：{path} 顶层不是数组"]

    messages: list[str] = []
    provider_key = str(record.get("provider_key") or constants.DSH_PROVIDER_KEY)
    llm_entry = DshTarget._find_entry(
        data, constants.DSH_LLM_ENTRY_ID, constants.DSH_LLM_ENTRY_NAME
    )
    config = llm_entry.get("config") if isinstance(llm_entry, dict) else None
    providers = config.get("providers") if isinstance(config, dict) else None
    provider = providers.get(provider_key) if isinstance(providers, dict) else None

    provider_changed = False
    provider_removed = False
    llm_entry_removed = False
    if isinstance(provider, dict):
        provider_changed, conflicts = _restore_simple_fields(
            provider,
            record.get("provider_before"),
            record.get("provider_after"),
            [str(field) for field in record.get("managed_provider_fields") or []],
        )
        if conflicts:
            messages.append(
                f"DSH profile {path.parent.name} 冲突：provider {provider_key} 的 "
                + "、".join(conflicts)
                + " 已被其他程序修改，这些字段保持现状"
            )
        if provider_changed and not provider:
            providers.pop(provider_key, None)
            provider_removed = True
            if (
                bool(record.get("llm_entry_created"))
                and providers == {}
                and set(llm_entry.keys()) <= {"id", "name", "config"}
            ):
                data.remove(llm_entry)
                llm_entry_removed = True

    entry_changed = False
    entry_removed = False
    default_entry = DshTarget._find_entry(
        data,
        constants.DSH_DEFAULT_MODEL_ENTRY_ID,
        constants.DSH_DEFAULT_MODEL_ENTRY_NAME,
    )
    if bool(record.get("default_changed")) and isinstance(default_entry, dict):
        default_config = default_entry.get("config")
        if isinstance(default_config, dict):
            entry_changed, conflicts = _restore_simple_fields(
                default_config,
                (record.get("default_before") or {}).get("config")
                if isinstance(record.get("default_before"), dict)
                else None,
                (record.get("default_after") or {}).get("config")
                if isinstance(record.get("default_after"), dict)
                else None,
                [str(field) for field in record.get("managed_default_fields") or []],
            )
            if conflicts:
                messages.append(
                    f"DSH profile {path.parent.name} 冲突：默认模型的 "
                    + "、".join(conflicts)
                    + " 已被其他程序修改，这些字段保持现状"
                )
            if (
                entry_changed
                and bool(record.get("default_created"))
                and not default_config
                and set(default_entry.keys()) <= {"id", "name", "config"}
            ):
                data.remove(default_entry)
                entry_removed = True

    if not provider_changed and not entry_changed and not llm_entry_removed:
        return messages + [f"DSH profile 无需还原或已存在冲突：{path}"]

    import io

    stream = io.StringIO()
    _yaml(original).dump(data, stream)
    new_text = stream.getvalue()
    foreign = False
    for item in data:
        item_config = item.get("config") if isinstance(item, dict) else None
        item_providers = item_config.get("providers") if isinstance(item_config, dict) else None
        if not isinstance(item_providers, dict):
            continue
        for key in item_providers:
            if item is not llm_entry and str(key) == provider_key:
                foreign = True
            elif item is not llm_entry:
                foreign = True
    new_text = DshTarget._keep_other_blocks_verbatim(
        original,
        new_text,
        provider_key=provider_key,
        provider_changed=provider_changed and not provider_removed and not llm_entry_removed,
        provider_removed=provider_removed and not llm_entry_removed,
        llm_entry_removed=llm_entry_removed,
        entry_changed=entry_changed and not entry_removed,
        entry_removed=entry_removed,
        allow_full_rewrite=not foreign,
    )
    check = _yaml(new_text).load(new_text)
    if not isinstance(check, list):
        return messages + [f"DSH profile 还原失败：回读不是数组 {path}"]
    backup = BackupManager(paths.backups_dir(), keep=10)
    ok, message = write_with_rollback(path, new_text, backup, "pre-field-restore")
    if not ok:
        return messages + [f"DSH profile 还原失败：{message}"]
    messages.append(f"已按字段还原 DSH profile：{path}")
    return messages


def restore_journal_record(record: dict) -> list[str]:
    kind = str(record.get("kind") or "")
    if kind == "dsh_credentials":
        return _restore_credentials_record(record)
    if kind == "dsh_profile":
        return _restore_profile_record(record)
    return [f"不支持的 DSH 变更类型：{kind}"]
