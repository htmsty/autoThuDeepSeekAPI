"""运行状态：最近一次 Token、时间戳（存在 private/state.json）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime

from . import paths


@dataclass
class AppState:
    last_token: str = ""
    last_token_at: str = ""
    last_verified_at: str = ""
    last_status: str = ""

    @classmethod
    def load(cls) -> "AppState":
        path = paths.state_file()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
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

