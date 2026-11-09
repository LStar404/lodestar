"""本地安全边界（10.6.5）：Host/Origin/Fetch Metadata + X-Local-Token 双重校验。"""

from __future__ import annotations

import secrets
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    """拒绝跨站请求；/api/* 需要 X-Local-Token（生产由 HTML meta 注入，开发由 Vite 代理注入）。"""

    def __init__(self, app, *, token: str, allowed_hosts: set[str],
                 allowed_origins: set[str]):
        super().__init__(app)
        self.token = token
        self.allowed_hosts = allowed_hosts
        self.allowed_origins = allowed_origins

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        if host and host not in self.allowed_hosts:
            return JSONResponse({"error": "FORBIDDEN_ORIGIN", "message": f"Host 不被允许: {host}"},
                                status_code=403)
        origin = request.headers.get("origin")
        if origin:
            if origin == "null" or origin not in self.allowed_origins:
                return JSONResponse({"error": "FORBIDDEN_ORIGIN", "message": f"Origin 不被允许: {origin}"},
                                    status_code=403)
        sec_fetch_site = request.headers.get("sec-fetch-site")
        if sec_fetch_site == "cross-site":
            return JSONResponse({"error": "FORBIDDEN_ORIGIN", "message": "拒绝 cross-site 请求"},
                                status_code=403)

        path = request.url.path
        if path.startswith("/api/"):
            supplied = request.headers.get("x-local-token") or request.cookies.get("lodestar_session")
            if not supplied or not secrets.compare_digest(str(supplied), self.token):
                return JSONResponse({"error": "FORBIDDEN", "message": "缺少或错误的 X-Local-Token"},
                                    status_code=403)
        response: Response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        return response


def load_or_create_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass
    return token
