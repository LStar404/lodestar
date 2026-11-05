"""块 CRUD、历史、回滚、重排、审核队列、agent 编辑端点（9.2）。"""

from __future__ import annotations

import base64
import json

from fastapi import APIRouter, Request

from src.core.errors import ConflictError, InputError, NotFound
from src.core.paths import ANCHOR_ID_RE, DOC_ID_RE
from src.core.store import DocStore, sort_blocks

router = APIRouter()


def state(request: Request):
    return request.app.state.app_state


def _encode_cursor(row: dict, revision: int) -> str:
    raw = json.dumps({"order": row.get("order"), "id": row.get("id"), "data_revision": revision})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> dict:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        raise InputError("cursor 非法")


@router.get("/api/v1/docs/{doc_id}/chapters/{chapter_id}/blocks")
def list_blocks(request: Request, doc_id: str, chapter_id: str, include_deleted: int = 0,
                limit: int = 100, cursor: str | None = None, status: str | None = None):
    st = state(request)
    store = DocStore(st.layout, doc_id)
    revision = store.data_revision()
    limit = max(1, min(200, int(limit)))
    rows = store.block_rows(chapter_id, include_deleted=bool(include_deleted))
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if cursor:
        cur = _decode_cursor(cursor)
        if int(cur.get("data_revision", -1)) != revision:
            raise ConflictError("数据已变化，请重新取首页", code="CURSOR_STALE",
                                detail={"data_revision": revision})
        rows = [r for r in rows if (float(r["order"]), r["id"]) > (float(cur["order"]), cur["id"])]
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1], revision) if len(rows) > limit else None
    return {"items": page, "next_cursor": next_cursor, "data_revision": revision}


@router.post("/api/v1/docs/{doc_id}/chapters/{chapter_id}/blocks")
def create_block(request: Request, doc_id: str, chapter_id: str, body: dict):
    st = state(request)
    store = DocStore(st.layout, doc_id)
    author = body.pop("author", "human")
    # message 是历史备注而非块字段：不摘掉会被 dict(payload) 原样写进块记录
    message = body.pop("message", "新建块")
    block = store.create_block(body, author=author, message=message,
                               chapter_id=chapter_id)
    return block


@router.get("/api/v1/docs/{doc_id}/blocks")
def blocks_by_status(request: Request, doc_id: str, status: str | None = None):
    """待审核队列入口（?status=needs_review）。"""
    st = state(request)
    store = DocStore(st.layout, doc_id)
    rows = store.all_block_rows()
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return {"items": sort_blocks(rows)}


@router.get("/api/v1/docs/{doc_id}/block-counts")
def block_counts(request: Request, doc_id: str):
    return DocStore(state(request).layout, doc_id).block_counts()


@router.get("/api/v1/blocks/{block_id}")
def get_block(request: Request, block_id: str):
    st = state(request)
    store = _find_store(st, block_id)
    block = store.get_block(block_id)
    history = [{"rev_id": h["rev_id"], "author": h.get("author"), "message": h.get("message"),
                "created_at": h.get("created_at")} for h in store.history_rows(block_id)]
    return {"block": block, "history": history}


@router.post("/api/v1/docs/{doc_id}/anchors/batch")
def batch_anchors(request: Request, doc_id: str, body: dict):
    """一批最多 200 个来源，仅查指定教材，避免逐卡请求与全库扫描。"""
    ids = body.get("ids")
    if not isinstance(ids, list) or len(ids) > 200:
        raise InputError("ids 必须是最多 200 个锚点 ID 的数组")
    if any(not isinstance(key, str) or not ANCHOR_ID_RE.fullmatch(key) for key in ids):
        raise InputError("ids 包含非法锚点 ID")
    st = state(request)
    revision, found = DocStore(st.layout, doc_id).get_anchors(ids)
    return {"items": list(found.values()), "data_revision": revision,
            "missing": [key for key in dict.fromkeys(ids) if key not in found]}


@router.get("/api/v1/docs/{doc_id}/block-titles")
def block_titles(request: Request, doc_id: str):
    """知识点链接只需要标题，无须下载全书正文。"""
    store = DocStore(state(request).layout, doc_id)
    return {"items": store.block_titles()}


@router.patch("/api/v1/blocks/{block_id}")
def patch_block(request: Request, block_id: str, body: dict):
    st = state(request)
    store = _find_store(st, block_id)
    expected = body.pop("expected_revision", None)
    message = body.pop("message", "手动编辑")
    author = body.pop("author", "human")
    block = store.update_block(block_id, body, expected_revision=expected, author=author,
                               message=message)
    return block


@router.delete("/api/v1/blocks/{block_id}")
def delete_block(request: Request, block_id: str, body: dict | None = None):
    st = state(request)
    store = _find_store(st, block_id)
    body = body or {}
    return store.soft_delete_block(block_id, expected_revision=body.get("expected_revision"),
                                   author=body.get("author", "human"),
                                   message=body.get("message", "删除块"))


@router.post("/api/v1/blocks/{block_id}/restore")
def restore_block(request: Request, block_id: str, body: dict | None = None):
    st = state(request)
    store = _find_store(st, block_id)
    body = body or {}
    return store.restore_block(block_id, expected_revision=body.get("expected_revision"),
                               message=body.get("message", "恢复块"))


@router.post("/api/v1/blocks/{block_id}/rollback")
def rollback_block(request: Request, block_id: str, body: dict):
    st = state(request)
    store = _find_store(st, block_id)
    return store.rollback_block(block_id, body.get("rev_id", ""),
                                expected_revision=body.get("expected_revision"))


@router.post("/api/v1/blocks/{block_id}/allow-regeneration")
def allow_regeneration(request: Request, block_id: str, body: dict):
    st = state(request)
    if not body.get("confirmed"):
        raise InputError("需要 confirmed=true")
    store = _find_store(st, block_id)
    return store.allow_regeneration(block_id, expected_revision=body.get("expected_revision"))


@router.post("/api/v1/blocks/{block_id}/unlock")
def unlock_block(request: Request, block_id: str, body: dict | None = None):
    st = state(request)
    body = body or {}
    plan_id = body.get("plan_id", "")
    st.runtime.unlock_block(plan_id, block_id, ttl_hours=1)
    return {"block_id": block_id, "plan_id": plan_id, "unlocked": True}


@router.post("/api/v1/blocks/{block_id}/lock")
def lock_block(request: Request, block_id: str):
    st = state(request)
    return {"block_id": block_id, "removed": st.runtime.lock_block(block_id)}


@router.post("/api/v1/docs/{doc_id}/chapters/{chapter_id}/reorder")
def reorder(request: Request, doc_id: str, chapter_id: str, body: dict):
    st = state(request)
    store = DocStore(st.layout, doc_id)
    rows = store.reorder_block(body.get("block_id", ""), body.get("after"),
                               expected_revision=body.get("expected_revision"))
    return {"items": rows}


@router.post("/api/v1/blocks/{block_id}/agent-edit")
def agent_edit_block(request: Request, block_id: str, body: dict):
    return _agent_edit(request, body, block_id=block_id)


@router.post("/api/v1/docs/{doc_id}/chapters/{chapter_id}/agent-edit")
def agent_edit_chapter(request: Request, doc_id: str, chapter_id: str, body: dict):
    return _agent_edit(request, body, doc_id=doc_id, chapter_id=chapter_id)


def _agent_edit(request: Request, body: dict, *, block_id: str | None = None,
                doc_id: str | None = None, chapter_id: str | None = None):
    from src.api.agent_edit import execute_plan, plan_edit

    st = state(request)
    if block_id:
        store = _find_store(st, block_id)
        doc_id = store.get_block(block_id)["doc_id"]
    if body.get("mode") == "plan":
        return plan_edit(st, doc_id=doc_id, instruction=body.get("instruction", ""),
                         block_id=block_id, chapter_id=chapter_id)
    if body.get("mode") == "exec":
        return execute_plan(st, doc_id=doc_id, plan_id=body.get("plan_id", ""),
                            confirmed=bool(body.get("confirmed")))
    raise InputError("mode 必须是 plan 或 exec")


@router.get("/api/v1/anchors/{anchor_id}")
def get_anchor(request: Request, anchor_id: str):
    st = state(request)
    # 点查解析链（6.4）：runtime.doc_map → 命中则只读该 doc；未命中回退扫描
    doc_id = _lookup_doc(st, "anchor", anchor_id)
    if doc_id:
        try:
            return DocStore(st.layout, doc_id).get_anchor(anchor_id)
        except NotFound:
            pass  # 派生映射可能落后于删书/恢复；继续按事实源扫描。
    for doc_dir in sorted(st.layout.data_dir.glob("*/manifest.json")):
        cand = doc_dir.parent.name
        if not DOC_ID_RE.match(cand):
            continue
        try:
            return DocStore(st.layout, cand).get_anchor(anchor_id)
        except Exception:
            continue
    raise NotFound(f"锚点不存在: {anchor_id}")


def _lookup_doc(st, kind: str, key: str) -> str | None:
    try:
        from src.db.runtime import get_runtime

        return get_runtime(st.layout.runtime_db).lookup_doc(kind, key)
    except Exception:
        return None


def _find_store(st, block_id: str) -> DocStore:
    """block_id → doc 映射（6.4）：runtime.doc_map 优先，未命中回退扫描。"""
    doc_id = _lookup_doc(st, "block", block_id)
    if doc_id:
        store = DocStore(st.layout, doc_id)
        try:
            store.get_block(block_id)
            return store
        except NotFound:
            pass
    for doc_dir in sorted(st.layout.data_dir.glob("*/manifest.json")):
        cand = doc_dir.parent.name
        if not DOC_ID_RE.match(cand):
            continue
        try:
            store = DocStore(st.layout, cand)
            store.get_block(block_id)
            return store
        except Exception:
            continue
    raise NotFound(f"块不存在: {block_id}")
