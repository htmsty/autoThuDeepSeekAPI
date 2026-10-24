"""CC Switch 配置写入：供应商卡片、当前路由、同站点旧卡片令牌。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import textwrap
from pathlib import Path

from .. import constants
from ..backup import BackupManager
from ..change_journal import ChangeJournal
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


def _plain(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class CcSwitchTarget:
    key = "ccswitch"
    label = "CC Switch"

    def __init__(
        self,
        paths: TargetPaths,
        *,
        enable_card: bool = True,
        activate: bool = False,
        refresh_sibling_cards: bool = False,
    ) -> None:
        self.paths = paths
        self.enable_card = bool(enable_card)
        self.activate = bool(activate)
        # 刷新"同站点旧卡片"会改动别的卡片，默认关（要的用户在设置里显式打开）
        self.refresh_sibling_cards = bool(refresh_sibling_cards)
        # 干跑：只渲染将要写入的内容，不备份、不写库（预检用）
        self.dry_run = False
        self.rendered: list[RenderedFile] = []
        self._journal_records: list[Path] = []

    def _write_json(
        self,
        path: Path,
        data: dict,
        backup: BackupManager,
        label: str,
        *,
        patch: tuple[str, object] | None = None,
        insert: tuple[tuple[str, ...], str, object] | None = None,
    ) -> bool:
        """写一个 JSON 文件。

        `patch=(key, 旧值)` 时，优先只替换那**一行**，其它行（含用户自己的键、
        缩进风格、紧凑写法）逐字不动；替换结果必须能解析且等于 data，否则退回整份重排。

        `insert=(对象路径, key, value)` 是"键还不存在"时的兜底：只往那个对象末尾补一行
        （缺的中间层整块补出来），同样要求解析结果等于 data，失败才退回整份重排。
        这样别的应用（例如 CC Switch 里的 claude 配置）的排版不会被顺手重写。
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
        if original:
            spliced = self._splice_json_line(original, new_text, *patch) if patch is not None else None
            if spliced is None and insert is not None:
                spliced = self._insert_json_key(original, *insert)
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
        original_text: str, new_text: str, key: str, old_value: object
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

    # —— 最小插入：键还不存在时只往对象末尾补一行 ——

    @classmethod
    def _insert_json_key(
        cls, original_text: str, path: tuple[str, ...], key: str, value: object
    ) -> str | None:
        """在 `path` 指向的对象末尾插入 `key: value`，其余文本一字不动。

        路径里缺的中间对象会整块补出来（例如 live-state.json 还没有 apps.codex）。
        单行 JSON、找不到顶层对象、键已经存在等情况返回 None，调用方退回整份重排。
        """

        if "\n" not in original_text:
            # 单行 JSON 本身就是紧凑写法，整份重写不会重排别人的东西
            return None
        top = original_text.find("{")
        if top < 0:
            return None
        top_end = cls._match_brace(original_text, top)
        if top_end is None:
            return None

        current_start, current_end = top, top_end
        for depth, segment in enumerate(path):
            value_start = cls._find_json_key(original_text, current_start, current_end, segment)
            if value_start is None:
                # 缺了这一层：把它以及剩下的路径整块补在当前对象末尾
                nested = cls._nested_payload(path[depth + 1 :], key, value)
                return cls._append_json_member(
                    original_text, current_start, current_end, segment, nested
                )
            if original_text[value_start] != "{":
                return None
            current_start = value_start
            matched = cls._match_brace(original_text, current_start)
            if matched is None:
                return None
            current_end = matched

        if cls._find_json_key(original_text, current_start, current_end, key) is not None:
            # 已经存在了：绝不能插成重复键
            return None
        return cls._append_json_member(original_text, current_start, current_end, key, value)

    @staticmethod
    def _nested_payload(rest: tuple[str, ...], key: str, value: object) -> object:
        """把剩余路径包成一个嵌套对象：rest=("a","b") → {"a": {"b": {key: value}}}。"""

        payload: object = {key: value}
        for segment in reversed(rest):
            payload = {segment: payload}
        return payload

    @staticmethod
    def _match_brace(text: str, start: int) -> int | None:
        """text[start] == '{' 时返回配对的 '}' 下标（跳过字符串与转义）。"""

        depth = 0
        quote = ""
        index = start
        while index < len(text):
            char = text[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = ""
                index += 1
                continue
            if char == '"':
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index
            index += 1
        return None

    @classmethod
    def _find_json_key(cls, text: str, obj_start: int, obj_end: int, key: str) -> int | None:
        """在**这一层**对象里找 `"key":`，返回值起始下标；找不到返回 None。

        嵌套对象里的同名键不算（用括号深度过滤，例如 live-state.json 里
        claude / codex 各有一份 proxy_route 时不会互相串）。
        """

        pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*')
        for match in pattern.finditer(text, obj_start + 1, obj_end):
            if cls._depth_at(text, obj_start, match.start()) != 0:
                continue
            value_start = match.end()
            while value_start < obj_end and text[value_start] in " \t\r\n":
                value_start += 1
            return value_start if value_start < obj_end else None
        return None

    @staticmethod
    def _depth_at(text: str, obj_start: int, index: int) -> int:
        """obj_start 之后、index 之前，括号的净深度（跳过字符串里的括号）。"""

        depth = 0
        quote = ""
        position = obj_start + 1
        while position < index:
            char = text[position]
            if quote:
                if char == "\\":
                    position += 2
                    continue
                if char == quote:
                    quote = ""
            elif char == '"':
                quote = char
            elif char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
            position += 1
        return depth

    @classmethod
    def _append_json_member(
        cls, text: str, obj_start: int, obj_end: int, key: str, value: object
    ) -> str | None:
        """把 `"key": value` 追加到对象末尾，返回新文本；结构不认识时返回 None。"""

        last = obj_end - 1
        while last > obj_start and text[last] in " \t\r\n":
            last -= 1
        empty_object = text[last] == "{"
        child_indent = cls._child_indent(text, obj_start, obj_end)
        parent_indent = cls._line_indent(text, obj_start)

        body = json.dumps({key: value}, ensure_ascii=False, indent=2)
        inner_lines = body.splitlines()[1:-1]
        if not inner_lines:
            return None
        # json.dumps 自带 2 格基础缩进，先去掉，再整体平移成原文子键的缩进
        inner = textwrap.dedent("\n".join(inner_lines))
        member = "\n".join(
            child_indent + line if line.strip() else "" for line in inner.splitlines()
        )

        if empty_object:
            new_object = "{" + "\n" + member + "\n" + parent_indent + "}"
            return text[:obj_start] + new_object + text[obj_end + 1 :]
        head = text[: last + 1]
        tail = text[last + 1 : obj_end + 1]  # 原对象尾部的空白 + '}'
        return head + ",\n" + member + tail + text[obj_end + 1 :]

    @staticmethod
    def _line_indent(text: str, index: int) -> str:
        line_start = text.rfind("\n", 0, index) + 1
        prefix = text[line_start:index]
        return prefix[: len(prefix) - len(prefix.lstrip(" \t"))]

    @classmethod
    def _child_indent(cls, text: str, obj_start: int, obj_end: int) -> str:
        """对象里子键用的缩进；看不出来就用"父行缩进 + 两格"。"""

        fallback = cls._line_indent(text, obj_start) + "  "
        newline = text.find("\n", obj_start, obj_end)
        if newline < 0:
            return fallback
        line_start = newline + 1
        line_end = text.find("\n", line_start, obj_end + 1)
        row = text[line_start : line_end if line_end >= 0 else obj_end]
        stripped = row.lstrip(" \t")
        if not stripped or stripped[0] in "}],":
            return fallback
        indent = row[: len(row) - len(stripped)]
        return indent or fallback

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
        self._journal_records.clear()
        db = self.paths.db_file
        if not db or not db.exists():
            result.note(f"未找到数据库，跳过：{db}")
            return result
        if not self.enable_card and not self.activate:
            result.note("供应商卡片和当前路由都已在设置中关闭")
            return result

        if self.enable_card or self.activate:
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

        if self.activate:
            try:
                self._write_routing(backup, result)
            except Exception as exc:  # noqa: BLE001
                self._rollback_current_operation()
                result.fail(f"当前路由写入失败：{exc}")
        else:
            result.note("当前路由激活已关闭，未改 settings.json / live-state.json")
        return result

    def _rollback_current_operation(self) -> None:
        from .ccswitch_restore import restore_journal_record

        for record_path in reversed(self._journal_records):
            record = ChangeJournal.read(record_path)
            if record is not None:
                try:
                    restore_journal_record(record)
                except Exception as exc:  # noqa: BLE001
                    logger.error("CC Switch 跨文件回滚失败：%s", exc)
        self._journal_records.clear()

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
                if not self.enable_card:
                    result.fail("要激活的 DeepSeek (THU) 卡片不存在")
                    return
                settings_config = self._new_settings_config(token, models)
                meta = self._new_meta()
                card_needs_write = True
                is_current = 0
            else:
                raw_settings, raw_meta, name, is_current = row
                settings_config = self._json_object(raw_settings)
                meta = self._json_object(raw_meta)
                if self.enable_card:
                    changed_settings = self._update_settings_config(settings_config, token, models)
                    changed_meta = self._update_meta(meta)
                    card_needs_write = (
                        changed_settings
                        or changed_meta
                        or name != constants.PROVIDER_DISPLAY_NAME
                    )
                else:
                    card_needs_write = False

            other_current = self._other_current_cards(connection) if has_current else []
            activation_needs_write = bool(
                self.activate
                and has_current
                and (row is None or is_current != 1 or other_current)
            )
            refresh_targets = self._refresh_targets(connection, token) if self.enable_card else []

            if self.dry_run:
                if self.enable_card:
                    payload = json.dumps(
                        {
                            "name": constants.PROVIDER_DISPLAY_NAME,
                            "settings_config": settings_config,
                            "meta": meta,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    emit(self, db, payload, "ccswitch-card", "sqlite-json")
                    result.note("（预检）已渲染 CC Switch 卡片内容（未写库）")
                if self.activate:
                    result.note("（预检）将在写库时激活 DeepSeek (THU)（未写库）")
                return

            if not card_needs_write and not activation_needs_write and not refresh_targets:
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
                before_exists = fresh is not None
                if fresh is not None:
                    raw_before_settings, raw_before_meta, before_name, before_is_current = fresh
                    before_settings = self._json_object(raw_before_settings)
                    before_meta = self._json_object(raw_before_meta)
                else:
                    before_settings = {}
                    before_meta = {}
                    before_name = ""
                    before_is_current = False
                if insert_mode:
                    if not self.enable_card:
                        raise RuntimeError("DeepSeek (THU) 卡片在加锁后消失")
                    settings_config = self._new_settings_config(token, models)
                    meta = self._new_meta()
                    card_needs_write = True
                    is_current = 0
                else:
                    raw_settings, raw_meta, name, is_current = fresh
                    settings_config = self._json_object(raw_settings)
                    meta = self._json_object(raw_meta)
                    # 注意：这里必须分开调用，不能写成 a() or b() ——
                    # 短路会让 b() 根本不执行，meta（apiFormat）就会一直漏写。
                    if self.enable_card:
                        changed_settings = self._update_settings_config(settings_config, token, models)
                        changed_meta = self._update_meta(meta)
                        card_needs_write = (
                            changed_settings
                            or changed_meta
                            or name != constants.PROVIDER_DISPLAY_NAME
                        )
                    else:
                        card_needs_write = False
                other_current = self._other_current_cards(connection) if has_current else []
                activation_needs_write = bool(
                    self.activate
                    and has_current
                    and (insert_mode or is_current != 1 or other_current)
                )
                refresh_targets = self._refresh_targets(connection, token) if self.enable_card else []
                if not card_needs_write and not activation_needs_write and not refresh_targets:
                    connection.rollback()
                    result.note("加锁后发现已经是最新，未改动 CC Switch 数据库")
                    return

                current_state_before = []
                sibling_changes: list[dict] = []
                if has_current:
                    current_state_before = [
                        {"id": str(item[0]), "current": bool(item[1])}
                        for item in connection.execute(
                            "SELECT id, is_current FROM providers WHERE app_type = ?",
                            (constants.CCSWITCH_APP_TYPE,),
                        ).fetchall()
                    ]
                for payload, provider_id, app_type in refresh_targets:
                    old_row = connection.execute(
                        "SELECT settings_config FROM providers WHERE id = ? AND app_type = ?",
                        (provider_id, app_type),
                    ).fetchone()
                    sibling_changes.append(
                        {
                            "id": str(provider_id),
                            "app_type": str(app_type),
                            "before": self._json_object(old_row[0]) if old_row else {},
                            "after": self._json_object(payload),
                        }
                    )

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
                        insert_columns.append("is_current")
                        placeholders.append("?")
                        insert_values.append(1 if self.activate else 0)
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
                    connection.execute(
                        f"UPDATE providers SET {', '.join(assignments)} WHERE id = ? AND app_type = ?",
                        (*values, constants.CCSWITCH_PROVIDER_ID, constants.CCSWITCH_APP_TYPE),
                    )
                    result.note("已更新供应商卡片 DeepSeek (THU)")

                if activation_needs_write and has_current:
                    connection.execute(
                        "UPDATE providers SET is_current = 1 WHERE id = ? AND app_type = ?",
                        (constants.CCSWITCH_PROVIDER_ID, constants.CCSWITCH_APP_TYPE),
                    )
                    connection.execute(
                        "UPDATE providers SET is_current = 0 WHERE app_type = ? AND id <> ?",
                        (constants.CCSWITCH_APP_TYPE, constants.CCSWITCH_PROVIDER_ID),
                    )

                for payload, provider_id, app_type in refresh_targets:
                    connection.execute(
                        "UPDATE providers SET settings_config = ? WHERE id = ? AND app_type = ?",
                        (payload, provider_id, app_type),
                    )

                # 提交前读回校验：确认写进这张卡片的就是我们要写的内容。
                # 有出入就抛异常 → 外层 rollback，数据库保持原样，
                # 而不是"先提交、之后再发现写坏了"。
                check = connection.execute(
                    "SELECT settings_config, meta FROM providers WHERE id = ? AND app_type = ?",
                    (constants.CCSWITCH_PROVIDER_ID, constants.CCSWITCH_APP_TYPE),
                ).fetchone()
                if check is None:
                    raise RuntimeError("写入后读不到 DeepSeek (THU) 卡片")
                if self.enable_card:
                    check_settings = self._json_object(check[0])
                    check_auth = check_settings.get("auth")
                    check_key = (
                        check_auth.get("OPENAI_API_KEY") if isinstance(check_auth, dict) else None
                    )
                    if check_key != token:
                        raise RuntimeError("写入后卡片里的 Token 与预期不一致，已回滚")
                    if self._json_object(check[1]).get("apiFormat") != constants.CCSWITCH_API_FORMAT:
                        raise RuntimeError("写入后卡片的 apiFormat 与预期不一致，已回滚")

                connection.commit()
            except Exception:
                connection.rollback()
                raise

            result.changed = True
            current_state_after = current_state_before
            after_current = self.activate
            if has_current:
                current_state_after = [
                    {"id": str(item[0]), "current": bool(item[1])}
                    for item in connection.execute(
                        "SELECT id, is_current FROM providers WHERE app_type = ?",
                        (constants.CCSWITCH_APP_TYPE,),
                    ).fetchall()
                ]
            journal_path = ChangeJournal.for_backup(backup).record(
                "ccswitch_card",
                path=str(db),
                before_exists=before_exists,
                before_settings=_plain(before_settings),
                before_meta=_plain(before_meta),
                before_name=before_name,
                before_is_current=bool(before_is_current),
                after_settings=_plain(settings_config),
                after_meta=_plain(meta),
                after_name=constants.PROVIDER_DISPLAY_NAME,
                after_is_current=after_current,
                current_state_before=current_state_before,
                current_state_after=current_state_after,
                activation_changed=activation_needs_write,
                card_changed=card_needs_write,
                sibling_changes=sibling_changes,
            )
            if journal_path is not None:
                self._journal_records.append(journal_path)
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

    def _write_routing(self, backup: BackupManager, result: ApplyResult) -> None:
        settings_path = self.paths.settings_file
        if settings_path and settings_path.exists():
            data = _load_json(settings_path)
            key = f"currentProvider{constants.CCSWITCH_APP_TYPE.capitalize()}"
            previous = data.get(key)
            previous_present = key in data
            if previous != constants.CCSWITCH_PROVIDER_ID:
                data[key] = constants.CCSWITCH_PROVIDER_ID
                if self._write_json(
                    settings_path,
                    data,
                    backup,
                    "ccswitch-settings",
                    patch=(key, previous),
                    insert=((), key, constants.CCSWITCH_PROVIDER_ID),
                ):
                    result.changed = True
                    result.note(f"已把 settings.json 的 {key} 指向 DeepSeek (THU)")
                    journal_path = ChangeJournal.for_backup(backup).record(
                        "ccswitch_routing",
                        path=str(settings_path),
                        json_path=[key],
                        before_present=previous_present,
                        before_value=previous,
                        after_value=constants.CCSWITCH_PROVIDER_ID,
                    )
                    if journal_path is not None:
                        self._journal_records.append(journal_path)
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
                previous_route_present = "proxy_route" in app_state
                app_state["proxy_route"] = constants.CCSWITCH_PROVIDER_ID
                if self._write_json(
                    live_path,
                    data,
                    backup,
                    "ccswitch-live-state",
                    patch=("proxy_route", previous_route),
                    insert=(
                        ("apps", constants.CCSWITCH_APP_TYPE),
                        "proxy_route",
                        constants.CCSWITCH_PROVIDER_ID,
                    ),
                ):
                    result.changed = True
                    result.note("已把 live-state.json 的代理路由指向 DeepSeek (THU)")
                    journal_path = ChangeJournal.for_backup(backup).record(
                        "ccswitch_routing",
                        path=str(live_path),
                        json_path=["apps", constants.CCSWITCH_APP_TYPE, "proxy_route"],
                        before_present=previous_route_present,
                        before_value=previous_route,
                        after_value=constants.CCSWITCH_PROVIDER_ID,
                    )
                    if journal_path is not None:
                        self._journal_records.append(journal_path)
                if "contract" in app_state:
                    result.note("注：live-state.json 里的 contract 未改动，CC Switch 下次启动会自行校准")
            else:
                result.note("live-state.json 的代理路由已指向 DeepSeek (THU)")
        else:
            result.note(f"未找到 live-state.json，跳过：{live_path}")

