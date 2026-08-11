from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from control_plane.config import (
    ADMIN_ALLOW_INSECURE,
    ADMIN_COOKIE_NAME,
    ADMIN_LOGIN_MAX_ATTEMPTS,
    ADMIN_LOGIN_WINDOW_SECONDS,
    ADMIN_MIN_PASSWORD_LENGTH,
    ADMIN_PASSWORD,
    ADMIN_SESSION_TTL,
    DATA_DIR,
    TRUST_PROXY_HEADERS,
)

_SESSIONS: dict[str, float] = {}
_LOGIN_FAILURES: dict[str, list[float]] = {}
_SIGNING_KEY_PATH = DATA_DIR / ".admin_signing_key"
_EPHEMERAL_SIGNING_KEY: bytes | None = None


def _signing_key() -> bytes:
    global _EPHEMERAL_SIGNING_KEY
    if _SIGNING_KEY_PATH.exists():
        raw = _SIGNING_KEY_PATH.read_bytes()
        if len(raw) >= 32:
            return raw[:32]
    key = secrets.token_bytes(32)
    try:
        _SIGNING_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SIGNING_KEY_PATH.write_bytes(key)
        try:
            _SIGNING_KEY_PATH.chmod(0o600)
        except OSError:
            pass
        return key
    except OSError:
        if _EPHEMERAL_SIGNING_KEY is None:
            _EPHEMERAL_SIGNING_KEY = key
        return _EPHEMERAL_SIGNING_KEY


def admin_password_state() -> str:
    """One of ``ok`` / ``missing`` / ``too_short``."""
    if not ADMIN_PASSWORD:
        return "missing"
    if len(ADMIN_PASSWORD) < ADMIN_MIN_PASSWORD_LENGTH:
        return "too_short"
    return "ok"


def admin_auth_configured() -> bool:
    """True when a usable admin password is set."""
    return admin_password_state() == "ok"


def admin_auth_bypassed() -> bool:
    """True only when the operator explicitly opted out of admin auth.

    Requires HERMES_ALLOW_INSECURE_ADMIN *and* the absence of a real password,
    so the escape hatch can never silently disable a configured password.
    """
    return ADMIN_ALLOW_INSECURE and not admin_auth_configured()


def admin_auth_enabled() -> bool:
    """True when /admin is gated. Only an explicit opt-out turns this off."""
    return not admin_auth_bypassed()


def verify_admin_password(password: str) -> bool:
    """Constant-time password check. Fails closed when auth is unconfigured."""
    if not admin_auth_configured():
        return False
    return hmac.compare_digest(password or "", ADMIN_PASSWORD)


def _prune_sessions() -> None:
    now = time.time()
    for token, expiry in list(_SESSIONS.items()):
        if expiry <= now:
            _SESSIONS.pop(token, None)


def create_admin_session() -> str:
    token = secrets.token_hex(32)
    _SESSIONS[token] = time.time() + ADMIN_SESSION_TTL
    sig = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{token}.{sig}"


def verify_admin_session(cookie_value: str | None) -> bool:
    if not cookie_value or "." not in cookie_value:
        return False
    _prune_sessions()
    token, sig = cookie_value.rsplit(".", 1)
    expected = hmac.new(_signing_key(), token.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return False
    expiry = _SESSIONS.get(token)
    if not expiry or expiry <= time.time():
        _SESSIONS.pop(token, None)
        return False
    return True


def clear_admin_session(cookie_value: str | None) -> None:
    if cookie_value and "." in cookie_value:
        token = cookie_value.rsplit(".", 1)[0]
        _SESSIONS.pop(token, None)


def admin_cookie_value(request: Request) -> str | None:
    return request.cookies.get(ADMIN_COOKIE_NAME)


def is_admin_authenticated(request: Request) -> bool:
    if admin_auth_bypassed():
        return True
    # Fail closed: no configured password means nobody gets in.
    if not admin_auth_configured():
        return False
    return verify_admin_session(admin_cookie_value(request))


def set_admin_cookie(request: Request, response: Response, cookie_value: str) -> None:
    response.set_cookie(
        ADMIN_COOKIE_NAME,
        cookie_value,
        httponly=True,
        samesite="strict",
        max_age=ADMIN_SESSION_TTL,
        secure=request_is_secure(request),
        path="/admin",
    )


def clear_admin_cookie(response: Response) -> None:
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/admin")


def request_is_secure(request: Request) -> bool:
    """Whether the *client* connection is HTTPS, honouring the edge proxy."""
    if request.url.scheme == "https":
        return True
    if not TRUST_PROXY_HEADERS:
        return False
    forwarded = request.headers.get("x-forwarded-proto", "")
    # The header may be a comma-separated chain; the client-facing value is first.
    return forwarded.split(",")[0].strip().lower() == "https"


def client_ip(request: Request) -> str:
    """Best-effort client identity for throttling."""
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_throttled(request: Request) -> bool:
    """True when this client has burned through its failed-login budget."""
    ip = client_ip(request)
    cutoff = time.time() - ADMIN_LOGIN_WINDOW_SECONDS
    attempts = [ts for ts in _LOGIN_FAILURES.get(ip, []) if ts > cutoff]
    if attempts:
        _LOGIN_FAILURES[ip] = attempts
    else:
        _LOGIN_FAILURES.pop(ip, None)
    return len(attempts) >= ADMIN_LOGIN_MAX_ATTEMPTS


def record_login_failure(request: Request) -> None:
    ip = client_ip(request)
    cutoff = time.time() - ADMIN_LOGIN_WINDOW_SECONDS
    attempts = [ts for ts in _LOGIN_FAILURES.get(ip, []) if ts > cutoff]
    attempts.append(time.time())
    _LOGIN_FAILURES[ip] = attempts
    # Bound the table so a spray across many source IPs can't grow it forever.
    if len(_LOGIN_FAILURES) > 1024:
        for stale_ip, stamps in list(_LOGIN_FAILURES.items()):
            if not [ts for ts in stamps if ts > cutoff]:
                _LOGIN_FAILURES.pop(stale_ip, None)


def clear_login_failures(request: Request) -> None:
    _LOGIN_FAILURES.pop(client_ip(request), None)


def admin_unauthorized_response(request: Request) -> Response:
    if request.url.path.startswith("/admin/api/"):
        if not admin_auth_configured():
            return JSONResponse(
                {"error": "Admin auth is not configured. Set HERMES_ADMIN_PASSWORD and redeploy."},
                status_code=503,
            )
        return JSONResponse({"error": "Authentication required"}, status_code=401)
    return RedirectResponse(url="/admin/login", status_code=302)
