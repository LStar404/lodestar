"""Durable, explicit share snapshots. Reader storage never contains application secrets."""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from filelock import FileLock

from .errors import ConflictError, InputError, NotFound
from .jsonl import atomic_write_bytes
from .text import utc_now_iso

ID_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
MAX_HTML_BYTES = 40 * 1024 * 1024
CSP = ("default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox")


class PublicationStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path(self, publication_id: str) -> Path:
        if not ID_RE.fullmatch(publication_id):
            raise NotFound("分享不存在")
        return self.directory / (publication_id + ".json")

    def read(self, publication_id: str) -> dict:
        path = self.path(publication_id)
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise NotFound("分享不存在或版本损坏") from None
        # A partial backup/corrupted record must not turn the public reader into a 500.
        if (not isinstance(item, dict) or item.get("id") != publication_id
                or not all(isinstance(item.get(key), str) for key in
                           ("title", "doc_id", "html", "created_at", "updated_at"))
                or "chapter_id" not in item
                or not (item["chapter_id"] is None or isinstance(item["chapter_id"], str))
                or type(item.get("revision")) is not int or item["revision"] < 1
                or type(item.get("revoked")) is not bool):
            raise NotFound("分享版本损坏")
        return item

    @staticmethod
    def summary(item: dict) -> dict:
        return {k: v for k, v in item.items() if k != "html"}

    def list(self) -> list[dict]:
        if not self.directory.exists():
            return []
        items = []
        for path in self.directory.glob("*.json"):
            if not ID_RE.fullmatch(path.stem):
                continue
            try:
                items.append(self.summary(self.read(path.stem)))
            except NotFound:
                continue
        return sorted(items, key=lambda x: x["updated_at"], reverse=True)

    def save(self, *, title: str, html: str, doc_id: str, chapter_id: str | None,
             publication_id: str | None = None, base_revision: int | None = None) -> dict:
        if not html.strip() or len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise InputError("分享页面为空或超过 40MB")
        self.directory.mkdir(parents=True, exist_ok=True)
        updating = publication_id is not None
        publication_id = publication_id if updating else secrets.token_urlsafe(24)
        path = self.path(publication_id)
        with FileLock(str(path) + ".lock", timeout=10):
            old = self.read(publication_id) if path.exists() else None
            if (updating or base_revision is not None) and not old:
                raise NotFound("分享不存在")
            if old and old["revision"] != base_revision:
                raise ConflictError("分享已更新，请刷新后重试")
            if old and old["revoked"]:
                raise ConflictError("已撤销的链接不能重新启用，请创建新分享")
            if old and (old["doc_id"] != doc_id or old["chapter_id"] != chapter_id):
                raise ConflictError("固定分享只能更新原书籍和章节范围，请为其他范围创建新分享")
            now = utc_now_iso()
            item = dict(id=publication_id, title=title, doc_id=doc_id,
                        chapter_id=chapter_id, html=html, revision=(old or {}).get("revision", 0) + 1,
                        created_at=(old or {}).get("created_at", now), updated_at=now, revoked=False)
            atomic_write_bytes(path, json.dumps(item, ensure_ascii=False).encode("utf-8"))
        return self.summary(item)

    def revoke(self, publication_id: str, base_revision: int) -> dict:
        path = self.path(publication_id)
        self.read(publication_id)  # Return 404 before trying to lock an absent directory.
        with FileLock(str(path) + ".lock", timeout=10):
            item = self.read(publication_id)
            if item["revision"] != base_revision:
                raise ConflictError("分享已更新，请刷新后重试")
            item.update(revoked=True, html="", revision=item["revision"] + 1,
                        updated_at=utc_now_iso())
            atomic_write_bytes(path, json.dumps(item, ensure_ascii=False).encode("utf-8"))
        return self.summary(item)
