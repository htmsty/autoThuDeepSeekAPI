"""运行状态：最近一次 Token、时间戳（存在 private/state.json）。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from . import paths

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    last_token: str = ""
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
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        path = paths.state_file()
        paths.write_text_atomic(path, json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n")

    def remember_token(self, token: str) -> None:
        self.last_token = token
        self.last_token_at = datetime.now().isoformat(timespec="seconds")
        self.save()

    def remember_status(self, status: str) -> None:
        self.last_status = status
        self.save()

