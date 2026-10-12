"""写入前预检：校验"将要写进模型文件的内容"是否合法。

做法是让目标以**干跑**模式跑一遍写入流程：渲染代码和真正写盘时完全一样，
只是不备份、不落盘。渲染出来的文本再逐份做结构校验（YAML / JSON 能不能解析、
字段齐不齐、模型 id 与站点清单是否一致、Token 是不是同一个）。

预检不通过时，同步会跳过对应目标的写入——宁可这次不写，也不把坏内容写进去。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from . import constants, paths
from .backup import BackupManager
from .settings import AppSettings
from .targets.base import RenderedFile
from .token import site_models
from .token.jwt_utils import looks_like_jwt

logger = logging.getLogger(__name__)

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,63}$")


@dataclass
class CheckItem:
    """一条预检结论。target_key 用于决定"这条不通过时跳过哪个目标"。"""

    target_key: str
    name: str
    ok: bool
    detail: str


@dataclass
class PreflightReport:
    items: list[CheckItem] = field(default_factory=list)
    models: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.items)

    def failed(self) -> list[CheckItem]:
        return [item for item in self.items if not item.ok]

    def blocked_targets(self) -> set[str]:
        return {item.target_key for item in self.failed() if item.target_key in ("dsh", "ccswitch")}

    def text(self) -> str:
        lines = []
        for item in self.items:
            mark = "✓" if item.ok else "✗"
            lines.append(f"{mark} [{item.name}] {item.detail}")
        if self.models:
            lines.append("模型清单：" + "、".join(str(m.get("id")) for m in self.models))
        if self.ok:
            lines.insert(0, "预检通过：将要写入的内容都合法。")
        else:
            lines.insert(0, f"预检发现问题 {len(self.failed())} 处（对应目标这次不会写入）：")
        return "\n".join(lines)


# —— 各类内容的校验 ——

def _check_credentials(text: str, token: str) -> tuple[bool, str]:
    from ruamel.yaml import YAML

    try:
        data = YAML(typ="safe").load(text)
    except Exception as exc:  # noqa: BLE001
        return False, f"YAML 解析失败：{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return False, "顶层不是映射"
    refs = data.get("refs")
    if not isinstance(refs, dict):
        return False, "缺少 refs 映射"
    value = refs.get(constants.DSH_CREDENTIAL_KEY)
    if not isinstance(value, str) or not value:
        return False, f"refs.{constants.DSH_CREDENTIAL_KEY} 缺失或为空"
    if value != token:
        return False, "写入的 Token 与本次拿到的 Token 不一致"
    return True, f"refs.{constants.DSH_CREDENTIAL_KEY} 已就位（长度 {len(value)}）"


def _check_profile(text: str) -> tuple[bool, str]:
    from ruamel.yaml import YAML

    try:
        data = YAML(typ="safe").load(text)
    except Exception as exc:  # noqa: BLE001
        return False, f"YAML 解析失败：{type(exc).__name__}: {exc}"
    if not isinstance(data, list):
        return False, "顶层不是数组"

    entries = {entry.get("id"): entry for entry in data if isinstance(entry, dict)}
    llm = entries.get(constants.DSH_LLM_ENTRY_ID)
    providers = ((llm or {}).get("config") or {}).get("providers") if isinstance(llm, dict) else None
    if not isinstance(providers, dict) or not providers:
        return False, "找不到 llm-pi-ai.config.providers"

    ours = [
        key
        for key, value in providers.items()
        if isinstance(value, dict) and value.get("apiKeyEnv") == constants.DSH_CREDENTIAL_KEY
    ]
    if not ours:
        return False, f"没有 provider 使用 apiKeyEnv={constants.DSH_CREDENTIAL_KEY}"
    provider_key = ours[0]
    provider = providers[provider_key]

    problems: list[str] = []
    if not provider.get("displayName"):
        problems.append("displayName 为空")
    if not provider.get("baseURL"):
        problems.append("baseURL 为空")
    if not provider.get("api"):
        problems.append("api 为空")

    model_ids: list[str] = []
    models = provider.get("models")
    if not isinstance(models, list) or not models:
        problems.append("models 为空")
    else:
        for model in models:
            if not isinstance(model, dict):
                problems.append("models 里有非映射条目")
                continue
            model_id = model.get("id")
            if not isinstance(model_id, str) or not MODEL_ID_RE.match(model_id):
                problems.append(f"模型 id 不合法：{model_id!r}")
                continue
            model_ids.append(model_id)
            if not model.get("name"):
                problems.append(f"{model_id} 缺 name")
            window = model.get("contextWindow")
            if not isinstance(window, int) or window <= 0:
                problems.append(f"{model_id} 的 contextWindow 不合法：{window!r}")
            modalities = model.get("input")
            if not isinstance(modalities, list) or not modalities:
                problems.append(f"{model_id} 的 input 不合法")
            efforts = model.get("reasoningEfforts")
            if efforts is not None:
                if not isinstance(efforts, dict) or any(
                    value not in constants.THINKING_LEVELS for value in efforts.values()
                ):
                    problems.append(f"{model_id} 的 reasoningEfforts 不合法")
        if len(model_ids) != len(set(model_ids)):
            problems.append("模型 id 有重复")

    default_entry = entries.get(constants.DSH_DEFAULT_MODEL_ENTRY_ID)
    default_config = (default_entry or {}).get("config") if isinstance(default_entry, dict) else None
    if isinstance(default_config, dict) and default_config:
        provider_of_default = default_config.get("provider")
        if provider_of_default not in providers:
            problems.append(f"默认模型指向的 provider 不存在：{provider_of_default!r}")
        elif provider_of_default == provider_key and default_config.get("model") not in model_ids:
            problems.append(f"默认模型的 model 不在清单里：{default_config.get('model')!r}")

    if problems:
        return False, "；".join(problems)
    return True, f"provider {provider_key}，{len(model_ids)} 个模型，字段齐全"


def _check_routing_json(text: str) -> tuple[bool, str]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        return False, f"JSON 解析失败：{exc}"
    if not isinstance(data, dict):
        return False, "顶层不是对象"
    if "currentProviderCodex" in data:
        if data["currentProviderCodex"] != constants.CCSWITCH_PROVIDER_ID:
            return False, f"currentProviderCodex={data['currentProviderCodex']!r}"
        return True, f"currentProviderCodex={constants.CCSWITCH_PROVIDER_ID}"
    apps = data.get("apps")
    codex = apps.get(constants.CCSWITCH_APP_TYPE) if isinstance(apps, dict) else None
    if not isinstance(codex, dict):
        return False, "缺 apps.codex"
    if codex.get("proxy_route") != constants.CCSWITCH_PROVIDER_ID:
        return False, f"proxy_route={codex.get('proxy_route')!r}"
    return True, f"proxy_route={constants.CCSWITCH_PROVIDER_ID}"


def _check_ccswitch_card(text: str, token: str, expected_ids: list[str]) -> tuple[bool, str]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        return False, f"JSON 解析失败：{exc}"
    settings = data.get("settings_config") if isinstance(data, dict) else None
    meta = data.get("meta") if isinstance(data, dict) else None
    if not isinstance(settings, dict) or not isinstance(meta, dict):
        return False, "缺 settings_config / meta"

    problems: list[str] = []
    key = (settings.get("auth") or {}).get("OPENAI_API_KEY")
    if not isinstance(key, str) or not key:
        problems.append("auth.OPENAI_API_KEY 缺失")
    elif key != token:
        problems.append("卡片里的 Token 与本次拿到的 Token 不一致")
    if meta.get("apiFormat") != constants.CCSWITCH_API_FORMAT:
        problems.append(f"meta.apiFormat={meta.get('apiFormat')!r}")
    if not isinstance(settings.get("config"), str) or "base_url" not in str(settings.get("config")):
        problems.append("config（Codex TOML）缺 base_url")
    catalog = (settings.get("modelCatalog") or {}).get("models")
    if isinstance(catalog, list) and catalog:
        catalog_ids = [str(item.get("model")) for item in catalog if isinstance(item, dict)]
        if expected_ids and catalog_ids != expected_ids:
            problems.append(f"模型目录与站点清单不一致：{catalog_ids}")
    if problems:
        return False, "；".join(problems)
    return True, f"卡片内容完整，模型目录 {len(catalog or [])} 项"


def _validate_rendered(rendered: RenderedFile, token: str, expected_ids: list[str]) -> CheckItem:
    name = rendered.path.name
    if rendered.kind == "yaml":
        if name == constants.DSH_CREDENTIAL_FILE:
            ok, detail = _check_credentials(rendered.text, token)
            return CheckItem("dsh", f"DSH 凭据 {name}", ok, detail)
        ok, detail = _check_profile(rendered.text)
        return CheckItem("dsh", f"DSH profile {rendered.path.parent.name}/{name}", ok, detail)
    if rendered.kind == "json":
        ok, detail = _check_routing_json(rendered.text)
        return CheckItem("ccswitch", f"CC Switch {name}", ok, detail)
    if rendered.kind == "sqlite-json":
        ok, detail = _check_ccswitch_card(rendered.text, token, expected_ids)
        return CheckItem("ccswitch", "CC Switch 供应商卡片", ok, detail)
    return CheckItem("", name, True, "无需校验的文本")


def _check_models(models: list[dict]) -> CheckItem:
    problems: list[str] = []
    ids: list[str] = []
    for model in models:
        model_id = model.get("id")
        if not isinstance(model_id, str) or not MODEL_ID_RE.match(model_id):
            problems.append(f"模型 id 不合法：{model_id!r}")
            continue
        ids.append(model_id)
        window = model.get("contextWindow")
        if not isinstance(window, int) or window <= 0:
            problems.append(f"{model_id} 的 contextWindow 不合法")
        if not isinstance(model.get("input"), list) or not model.get("input"):
            problems.append(f"{model_id} 的 input 不合法")
    if len(ids) != len(set(ids)):
        problems.append("模型 id 有重复")
    if problems:
        return CheckItem("models", "模型清单", False, "；".join(problems))
    return CheckItem("models", "模型清单", True, "、".join(ids))


def run_preflight(settings: AppSettings, token: str, models: list[dict] | None = None) -> PreflightReport:
    """跑一遍预检。只读：不改任何目标文件。"""

    from .sync import build_targets  # 延迟导入，避免模块循环

    report = PreflightReport()

    if not looks_like_jwt(token):
        report.items.append(CheckItem("token", "Token", False, "拿到的不是一个 JWT"))
    else:
        report.items.append(CheckItem("token", "Token", True, f"JWT 格式正确（长度 {len(token)}）"))

    report.models = models if models is not None else site_models.resolve_models(token)
    report.items.append(_check_models(report.models))
    expected_ids = [str(model.get("id")) for model in report.models if model.get("id")]

    backup = BackupManager(paths.backups_dir(), keep=settings.backup_keep)
    for target in build_targets(settings):
        status = target.detect()
        if not status.detected:
            report.items.append(CheckItem(target.key, target.label, True, f"未安装，跳过（{status.detail}）"))
            continue
        target.dry_run = True
        try:
            result = target.apply(token, report.models, backup)
        except Exception as exc:  # noqa: BLE001
            report.items.append(CheckItem(target.key, target.label, False, f"渲染时异常：{exc}"))
            continue

        for error in result.errors:
            report.items.append(CheckItem(target.key, target.label, False, error))
        for rendered in target.rendered:
            report.items.append(_validate_rendered(rendered, token, expected_ids))
        if not result.errors and not target.rendered:
            report.items.append(
                CheckItem(target.key, target.label, True, "已经是最新，本次不会写入任何内容")
            )
    return report
