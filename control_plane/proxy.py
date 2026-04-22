from __future__ import annotations

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from control_plane.config import INTERNAL_WEBUI_BASE

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


async def proxy_to_webui(request: Request, path: str) -> Response:
    upstream_path = "/" + path.lstrip("/")
    target_url = f"{INTERNAL_WEBUI_BASE}{upstream_path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    headers = {}
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in _HOP_BY_HOP:
            continue
        headers[key] = value
    headers["X-Forwarded-Host"] = request.headers.get("host", "")
    headers["X-Forwarded-Proto"] = request.url.scheme
    headers["X-Real-Host"] = request.headers.get("host", "")

    body = await request.body()
    client = httpx.AsyncClient(follow_redirects=False, timeout=60.0)
    upstream_request = client.build_request(
        request.method,
        target_url,
        headers=headers,
        content=body if body else None,
    )
    upstream_response = await client.send(upstream_request, stream=True)
    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in _HOP_BY_HOP
    }

    async def _close_upstream() -> None:
        await upstream_response.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        background=BackgroundTask(_close_upstream),
    )
