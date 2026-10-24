"""HTTPS 证书上下文，兼容 PyInstaller 与精简版 Python 运行时。"""

from __future__ import annotations

import logging
import ssl
from functools import lru_cache

logger = logging.getLogger(__name__)


def _server_auth_trusted(trust: object, purpose_oid: str) -> bool:
    if trust is True:
        return True
    if isinstance(trust, (set, frozenset, list, tuple)):
        return purpose_oid in trust
    return False


def _new_ssl_context() -> ssl.SSLContext:
    """Create the full SSLContext subclass without its custom __new__ hook."""

    raw_context = getattr(ssl, "_SSLContext", None)
    if raw_context is None:
        raise RuntimeError("当前 Python 没有可用的底层 SSLContext")
    context = raw_context.__new__(ssl.SSLContext, ssl.PROTOCOL_TLS_CLIENT)
    if not hasattr(context, "wrap_socket"):
        raise RuntimeError("当前 Python 的 SSLContext 缺少 wrap_socket")
    return context


@lru_cache(maxsize=1)
def _windows_certificate_context() -> ssl.SSLContext:
    """Build a verified client context from the Windows certificate stores."""

    context = _new_ssl_context()
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True

    purpose_oid = getattr(ssl.Purpose.SERVER_AUTH, "oid", "1.3.6.1.5.5.7.3.1")
    loaded = 0
    seen: set[bytes] = set()
    for store_name in ("CA", "ROOT"):
        try:
            certificates = ssl.enum_certificates(store_name)
        except (AttributeError, OSError):
            continue
        for cert, encoding, trust in certificates:
            if encoding != "x509_asn" or not _server_auth_trusted(trust, purpose_oid):
                continue
            if cert in seen:
                continue
            try:
                context.load_verify_locations(cadata=cert)
            except (ssl.SSLError, ValueError):
                continue
            seen.add(cert)
            loaded += 1

    if not loaded:
        raise ssl.SSLError("Windows 证书库里没有可用于服务器认证的根证书")
    logger.info("已从 Windows 证书库加载 %d 张根证书（SSL 兼容模式）", loaded)
    return context


def create_verified_context() -> ssl.SSLContext:
    """Create a certificate-verifying context with a Windows-store fallback."""

    try:
        return ssl.create_default_context()
    except (FileNotFoundError, OSError, ssl.SSLError) as exc:
        # 精简 Python / PyInstaller 可能缺少 ssl.py 预期的 CA 文件。
        # 回退仍保持 CERT_REQUIRED 和主机名校验，不降级成“跳过证书验证”。
        logger.warning("默认 CA 初始化失败，改用 Windows 证书库：%s", exc)
        return _windows_certificate_context()
