"""全局常量：应用信息、站点地址、目标标识与默认模型。"""

from __future__ import annotations

APP_NAME = "autoThuDeepSeekAPI"
APP_DISPLAY_NAME = "Auto THU DeepSeek API"
APP_AUTHOR = "htmsty"
APP_VERSION = "2.5.1"
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
# 180 分钟是"来得来得及、又不至于太频繁"的折中，用户可自行修改。
DEFAULT_SYNC_INTERVAL_MINUTES = 180

# 站点 Token 的有效期（小时）。只在 Token 自身没带 exp 时用来估算到期时刻。
TOKEN_LIFETIME_HOURS = 5

# 站点模型列表读不到时的兜底（2026-10 实测值）。
DEFAULT_MODELS: tuple[dict[str, object], ...] = (
    {
        "id": "DeepSeek-V4.1-Flash",
        "name": "DeepSeek-V4.1-Flash (THU)",
        "contextWindow": 1000000,
        "input": ["text", "image"],
        "reasoningEfforts": {"low": "low", "high": "high", "max": "max"},
    },
    {
        "id": "qwen3.8-27b",
        "name": "qwen3.8-27b (THU)",
        "contextWindow": 1000000,
        "input": ["text", "image"],
        "reasoningEfforts": {"low": "low", "medium": "medium", "xhigh": "xhigh"},
    },
)

# 站点有、但我们故意不注册的模型（与 thu-tok-auto 保持一致）。
EXCLUDED_MODEL_IDS = (
    "DeepSeek-V4-Flash-Vision-Exp",  # V4.1-Flash 已经覆盖图片输入
    "DeepSeek-R1-W8A8",
)

# 站点的思考档位取值；只有这些才写进 provider。
THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")

# 站点前端包里没标 supportImage、但我们实测支持图片的模型。
IMAGE_CONFIRMED = ("DeepSeek-V4.1-Flash",)

# 所有模型统一用这个上下文长度：模型侧 1,048,576 通过、网关 8 MiB 请求体通过，
# 留约 4.8 万 token 给系统提示与工具定义（thu-tok-auto 2026-10-01 实测结论）。
CONTEXT_WINDOW = 1000000

# 站点前端包有几 MB，读一次缓存 6 小时。
MODEL_CACHE_TTL_SECONDS = 6 * 3600

# 注册表开机启动项名。
STARTUP_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = APP_NAME

