from __future__ import annotations

import json
import os
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from control_plane.auth import (
    admin_auth_bypassed,
    admin_auth_configured,
    admin_auth_enabled,
    admin_cookie_value,
    admin_password_state,
    admin_unauthorized_response,
    clear_admin_cookie,
    clear_login_failures,
    clear_admin_session,
    create_admin_session,
    is_admin_authenticated,
    login_throttled,
    record_login_failure,
    request_is_secure,
    set_admin_cookie,
    verify_admin_password,
)
from control_plane.config import (
    ADMIN_MIN_PASSWORD_LENGTH,
    ADMIN_PASSWORD,
    CHANNEL_ENV_KEYS,
    HERMES_CONFIG_PATH,
    HERMES_ENV_PATH,
    STATUS_CACHE_TTL,
    UNSUPPORTED_PROVIDER_NOTE,
    WEBUI_STATE_DIR,
    WORKSPACE_DIR,
    apply_provider_setup,
    approve_pairing,
    channel_form_values,
    channel_summary,
    deny_pairing,
    ensure_runtime_dirs,
    extract_model_config,
    get_approved_users,
    get_pending_pairings,
    load_env_file,
    load_yaml_config,
    masked_env_snapshot,
    provider_catalog,
    revoke_user,
    save_channel_values,
)
from control_plane.gateway_manager import GatewayManager
from control_plane.proxy import proxy_to_webui
from control_plane.webui_manager import WebUIManager

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

webui_manager = WebUIManager()
gateway_manager = GatewayManager()
_status_cache: dict[str, object] = {"ts": 0.0, "data": None}


def _invalidate_status_cache() -> None:
    _status_cache["ts"] = 0.0
    _status_cache["data"] = None


def _current_status() -> dict:
    now = time.time()
    if _status_cache["data"] and (now - float(_status_cache["ts"])) < STATUS_CACHE_TTL:
        return _status_cache["data"]  # type: ignore[return-value]

    env_values = load_env_file(HERMES_ENV_PATH)
    config = load_yaml_config(HERMES_CONFIG_PATH)
    data = {
        "webui": webui_manager.status(),
        "gateway": gateway_manager.status(),
        "model": extract_model_config(config),
        "channels": channel_summary(env_values),
        "env_masked": masked_env_snapshot(env_values),
        "autostart": gateway_manager.should_autostart(),
        "provider_catalog": provider_catalog(),
        "unsupported_provider_note": UNSUPPORTED_PROVIDER_NOTE,
        "paths": {
            "hermes_home": str(HERMES_CONFIG_PATH.parent),
            "config_path": str(HERMES_CONFIG_PATH),
            "env_path": str(HERMES_ENV_PATH),
            "webui_state_dir": str(WEBUI_STATE_DIR),
            "workspace_dir": str(WORKSPACE_DIR),
        },
    }
    _status_cache["ts"] = now
    _status_cache["data"] = data
    return data


def _admin_required(request: Request) -> Response | None:
    if is_admin_authenticated(request):
        return None
    return admin_unauthorized_response(request)


def _log_admin_auth_state() -> None:
    state = admin_password_state()
    if state == "ok":
        return
    reason = (
        f"HERMES_ADMIN_PASSWORD is shorter than {ADMIN_MIN_PASSWORD_LENGTH} characters"
        if state == "too_short"
        else "neither HERMES_ADMIN_PASSWORD nor HERMES_WEBUI_PASSWORD is set"
    )
    if admin_auth_bypassed():
        print(
            f"[control-plane] SECURITY WARNING: {reason}, and HERMES_ALLOW_INSECURE_ADMIN is on. "
            "/admin is served WITHOUT authentication — anyone who can reach this URL can write "
            "provider API keys, channel bot tokens, and approve chat pairings.",
            flush=True,
        )
    else:
        print(
            f"[control-plane] /admin is LOCKED: {reason}. "
            "Set HERMES_ADMIN_PASSWORD and redeploy to unlock the control plane.",
            flush=True,
        )


async def on_startup() -> None:
    _log_admin_auth_state()
    ensure_runtime_dirs()
    webui_manager.start()
    webui_manager.wait_until_ready(timeout=30)
    if gateway_manager.should_autostart():
        gateway_manager.start()
    _invalidate_status_cache()


async def on_shutdown() -> None:
    gateway_manager.stop()
    webui_manager.stop()


async def health(request: Request) -> JSONResponse:
    status = _current_status()
    webui_ok = bool(status["webui"]["healthy"])
    payload = {
        "status": "ok" if webui_ok else "degraded",
        "service": "hermes-control-plane",
        "webui": status["webui"],
        "gateway": {
            "running": status["gateway"]["running"],
            "healthy": status["gateway"]["healthy"],
        },
    }
    return JSONResponse(payload, status_code=200 if webui_ok else 503)


_LOGIN_PAGE_CSS = """
    body{font-family:Inter,system-ui,sans-serif;background:#0f172a;color:#e5e7eb;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
    form,div.notice{background:#111827;border:1px solid #1f2937;border-radius:16px;padding:32px;min-width:320px;max-width:520px;box-shadow:0 24px 80px rgba(0,0,0,.35)}
    h1{margin:0 0 8px;font-size:24px}p{margin:0 0 24px;color:#94a3b8}input{width:100%;padding:12px 14px;border-radius:12px;border:1px solid #334155;background:#020617;color:#e5e7eb;box-sizing:border-box}button{margin-top:16px;width:100%;padding:12px 14px;border:0;border-radius:12px;background:#2563eb;color:#fff;font-weight:600}small{display:block;margin-top:16px;color:#64748b}
    code{background:#020617;border:1px solid #334155;border-radius:6px;padding:2px 6px;font-size:13px}
    .warn{color:#fca5a5}
"""


def _setup_required_page(state: str) -> HTMLResponse:
    """Shown when /admin is locked because no usable password is configured."""
    if state == "too_short":
        detail = (
            f"<p class=\"warn\">The configured admin password is shorter than "
            f"{ADMIN_MIN_PASSWORD_LENGTH} characters, so it is being ignored.</p>"
        )
    else:
        detail = "<p class=\"warn\">No admin password is configured, so the control plane is locked.</p>"
    html = f"""
    <!doctype html>
    <html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Hermes Admin — setup required</title>
    <style>{_LOGIN_PAGE_CSS}</style></head><body><div class="notice">
    <h1>Setup required</h1>
    {detail}
    <p>Set <code>HERMES_ADMIN_PASSWORD</code> (at least {ADMIN_MIN_PASSWORD_LENGTH} characters) in your
    Railway service variables and redeploy. <code>HERMES_WEBUI_PASSWORD</code> is used as a fallback.</p>
    <small>Every <code>/admin</code> route stays closed until then — this deployment can write provider
    API keys, channel bot tokens and pairing approvals, so it is never served unauthenticated by default.
    For a private, non-public deployment you can set <code>HERMES_ALLOW_INSECURE_ADMIN=1</code> to opt out,
    at your own risk.</small>
    </div></body></html>
    """
    return HTMLResponse(html, status_code=503)


async def admin_login_page(request: Request) -> Response:
    if is_admin_authenticated(request):
        return RedirectResponse(url="/admin", status_code=302)
    state = admin_password_state()
    if state != "ok":
        return _setup_required_page(state)
    html = """
    <!doctype html>
    <html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Hermes Admin Login</title>
    <style>
    body{font-family:Inter,system-ui,sans-serif;background:#0f172a;color:#e5e7eb;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
    form{background:#111827;border:1px solid #1f2937;border-radius:16px;padding:32px;min-width:320px;box-shadow:0 24px 80px rgba(0,0,0,.35)}
    h1{margin:0 0 8px;font-size:24px}p{margin:0 0 24px;color:#94a3b8}input{width:100%;padding:12px 14px;border-radius:12px;border:1px solid #334155;background:#020617;color:#e5e7eb;box-sizing:border-box}button{margin-top:16px;width:100%;padding:12px 14px;border:0;border-radius:12px;background:#2563eb;color:#fff;font-weight:600}small{display:block;margin-top:16px;color:#64748b}
    </style></head><body><form method=\"post\" action=\"/admin/login\"><h1>Hermes Admin</h1><p>Control plane access for your Railway deployment.</p><input type=\"password\" name=\"password\" placeholder=\"Admin password\" autofocus required><button type=\"submit\">Sign in</button><small>Set HERMES_ADMIN_PASSWORD for a dedicated admin password. If omitted, Hermes falls back to HERMES_WEBUI_PASSWORD.</small></form></body></html>
    """
    return HTMLResponse(html)


async def admin_login(request: Request) -> Response:
    from urllib.parse import parse_qs

    if not admin_auth_configured():
        return _setup_required_page(admin_password_state())
    if login_throttled(request):
        return HTMLResponse("Too many failed attempts. Try again later.", status_code=429)

    raw = (await request.body()).decode("utf-8", errors="ignore")
    form = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
    password = str(form.get("password") or "")
    if not verify_admin_password(password):
        record_login_failure(request)
        return HTMLResponse("Invalid password", status_code=401)

    clear_login_failures(request)
    response = RedirectResponse(url="/admin", status_code=302)
    set_admin_cookie(request, response, create_admin_session())
    return response


async def admin_logout(request: Request) -> Response:
    clear_admin_session(admin_cookie_value(request))
    response = RedirectResponse(url="/admin/login", status_code=302)
    clear_admin_cookie(response)
    return response


async def admin_index(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    status = _current_status()
    return TEMPLATES.TemplateResponse(
        request,
        "admin.html",
        {
            "status": status,
            "provider_catalog": provider_catalog(),
            "unsupported_provider_note": UNSUPPORTED_PROVIDER_NOTE,
            "admin_auth_enabled": admin_auth_enabled(),
            "admin_auth_bypassed": admin_auth_bypassed(),
            "has_separate_admin_password": bool(ADMIN_PASSWORD),
        },
    )


async def api_status(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    return JSONResponse(_current_status())


async def api_gateway_action(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    action = request.path_params["action"]
    if action == "start":
        gateway_manager.start()
    elif action == "stop":
        gateway_manager.stop()
    elif action == "restart":
        gateway_manager.restart()
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    _invalidate_status_cache()
    return JSONResponse({"ok": True, "status": _current_status()})


async def api_provider_setup(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    body = await request.json()
    result = apply_provider_setup(
        config_path=HERMES_CONFIG_PATH,
        env_path=HERMES_ENV_PATH,
        provider=str(body.get("provider") or ""),
        model=str(body.get("model") or ""),
        api_key=str(body.get("api_key") or ""),
        base_url=str(body.get("base_url") or ""),
    )
    _invalidate_status_cache()
    if gateway_manager.is_running():
        gateway_manager.restart()
    elif gateway_manager.should_autostart():
        gateway_manager.start()
    return JSONResponse({"ok": True, "result": result, "status": _current_status()})


async def api_channel_values(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    env_values = load_env_file(HERMES_ENV_PATH)
    return JSONResponse({"ok": True, "values": channel_form_values(env_values)})


async def api_channel_save(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    body = await request.json()
    all_keys = set(CHANNEL_ENV_KEYS) | {"GATEWAY_ALLOW_ALL_USERS"}
    updates = {key: body.get(key) for key in all_keys if key in body}
    if "TELEGRAM_BOT_TOKEN" in updates:
        updates.setdefault("TELEGRAM_ALLOWED_USERS", None)
    if "DISCORD_BOT_TOKEN" in updates:
        updates.setdefault("DISCORD_ALLOWED_USERS", None)
    env_values = save_channel_values(HERMES_ENV_PATH, updates)
    _invalidate_status_cache()
    restarted = False
    if gateway_manager.is_running():
        gateway_manager.restart()
        restarted = True
    elif gateway_manager.should_autostart():
        gateway_manager.start()
        restarted = True
    return JSONResponse({"ok": True, "restarted": restarted, "channels": channel_summary(env_values), "status": _current_status()})


async def api_webui_action(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    action = request.path_params["action"]
    if action == "restart":
        webui_manager.restart()
        webui_manager.wait_until_ready(timeout=30)
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    _invalidate_status_cache()
    return JSONResponse({"ok": True, "status": webui_manager.status()})


async def api_pairing_pending(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    return JSONResponse({"ok": True, "pending": get_pending_pairings()})


async def api_pairing_approved(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    return JSONResponse({"ok": True, "approved": get_approved_users()})


async def api_pairing_approve(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    body = await request.json()
    platform = str(body.get("platform", "")).strip()
    code = str(body.get("code", "")).upper().strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)
    try:
        result = approve_pairing(platform, code)
    except KeyError:
        return JSONResponse({"error": "Code not found or expired"}, status_code=404)
    return JSONResponse({"ok": True, **result})


async def api_pairing_deny(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    body = await request.json()
    platform = str(body.get("platform", "")).strip()
    code = str(body.get("code", "")).upper().strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)
    deny_pairing(platform, code)
    return JSONResponse({"ok": True})


async def api_pairing_revoke(request: Request) -> Response:
    unauthorized = _admin_required(request)
    if unauthorized:
        return unauthorized
    body = await request.json()
    platform = str(body.get("platform", "")).strip()
    user_id = str(body.get("user_id", "")).strip()
    if not platform or not user_id:
        return JSONResponse({"error": "platform and user_id required"}, status_code=400)
    revoke_user(platform, user_id)
    return JSONResponse({"ok": True})


async def proxy_catchall(request: Request) -> Response:
    path = request.path_params.get("path", "")
    return await proxy_to_webui(request, path)


routes = [
    Route("/health", health),
    Route("/admin", admin_index),
    Route("/admin/login", admin_login_page, methods=["GET"]),
    Route("/admin/login", admin_login, methods=["POST"]),
    Route("/admin/logout", admin_logout, methods=["POST"]),
    Route("/admin/api/status", api_status),
    Route("/admin/api/gateway/{action}", api_gateway_action, methods=["POST"]),
    Route("/admin/api/provider/setup", api_provider_setup, methods=["POST"]),
    Route("/admin/api/channels", api_channel_values, methods=["GET"]),
    Route("/admin/api/channels/save", api_channel_save, methods=["POST"]),
    Route("/admin/api/webui/{action}", api_webui_action, methods=["POST"]),
    Route("/admin/api/pairing/pending", api_pairing_pending),
    Route("/admin/api/pairing/approved", api_pairing_approved),
    Route("/admin/api/pairing/approve", api_pairing_approve, methods=["POST"]),
    Route("/admin/api/pairing/deny", api_pairing_deny, methods=["POST"]),
    Route("/admin/api/pairing/revoke", api_pairing_revoke, methods=["POST"]),
    Mount("/admin/static", app=StaticFiles(directory=str(BASE_DIR / "static")), name="admin-static"),
    Route("/", proxy_catchall, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]),
    Route("/{path:path}", proxy_catchall, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]),
]

class AdminSecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Harden /admin responses. WebUI responses are passed through untouched
    so the proxied app keeps control of its own headers."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/admin"):
            return response
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        # Admin pages carry masked secrets and status — never let a shared cache hold them.
        response.headers["Cache-Control"] = "no-store"
        if request_is_secure(request):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


app = Starlette(
    routes=routes,
    middleware=[Middleware(AdminSecurityHeadersMiddleware)],
    on_startup=[on_startup],
    on_shutdown=[on_shutdown],
)
