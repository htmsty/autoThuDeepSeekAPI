"""在临时目录里搭一套假的 DSH / CC Switch 配置，用来验证写入与还原。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

FAKE_OLD_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJjb2RlIjoiMCIsImlhdCI6MTc5MTczMzAxNiwiZXhwIjoxNzkxODE5NDE2fQ."
    "JOz4GCKb-zn4YmPxwDHw-Zne6DXu_OCq_IYjBrmkSsc"
)
FAKE_NEW_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJjb2RlIjoiMCIsImlhdCI6MTc5MTc5NjU0MSwiZXhwIjoxNzkxODgyOTQxfQ."
    "T9YmGc_4DL7X_DB7uN8DWY00UuhkLOwlAP9MfkYuXkc"
)

CREDENTIALS = f"""version: 1
records:
  client-connection/browser-session:
    kind: grant
    payload:
      version: 1
      secret: sXi1FWkuO-BGtyZMBAMVEee0-41RjAcJyxS8wjUSh3M
refs:
  DEEPSEEK_API_KEY: {FAKE_OLD_TOKEN}
  MADMODEL_API_KEY: {FAKE_OLD_TOKEN}
  TSINGHUA_API_KEY: {FAKE_OLD_TOKEN}
"""

PROFILE_DESKTOP = f"""# Your patch layer for this dsh profile
- id: ui-chat
  name: "@deepseek-ai/dsh-client-ui-chat"
  config:
    transcriptView: standard
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      madmodel:
        displayName: DeepSeek (THU)
        apiKeyEnv: MADMODEL_API_KEY
        api: openai-completions
        reasoning: medium
        baseURL: https://madmodel.cs.tsinghua.edu.cn/v1
        models:
          - id: DeepSeek-V4.1-Flash
            name: DeepSeek-V4.1-Flash (THU)
            contextWindow: 1000000
            input:
              - text
              - image
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: madmodel
    model: DeepSeek-V4.1-Flash
"""

PROFILE_WEB = """# Your patch layer for this dsh profile
- id: ui-settings-general
  name: "@deepseek-ai/dsh-client-ui-settings-general"
  config:
    welcomeNoticeVersion: 2026-09-28.1
"""

CCSWITCH_SETTINGS = {
    "showInTray": True,
    "currentProviderCodex": "codex-official",
    "enableLocalProxy": True,
}

CCSWITCH_LIVE_STATE = {
    "version": 1,
    "apps": {
        "claude": {"mode": "direct"},
        "codex": {
            "mode": "proxy",
            "attached": True,
            "proxy_route": "some-other",
            "contract": {"version": 1, "key": "deadbeef"},
        },
    },
}


@dataclass
class Sandbox:
    root: Path
    dsh_dir: Path
    ccswitch_dir: Path
    backups_dir: Path

    @classmethod
    def create(cls, root: Path) -> "Sandbox":
        dsh_dir = root / ".dsh"
        profiles = dsh_dir / "profiles"
        (profiles / "desktop").mkdir(parents=True)
        (profiles / "web").mkdir(parents=True)
        (dsh_dir / ".credentials.yaml").write_text(CREDENTIALS, encoding="utf-8")
        (profiles / "desktop" / "cordis.patch.yml").write_text(PROFILE_DESKTOP, encoding="utf-8")
        (profiles / "web" / "cordis.patch.yml").write_text(PROFILE_WEB, encoding="utf-8")

        ccswitch_dir = root / ".cc-switch"
        ccswitch_dir.mkdir(parents=True)
        (ccswitch_dir / "settings.json").write_text(
            json.dumps(CCSWITCH_SETTINGS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (ccswitch_dir / "live-state.json").write_text(
            json.dumps(CCSWITCH_LIVE_STATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        cls._create_database(ccswitch_dir / "cc-switch.db")

        backups_dir = root / "private" / "backups"
        backups_dir.mkdir(parents=True)
        return cls(root=root, dsh_dir=dsh_dir, ccswitch_dir=ccswitch_dir, backups_dir=backups_dir)

    @staticmethod
    def _create_database(path: Path) -> None:
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
                notes TEXT,
                icon TEXT,
                icon_color TEXT,
                meta TEXT NOT NULL DEFAULT '{}',
                is_current BOOLEAN NOT NULL DEFAULT 0,
                in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
                cost_multiplier TEXT NOT NULL DEFAULT '1.0',
                limit_daily_usd TEXT,
                limit_monthly_usd TEXT,
                provider_type TEXT,
                PRIMARY KEY (id, app_type)
            );
            """
        )
        official = {
            "auth": {"OPENAI_API_KEY": None},
            "config": 'model_provider = "openai"\n',
        }
        thu_settings = {
            "auth": {"OPENAI_API_KEY": FAKE_OLD_TOKEN},
            "config": (
                'model_provider = "custom"\n'
                'model = "DeepSeek-V4.1-Flash"\n'
                "disable_response_storage = true\n"
                "\n"
                "[model_providers.custom]\n"
                'name = "DeepSeek (THU)"\n'
                'base_url = "https://madmodel.cs.tsinghua.edu.cn/v1"\n'
                'wire_api = "responses"\n'
                "requires_openai_auth = true\n"
            ),
            "modelCatalog": {"models": [{"model": "DeepSeek-V4.1-Flash"}]},
        }
        legacy_settings = {
            "auth": {"OPENAI_API_KEY": FAKE_OLD_TOKEN},
            "config": (
                'model_provider = "tsinghua"\n\n'
                "[model_providers.tsinghua]\n"
                'base_url = "https://madmodel.cs.tsinghua.edu.cn/v1/chat/completions"\n'
                'wire_api = "chat"\n'
            ),
        }
        rows = [
            ("codex-official", "codex", "OpenAI Official", official, "{}", 0),
            (
                "deepseek-thu",
                "codex",
                "DeepSeek (THU)",
                thu_settings,
                json.dumps({"commonConfigEnabled": True, "endpointAutoSelect": True}),
                1,
            ),
            ("madmodel-legacy", "claude", "MadModel 旧卡片", legacy_settings, "{}", 0),
        ]
        for provider_id, app_type, name, settings, meta, is_current in rows:
            connection.execute(
                "INSERT INTO providers (id, app_type, name, settings_config, category, created_at,"
                " sort_index, meta, is_current) VALUES (?, ?, ?, ?, 'custom', 1, 0, ?, ?)",
                (provider_id, app_type, name, json.dumps(settings, ensure_ascii=False), meta, is_current),
            )
        connection.commit()
        connection.close()

