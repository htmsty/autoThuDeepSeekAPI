"""全局常量：应用信息、站点地址、目标标识与默认模型。"""

from __future__ import annotations

APP_NAME = "autoThuDeepSeekAPI"
APP_DISPLAY_NAME = "Auto THU DeepSeek API"
APP_AUTHOR = "htmsty"
APP_VERSION = "0.1.0"
REPO_URL = "https://github.com/htmsty/autoThuDeepSeekAPI"

# —— 清华 MadModel ——
MADMODEL_ORIGIN = "https://madmodel.cs.tsinghua.edu.cn"
MADMODEL_HOST = "madmodel.cs.tsinghua.edu.cn"
MADMODEL_API_BASE = f"{MADMODEL_ORIGIN}/v1"
MADMODEL_LOGIN_URL = f"{MADMODEL_ORIGIN}/"
MADMODEL_AUTH_LOGIN_PATH = "/model-api/auth-login"

# —— DSH ——
DSH_DIR_NAME = ".dsh"
DSH_CREDENTIAL_FILE = ".credentials.yaml"
DSH_CREDENTIAL_KEY = "MADMODEL_API_KEY"
DSH_PROFILES_DIR = "profiles"
DSH_PATCH_FILE = "cordis.patch.yml"
DSH_LLM_ENTRY_ID = "llm-pi-ai"
DSH_LLM_ENTRY_NAME = "@deepseek-ai/dsh-llm-pi-ai"
DSH_DEFAULT_MODEL_ENTRY_ID = "agent-default-model"
DSH_DEFAULT_MODEL_ENTRY_NAME = "@deepseek-ai/dsh-agent-default-model"
DSH_PROVIDER_KEY = "madmodel"

# —— CC Switch ——
CCSWITCH_DIR_NAME = ".cc-switch"
CCSWITCH_DB_FILE = "cc-switch.db"
CCSWITCH_SETTINGS_FILE = "settings.json"
CCSWITCH_LIVE_STATE_FILE = "live-state.json"
CCSWITCH_APP_TYPE = "codex"
CCSWITCH_PROVIDER_ID = "deepseek-thu"
CCSWITCH_PROCESS_NAME = "cc-switch"
CCSWITCH_API_FORMAT = "openai_chat"

# —— 共享标识 ——
PROVIDER_DISPLAY_NAME = "DeepSeek (THU)"

# 默认同步间隔（分钟）。站点会话约 45 分钟后可能失效、Token 约 5 小时到期，
# 20 分钟是"来得来得及、又不至于太频繁"的折中，用户可自行修改。
DEFAULT_SYNC_INTERVAL_MINUTES = 20

# 站点模型列表读不到时的兜底（2026-10 实测值）。
DEFAULT_MODELS: tuple[dict[str, object], ...] = (
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
)

# 注册表开机启动项名。
STARTUP_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = APP_NAME

