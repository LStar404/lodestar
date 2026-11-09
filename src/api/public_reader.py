"""Optional standalone read-only share service. Never mounts the local management app."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from src.core.errors import NotFound
from src.core.publications import CSP, PublicationStore


def create_reader(directory: str | Path) -> FastAPI:
    app = FastAPI(title="Lodestar 星引 · 分享阅读", docs_url=None, redoc_url=None, openapi_url=None)
    snapshots = PublicationStore(Path(directory))

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "read-only"}

    @app.get("/s/{publication_id}")
    def read(publication_id: str):
        try:
            item = snapshots.read(publication_id)
        except (NotFound, ValueError, OSError):
            return JSONResponse({"message": "分享不存在或已撤销"}, status_code=404,
                                headers={"Cache-Control": "no-store"})
        if item["revoked"]:
            return JSONResponse({"message": "分享不存在或已撤销"}, status_code=404,
                                headers={"Cache-Control": "no-store"})
        return HTMLResponse(item["html"], headers={
            "Content-Security-Policy": CSP, "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "X-Robots-Tag": "noindex, nofollow",
        })

    return app


def create_app() -> FastAPI:
    directory = os.environ.get("LODESTAR_PUBLICATIONS_DIR") or os.environ.get("NOTE_GEN_PUBLICATIONS_DIR")
    if not directory:
        raise RuntimeError("请设置 LODESTAR_PUBLICATIONS_DIR（旧名 NOTE_GEN_PUBLICATIONS_DIR 仍兼容），指向单独的分享版本目录")
    return create_reader(directory)
