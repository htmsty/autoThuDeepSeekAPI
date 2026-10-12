"""JWT 解析：只看载荷，不验签（验签由站点负责）。"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone

JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def looks_like_jwt(value: str | None) -> bool:
    return bool(value) and bool(JWT_PATTERN.fullmatch(value.strip()))


def find_jwt(text: str | None) -> str | None:
    """从任意文本里找出第一段 JWT。"""

    if not text:
        return None
    match = JWT_PATTERN.search(text)
    return match.group(0) if match else None


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_payload(token: str) -> dict:
    """解出 JWT 载荷；失败返回空字典。"""

    parts = token.strip().split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def expiry(token: str) -> datetime | None:
    """取 exp（UTC）。"""

    exp = decode_payload(token).get("exp")
    if not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def remaining(token: str, now: datetime | None = None) -> timedelta | None:
    exp = expiry(token)
    if exp is None:
        return None
    now = now or datetime.now(timezone.utc)
    return exp - now


def is_expired(token: str, now: datetime | None = None, skew: timedelta = timedelta(0)) -> bool | None:
    """True 已过期 / False 仍有效 / None 看不懂。"""

    left = remaining(token, now)
    if left is None:
        return None
    return left <= skew


def describe_remaining(token: str) -> str:
    """给界面用的剩余时间描述。"""

    left = remaining(token)
    if left is None:
        return "无法解析到期时间"
    total_minutes = int(left.total_seconds() // 60)
    if total_minutes < 0:
        return "已过期"
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"剩余 {hours} 小时 {minutes} 分"
    return f"剩余 {minutes} 分"

