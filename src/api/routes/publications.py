"""Local-only management API; the public reader is a separate ASGI application."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from src.core.paths import check_chapter_id, check_doc_id
from src.core.publications import PublicationStore

router = APIRouter(prefix="/api/v1/publications")


class Snapshot(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    html: str = Field(min_length=1, max_length=40 * 1024 * 1024)
    doc_id: str
    chapter_id: str | None = None
    base_revision: int | None = Field(default=None, ge=1)


class Revision(BaseModel):
    base_revision: int = Field(ge=1)


def store(request: Request) -> PublicationStore:
    return PublicationStore(request.app.state.app_state.layout.data_dir / "publications")


@router.get("")
def list_publications(request: Request):
    return {"items": store(request).list(), "deployment": "not_configured",
            "message": "本地分享版本已持久保存，尚未部署公网阅读服务。"}


def save(request: Request, body: Snapshot, publication_id: str | None = None):
    check_doc_id(body.doc_id)
    if body.chapter_id:
        check_chapter_id(body.chapter_id)
    return store(request).save(**body.model_dump(), publication_id=publication_id)


@router.post("")
def create_publication(request: Request, body: Snapshot):
    return save(request, body)


@router.put("/{publication_id}")
def update_publication(request: Request, publication_id: str, body: Snapshot):
    # Require existing ID even if a caller happens to supply a valid random string.
    store(request).read(publication_id)
    return save(request, body, publication_id)


@router.get("/{publication_id}")
def get_publication(request: Request, publication_id: str):
    return store(request).read(publication_id)


@router.delete("/{publication_id}")
def revoke_publication(request: Request, publication_id: str, body: Revision):
    return store(request).revoke(publication_id, body.base_revision)
