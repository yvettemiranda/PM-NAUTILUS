"""HTTP authentication, control-request origin checks and response hardening.

Moved unchanged from ``app.create_app``; the app passes its per-process
password, allowed hosts, CSRF token and shutdown flag explicitly.
"""

import base64
import os
import secrets

from fastapi import HTTPException
from fastapi.responses import JSONResponse

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def secret_matches(given: str, expected: str) -> bool:
    # secrets.compare_digest rejects non-ASCII str with TypeError. Basic-auth
    # credentials and headers can contain any character (a Chinese password,
    # or a malformed client header), so compare their UTF-8 bytes instead.
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


async def json_object(request) -> dict:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("请求内容须为JSON对象")
    return payload


def install_security(app, *, password, allowed, csrf, is_shutting_down):
    """Register the single HTTP middleware that guards every request."""

    @app.middleware("http")
    async def security(request, call_next):
        if is_shutting_down() and request.method not in {"GET", "HEAD"}:
            return JSONResponse({"error": "服务正在关闭，请稍后重试"}, 503)
        host = request.url.hostname
        if host not in allowed:
            return JSONResponse({"error": "Host未列入服务器允许列表"}, 400)
        if not password and host not in LOCAL_HOSTS:
            return JSONResponse({"error": "公网访问需要服务器身份验证"}, 403)
        if request.url.path != "/api/health" and password:
            try:
                scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
                user, pw = base64.b64decode(encoded, validate=True).decode().split(":", 1)
                ok = (
                    scheme.lower() == "basic"
                    and secret_matches(user, "pm")
                    and secret_matches(pw, password)
                )
            except (ValueError, UnicodeError):
                ok = False
            if not ok:
                return JSONResponse(
                    {"error": "需要登录"},
                    401,
                    headers={"WWW-Authenticate": 'Basic realm="PM-NAUTILUS"'},
                )
        if request.method not in {"GET", "HEAD"}:
            if not secret_matches(request.headers.get("x-pm-csrf", ""), csrf):
                return JSONResponse({"error": "控制请求来源校验失败"}, 403)
            origin = request.headers.get("origin")
            expected_origin = str(request.base_url).rstrip("/")
            if os.getenv("PM_TRUST_HTTPS_PROXY") == "true":
                forwarded_scheme = request.headers.get("x-forwarded-proto")
                if forwarded_scheme in {"http", "https"}:
                    expected_origin = f"{forwarded_scheme}://{request.headers['host']}"
            if origin and origin.rstrip("/") != expected_origin:
                return JSONResponse({"error": "跨来源控制被拒绝"}, 403)
        response = await call_next(request)
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        response.headers["cache-control"] = "no-store"
        response.headers["content-security-policy"] = CONTENT_SECURITY_POLICY
        if request.url.path.startswith("/api/"):
            response.headers["cache-control"] = "no-store"
            if request.url.path != "/api/health":
                response.headers["x-pm-csrf"] = csrf
        return response

    return security


def require_secure_wallet_request(request, password):
    host = request.url.hostname
    local = host in LOCAL_HOSTS
    scheme = request.url.scheme
    if os.getenv("PM_TRUST_HTTPS_PROXY") == "true":
        scheme = request.headers.get("x-forwarded-proto", scheme)
    if not local and scheme != "https":
        raise HTTPException(403, "钱包操作必须使用 HTTPS")
    if not local and not password:
        raise HTTPException(403, "钱包操作需要网页登录密码")
