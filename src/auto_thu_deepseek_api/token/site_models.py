"""模型列表跟着站点走。

站点没有模型列表接口（`/v1/models` 会落回 SPA），所以列表只能从站点自己的前端包里读：
站点首页 → 找到 `assets/js/*.js` → 下载这个包 → 解析里面的 `modelList:[{...}]`。
读到的结果转成 provider 的 models 形状，缓存 6 小时；读不到就回退内置名单。
做法与 thu-tok-auto（DSH 插件）一致，字段含义见那边的 extractModelList / toProviderModels。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import constants, paths
from .verifier import fetch_models

logger = logging.getLogger(__name__)

PAGE_MAX_BYTES = 2 * 1024 * 1024
BUNDLE_MAX_BYTES = 8 * 1024 * 1024
REQUEST_TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) autoThuDeepSeekAPI"

ASSET_REF = re.compile(r"""["']([^"']*assets/js/[^"']+\.js)["']""")
SUPPORT_IMAGE = re.compile(r"supportImage:!0|supportImage:true")
EFFORT_OPTIONS = re.compile(r"effortOptions:\[([^\]]*)\]")


# —— 解析前端包 ——

def _slice_bracket(js: str, start: int) -> str | None:
    """从 start（指向 '['）开始，按括号配对取出一段，字符串里的括号不算。"""

    if start < 0:
        return None
    depth = 0
    quote = ""
    index = start
    while index < len(js):
        char = js[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return js[start + 1 : index]
        index += 1
    return None


def _split_top_level_objects(body: str) -> list[str]:
    objects: list[str] = []
    depth = 0
    current = ""
    quote = ""
    index = 0
    while index < len(body):
        char = body[index]
        if quote:
            current += char
            if char == "\\":
                index += 1
                current += body[index] if index < len(body) else ""
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in "\"'":
            quote = char
            current += char
        elif char == "{":
            depth += 1
            current += char
        elif char == "}":
            depth -= 1
            current += char
            if depth == 0:
                objects.append(current)
                current = ""
        elif depth > 0:
            current += char
        index += 1
    return objects


def _field_string(entry: str, key: str) -> str:
    match = re.search(rf'{re.escape(key)}:"([^"]*)"', entry)
    return match.group(1) if match else ""


def extract_model_list(js: str) -> list[dict] | None:
    """从站点前端包里读出 `modelList:[{label,value,supportImage,effortOptions}, …]`。"""

    if not isinstance(js, str) or not js:
        return None
    at = js.find("modelList:[")
    if at < 0:
        return None
    body = _slice_bracket(js, js.find("[", at))
    if not body:
        return None

    models: list[dict] = []
    for entry in _split_top_level_objects(body):
        model_id = _field_string(entry, "value")
        if not model_id:
            continue
        effort_match = EFFORT_OPTIONS.search(entry)
        efforts = []
        if effort_match:
            efforts = [
                item.strip().strip("\"'")
                for item in effort_match.group(1).split(",")
                if item.strip().strip("\"'")
            ]
        models.append(
            {
                "id": model_id,
                "label": _field_string(entry, "label") or model_id,
                "supportImage": bool(SUPPORT_IMAGE.search(entry)),
                "efforts": efforts,
            }
        )
    return models or None


def to_provider_models(site_models: list[dict] | None) -> list[dict] | None:
    """转成 llm-pi-ai 的 provider.models 形状（排除名单见 constants.EXCLUDED_MODEL_IDS）。"""

    if not isinstance(site_models, list) or not site_models:
        return None
    kept = [
        model
        for model in site_models
        if model
        and model.get("id")
        and model["id"] not in constants.EXCLUDED_MODEL_IDS
    ]
    if not kept:
        return None

    result: list[dict] = []
    for model in kept:
        model_id = str(model["id"])
        efforts = {
            level: level
            for level in (model.get("efforts") or [])
            if level in constants.THINKING_LEVELS
        }
        entry: dict = {
            "id": model_id,
            "name": f"{model.get('label') or model_id} (THU)",
            "contextWindow": constants.CONTEXT_WINDOW,
            "input": (
                ["text", "image"]
                if model.get("supportImage") or model_id in constants.IMAGE_CONFIRMED
                else ["text"]
            ),
        }
        if efforts:
            entry["reasoningEfforts"] = efforts
        result.append(entry)
    return result


# —— 取前端包 ——

def _get_text(url: str, max_bytes: int, timeout: int = REQUEST_TIMEOUT) -> str | None:
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "text/html,application/javascript,*/*")
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            if response.status != 200:
                return None
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        logger.info("读取 %s 返回 HTTP %s", url, exc.code)
        return None
    except Exception as exc:  # noqa: BLE001 - 网络异常种类多
        logger.info("读取 %s 失败：%s", url, exc)
        return None
    if len(raw) > max_bytes:
        logger.info("%s 超过 %d 字节上限，放弃", url, max_bytes)
        return None
    return raw.decode("utf-8", "replace")


def fetch_site_models() -> list[dict] | None:
    """按 thu-tok-auto 的做法从站点前端包读模型列表；读不到返回 None。"""

    page = _get_text(f"{constants.MADMODEL_ORIGIN}/", PAGE_MAX_BYTES)
    if not page:
        return None
    match = ASSET_REF.search(page)
    if not match:
        logger.info("站点首页里没找到 assets/js 包引用")
        return None

    path = match.group(1)
    if path.startswith("http"):
        url = path
    else:
        url = constants.MADMODEL_ORIGIN + ("" if path.startswith("/") else "/") + path
    bundle = _get_text(url, BUNDLE_MAX_BYTES)
    if not bundle:
        return None

    models = to_provider_models(extract_model_list(bundle))
    if models:
        logger.info("已从站点前端包读到 %d 个模型：%s", len(models), [m["id"] for m in models])
    return models


# —— 缓存 ——

def _cache_file() -> Path:
    return paths.data_root() / "model-cache.json"


def _load_cache() -> list[dict] | None:
    path = _cache_file()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or time.time() - float(payload.get("at", 0)) > constants.MODEL_CACHE_TTL_SECONDS:
        return None
    models = payload.get("models")
    if isinstance(models, list) and models:
        return models
    return None


def _save_cache(models: list[dict]) -> None:
    try:
        _cache_file().write_text(
            json.dumps({"at": time.time(), "models": models}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - 缓存写不进去不影响功能
        logger.info("写模型缓存失败：%s", exc)


def models_equal(left: list[dict] | None, right: list[dict] | None) -> bool:
    """只比较我们负责的字段，避免每次都重写 provider。"""

    def normalize(models: list[dict] | None) -> str:
        rows = []
        for model in models or []:
            if not isinstance(model, dict):
                continue
            rows.append(
                {
                    "id": model.get("id") or "",
                    "name": model.get("name") or "",
                    "contextWindow": model.get("contextWindow") or 0,
                    "input": model.get("input") or [],
                    "reasoningEfforts": model.get("reasoningEfforts") or {},
                }
            )
        return json.dumps(rows, ensure_ascii=False, sort_keys=True)

    return normalize(left) == normalize(right)


def resolve_models(token: str | None = None, *, use_cache: bool = True) -> list[dict]:
    """站点前端包 → （读不到时）`/v1/models` → 内置名单。"""

    if use_cache:
        cached = _load_cache()
        if cached:
            return cached

    live = fetch_site_models()
    if not live and token:
        fallback = fetch_models(token)
        if fallback:
            live = [
                {
                    "id": str(item["id"]),
                    "name": f"{item['id']} (THU)",
                    "contextWindow": constants.CONTEXT_WINDOW,
                    "input": ["text", "image"],
                }
                for item in fallback
                if str(item["id"]) not in constants.EXCLUDED_MODEL_IDS
            ] or None

    if live:
        _save_cache(live)
        return live

    logger.info("模型列表读不到，回退内置名单")
    return [dict(model) for model in constants.DEFAULT_MODELS]
