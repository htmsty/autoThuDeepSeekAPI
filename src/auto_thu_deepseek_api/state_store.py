"""运行状态：最近一次 Token、时间戳（存在 private/state.json）。"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from . import crypto, paths

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    # 只在内存中保存明文。写盘时一定会转成 DPAPI 密文。
    last_token: str = field(default="", repr=False)
    last_token_encrypted: str = field(default="", repr=False)
    last_token_at: str = ""
    last_status: str = ""
    # 启动提醒的"已处理"状态（按用户要求写在 private 里，处理过就不再提醒）
    desktop_note_ack: bool = False  # 程序在桌面根目录的提醒
    targets_note_ack: bool = False  # DSH / CC Switch 都没检出的提醒

    @classmethod
    def load(cls) -> "AppState":
        path = paths.state_file()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            # 合法 JSON 但不是对象：按"没有状态"处理，别让加载过程抛异常
            logger.warning("状态文件结构不是 JSON 对象，按默认状态处理：%s", path)
            return cls()
        known = {field for field in cls.__dataclass_fields__}
        state = cls(**{k: v for k, v in raw.items() if k in known})
        encrypted = raw.get("last_token_encrypted")
        if isinstance(encrypted, str) and encrypted:
            try:
                state.last_token = crypto.unprotect_text(
                    base64.b64decode(encrypted, validate=True),
                    require_encrypted=True,
                )
            except (ValueError, OSError) as exc:
                logger.warning("无法解密已保存的 Token，将要求重新登录：%s", exc)
                state.last_token = ""
        legacy = raw.get("last_token")
        if isinstance(legacy, str) and legacy:
            state.last_token = legacy
        if isinstance(legacy, str) and legacy:
            # 旧版把 Token 明文写在 state.json。读取后立即原子重写，
            # 新文件只包含 DPAPI 密文。
            try:
                state.save()
                logger.info("已把明文保存的 Token 迁移为 DPAPI 密文")
            except OSError as exc:
                logger.warning("迁移明文 Token 失败：%s", exc)
        return state

    def save(self) -> None:
        path = paths.state_file()
        payload = {
            "last_token_at": self.last_token_at,
            "last_status": self.last_status,
            "desktop_note_ack": self.desktop_note_ack,
            "targets_note_ack": self.targets_note_ack,
            "last_token_encrypted": "",
        }
        if self.last_token:
            try:
                encrypted = crypto.protect_text(self.last_token)
            except OSError as exc:
                # 不能因为 DPAPI 故障退回明文。Token 仍可在本次运行中使用，
                # 但磁盘上只留下空值，下次需要重新登录。
                logger.error("Token 加密失败，本次不写盘：%s", exc)
            else:
                payload["last_token_encrypted"] = base64.b64encode(encrypted).decode("ascii")
        paths.write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def remember_token(self, token: str) -> None:
        self.last_token = token
        self.last_token_at = datetime.now().isoformat(timespec="seconds")
        self.save()

    def remember_status(self, status: str) -> None:
        self.last_status = status
        self.save()

