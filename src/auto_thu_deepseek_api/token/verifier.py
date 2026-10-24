"""Token 校验与模型列表获取（只用标准库，便于打包）。"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..constants import MADMODEL_API_BASE, MADMODEL_AUTH_LOGIN_PATH, MADMODEL_ORIGIN
from ..http_tls import create_verified_context

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
LIGHT_TIMEOUT = 15
CHAT_TIMEOUT = 30
MAX_RESPONSE_BYTES = 512 * 1024


@dataclass
class VerifyResult:
    """ok: True 有效 / False 明确被拒 / None 未能判定（网络问题等）。"""

    ok: bool | None
    detail: str
    status: int | None = None
    looks_like_html: bool = False


def _request(
    url: str,
    token: str,
    payload: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int | None, bytes, str]:
    """返回 (status, body, error)。错误时 status 为 None。"""

    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    if data:
        request.add_header("Content-Type", "application/json")

    try:
        context = create_verified_context()
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return response.status, b"", f"响应超过 {MAX_RESPONSE_BYTES} 字节上限"
            return response.status, body, ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES]
        except Exception:  # pragma: no cover
            body = b""
        return exc.code, body, ""
    except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一处理
        return None, b"", f"{type(exc).__name__}: {exc}"


def _first_model_id(models: list[dict] | None) -> str:
    if models:
        model_id = models[0].get("id")
        if isinstance(model_id, str) and model_id:
            return model_id
    from ..constants import DEFAULT_MODELS

    return str(DEFAULT_MODELS[0]["id"])


def _light_verdict(token: str, timeout: int) -> VerifyResult:
    """站点会话校验：GET /model-api/auth-login，不消耗模型额度。"""

    status, body, error = _request(
        f"{MADMODEL_ORIGIN}{MADMODEL_AUTH_LOGIN_PATH}", token, None, timeout
    )
    if status is None:
        return VerifyResult(None, f"未能连接站点校验（{error}）")
    if status in (401, 403):
        return VerifyResult(False, f"站点拒绝该 Token（HTTP {status}）", status)
    if status != 200:
        return VerifyResult(None, f"校验接口返回 HTTP {status}", status)

    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        text = body[:64].decode("utf-8", "replace").lstrip()
        html_like = text.startswith("<")
        detail = "校验接口返回的是网页而不是 JSON（多半没登录或接口变了）" if html_like else "校验接口没有返回预期的 JSON"
        return VerifyResult(None, detail, status, looks_like_html=html_like)
    if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
        return VerifyResult(None, "校验接口的返回结构不是预期的样子", status)
    if payload["success"]:
        return VerifyResult(True, "站点确认 Token 可用", status)
    return VerifyResult(False, "站点未接受该 Token（auth-login success=false）", status)


def _chat_verdict(token: str, model_id: str | None, timeout: int) -> VerifyResult:
    """兜底校验：发一次最小对话请求（慢，但结论最硬）。"""

    model = model_id or _first_model_id(None)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "max_tokens": 1,
    }
    status, body, error = _request(f"{MADMODEL_API_BASE}/chat/completions", token, payload, timeout)

    if status is None:
        return VerifyResult(None, f"未能连接站点校验（{error}）")
    if status in (401, 403):
        return VerifyResult(False, f"站点拒绝该 Token（HTTP {status}）", status)
    if status == 200:
        return VerifyResult(True, "站点确认 Token 可用", status)

    snippet = body[:200].decode("utf-8", "replace").strip()
    return VerifyResult(None, f"站点返回 HTTP {status}：{snippet or '（无内容）'}", status)


def verify_token(
    token: str,
    model_id: str | None = None,
    timeout: int | None = None,
    *,
    allow_chat_fallback: bool = False,
) -> VerifyResult:
    """先走站点的会话校验（快、不花额度），给不出结论时再发一次最小对话请求。"""

    light = _light_verdict(token, timeout or LIGHT_TIMEOUT)
    if light.ok is not None:
        return light

    # 连不上站点时没必要再打一次：结论同样是"未知"，只是白等更久。
    if light.status is None:
        return light
    # 站点回的是网页（SPA / 登录页）时，再发一次对话请求也是白等 30 秒。
    if light.looks_like_html:
        return light
    if not allow_chat_fallback:
        return VerifyResult(
            None,
            light.detail + "；为避免消耗额度，没有发送最小对话校验",
            light.status,
        )

    logger.info("会话校验未给出结论（%s），改用最小对话请求核对", light.detail)
    return _chat_verdict(token, model_id, timeout or CHAT_TIMEOUT)


def fetch_models(token: str, timeout: int = 15) -> list[dict] | None:
    """尝试读取站点模型列表；读不到返回 None（不是错误）。"""

    status, body, error = _request(f"{MADMODEL_API_BASE}/models", token, None, timeout)
    if status is None:
        logger.info("读取模型列表失败：%s", error)
        return None
    if status != 200:
        logger.info("读取模型列表返回 HTTP %s", status)
        return None
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        logger.info("模型列表不是 JSON（站点可能回落到网页）")
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    models: list[dict] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append({"id": item["id"]})
    return models or None


def site_reachable(timeout: int = 5) -> bool:
    """轻量探测站点能不能连上（只看首页有没有回应，不涉及登录态）。

    开机启动时网络经常还没就绪：先探一下再决定要不要开始取 Token，
    省得白白等一轮 40 秒的静默超时，失败后再靠短间隔重试补上。
    """

    request = urllib.request.Request(f"{MADMODEL_ORIGIN}/", method="HEAD")
    request.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) autoThuDeepSeekAPI")
    try:
        context = create_verified_context()
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        # 能回 4xx 说明网络是通的（只是不接受 HEAD 这种方式），同样算可达
        return exc.code < 500
    except Exception:  # noqa: BLE001 - 网络异常种类多，统一当"连不上"
        return False

