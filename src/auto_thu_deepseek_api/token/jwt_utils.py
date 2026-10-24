"""JWT 解析：只看载荷，不验签（验签由站点负责）。"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..constants import TOKEN_LIFETIME_HOURS

JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def looks_like_jwt(value: str | None) -> bool:
    return bool(value) and bool(JWT_PATTERN.fullmatch(value.strip()))


def find_jwt(text: str | None) -> str | None:
    """从任意文本里找出第一段 JWT。"""

    if not text:
        return None
    match = JWT_PATTERN.search(text)
    return match.group(0) if match else None


@dataclass(frozen=True)
class JwtCandidate:
    token: str
    source: str = ""
    key: str = ""
    url: str = ""


def _candidate_score(candidate: JwtCandidate) -> int:
    """Rank candidates without assuming a fixed storage key.

    Network responses and cookies from the MadModel origin are strongest. A
    plain JWT in unrelated page text is intentionally weak so an error page or
    documentation example cannot win by appearing first.
    """

    token = candidate.token
    source = candidate.source.lower()
    key = candidate.key.lower()
    url = candidate.url.lower()
    score = 0
    if source == "network":
        score += 60
    elif source == "cookie":
        score += 50
    elif source in {"localstorage", "sessionstorage"}:
        score += 30
    elif source == "text":
        score += 5
    if "madmodel.cs.tsinghua.edu.cn" in url:
        score += 25
    if any(word in key for word in ("token", "auth", "jwt", "access", "credential")):
        score += 20
    payload = decode_payload(token)
    if "exp" in payload:
        score += 15
    if any(word in payload for word in ("sub", "user", "uid", "username")):
        score += 8
    expired_value = is_expired(token)
    if expired_value is True:
        score -= 100
    elif expired_value is False:
        score += 20
    return score


def choose_jwt(candidates: list[JwtCandidate]) -> str | None:
    """Choose a high-confidence JWT or return None when the result is ambiguous."""

    unique: dict[str, JwtCandidate] = {}
    best_scores: dict[str, int] = {}
    for candidate in candidates:
        token = find_jwt(candidate.token)
        if not token:
            continue
        normalized = JwtCandidate(token, candidate.source, candidate.key, candidate.url)
        score = _candidate_score(normalized)
        if token not in unique or score > best_scores[token]:
            unique[token] = normalized
            best_scores[token] = score
    if not unique:
        return None
    ranked = sorted(
        ((best_scores[token], token) for token in unique),
        reverse=True,
    )
    if len(ranked) == 1:
        return ranked[0][1]
    best_score, best_token = ranked[0]
    second_score, second_token = ranked[1]
    if best_token != second_token and best_score > second_score:
        # A tie or a one-point accident is not enough evidence. Prefer waiting
        # for a network response instead of silently writing a wrong token.
        return best_token if best_score - second_score >= 10 else None
    return None


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


def expiry_moment(token: str, obtained_at: str | None = None) -> datetime | None:
    """估算 Token 的到期时刻（本地时区）。

    优先用 Token 自带的 exp；没有 exp 时按"取回时刻 + TOKEN_LIFETIME_HOURS 小时"算，
    这正是站点"有效期 5 小时"的口径。两种依据都拿不到就返回 None。
    """

    exp = expiry(token)
    if exp is not None:
        return exp.astimezone()
    if not obtained_at:
        return None
    try:
        base = datetime.fromisoformat(obtained_at)
    except ValueError:
        return None
    if base.tzinfo is None:
        base = base.astimezone()
    return (base + timedelta(hours=TOKEN_LIFETIME_HOURS)).astimezone()


def describe_expiry(token: str, obtained_at: str | None = None) -> str:
    """给界面用的到期时刻描述，例如「有效至 2026-10-13 08:05」。"""

    moment = expiry_moment(token, obtained_at)
    if moment is None:
        return "到期时间未知"
    return f"有效至 {moment:%Y-%m-%d %H:%M}"


def fingerprint(token: str) -> str:
    """给 Token 算一段 16 位十六进制指纹。

    只用来在日志里确认"两处是不是同一个 Token"，**不能**反推 Token 本身；
    算法与 thu-tok-auto 的 tokenFp 一致（两轮 32 位 FNV-1a 混合）。
    """

    if not token:
        return ""
    first = 0x811C9DC5
    second = 0x01000193
    for index, char in enumerate(token):
        code = ord(char)
        first = ((first ^ code) * 0x01000193) & 0xFFFFFFFF
        second = ((second ^ (code + index)) * 0x85EBCA6B) & 0xFFFFFFFF
    return f"{first:08x}{second:08x}"

