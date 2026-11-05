"""Template snapshots preserve notes and participate in actual generation caching."""
from __future__ import annotations

import base64
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.routes.templates import router
from src.core.errors import InputError, RevisionConflict
from src.core.note_templates import (Cover, StyledClient, TemplateDraft, apply_template,
                                     book_template, generation_identity, save_template)
from src.core.store import DocStore
from src.generate.engine import generate_chapter
from tests.unit.test_generate_cache_identity import _cfg, _client, _fill, _outline, _registry


def test_apply_snapshot_keeps_notes_and_stays_pinned(sample_doc):
    layout, doc_id = sample_doc
    original = {p: p.read_bytes() for p in layout.doc_dir(doc_id).rglob("*.jsonl")}
    original_revision = DocStore(layout, doc_id).data_revision()
    saved = save_template(layout, TemplateDraft(name="精读", writing_hint="先给直觉"))
    applied = apply_template(layout, doc_id, saved["id"], saved["revision"])
    updated = save_template(layout, TemplateDraft(name="复习", writing_hint="条件优先"), saved["id"], 1)
    assert updated["revision"] == 2
    assert book_template(layout, doc_id)["name"] == "精读"
    assert all(p.read_bytes() == data for p, data in original.items())
    assert DocStore(layout, doc_id).data_revision() == original_revision + 1
    assert applied["revision"] == 1
    with pytest.raises(RevisionConflict):
        apply_template(layout, doc_id, saved["id"], 1)
    with pytest.raises(RevisionConflict):
        save_template(layout, TemplateDraft(name="过期编辑"), saved["id"], 1)


def test_bad_cover_and_path_rejected(workspace):
    for image in ("https://example.com/a.png", "data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,aGVsbG8="):
        with pytest.raises(ValidationError):
            Cover(image=image)
    # PNG header with a decompression-bomb-sized canvas must be rejected.
    huge = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (100_000).to_bytes(4, "big") * 2 + bytes(9)
    with pytest.raises(ValidationError):
        Cover(image="data:image/png;base64," + base64.b64encode(huge).decode())
    with pytest.raises(InputError):
        save_template(workspace, TemplateDraft(name="x"), "../../bad")


def test_cover_and_name_do_not_invalidate_generation():
    one = TemplateDraft(name="A").model_dump()
    two = deepcopy(one)
    two["cover"]["color"] = "#000000"
    two["name"] = "重命名"
    two["revision"] = 99
    assert generation_identity(one) == generation_identity(two)
    two["sections"] = ["只保留核心结论"]
    assert generation_identity(one) != generation_identity(two)


def test_wrapper_isolates_books_and_preserves_original_messages():
    class Fake:
        def complete(self, task, messages, **kwargs):
            return {"messages": messages, "kwargs": kwargs}
    client = Fake()
    messages = [{"role": "user", "content": "source"}]
    a = StyledClient(client, "book A").complete("fill", messages, model="test")
    b = StyledClient(client, "book B").complete("fill", messages)
    assert len(messages) == 1
    assert a["messages"][-1]["content"] == "book A"
    assert b["messages"][-1]["content"] == "book B"
    assert a["kwargs"]["model"] == "test"


def test_actual_generation_uses_template_and_visual_edits_reuse_cache(sample_doc):
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchor = store.anchor_rows("ch01")[1]
    cfg = _cfg()
    saved = save_template(layout, TemplateDraft(name="精读", writing_hint="先给直觉"))
    apply_template(layout, doc_id, saved["id"], 1)
    client = _client(layout, registry, cfg, [_outline(anchor), _fill(anchor, "定义正文。")])
    first = generate_chapter(doc_id, "ch01", layout=layout, client=client, config=cfg, registry=registry)
    assert first["status"] == "complete", first
    assert any("先给直觉" in str(call["messages"]) for call in client.overrides["dummy"].calls)

    changed = TemplateDraft(name="精读新版", writing_hint="先给直觉", cover=Cover(color="#112233"))
    save_template(layout, changed, saved["id"], 1)
    apply_template(layout, doc_id, saved["id"], 2)
    cached_client = _client(layout, registry, cfg, [])
    again = generate_chapter(doc_id, "ch01", layout=layout, client=cached_client, config=cfg, registry=registry)
    assert again["cache_hit"] is True
    assert cached_client.stats["calls"] == 0

    changed.writing_hint = "按条件与结论组织"
    save_template(layout, changed, saved["id"], 2)
    apply_template(layout, doc_id, saved["id"], 3)
    new_client = _client(layout, registry, cfg, [_outline(anchor), _fill(anchor, "新的定义正文。")])
    fresh = generate_chapter(doc_id, "ch01", layout=layout, client=new_client, config=cfg, registry=registry)
    assert fresh["cache_hit"] is False
    assert new_client.stats["calls"] >= 2


def test_discussion_is_real_call_but_never_saves_and_carries_history(workspace):
    draft = TemplateDraft(name="阅读").model_dump()
    calls = []
    class Fake:
        def route(self, task):
            return "test/model"
        def complete(self, task, messages, **kwargs):
            calls.append(messages)
            return {"output": {"reply": "可以，把易错点放在最后。", "draft": {**draft, "sections": ["概念", "易错点"]}}}
    app = FastAPI()
    app.state.app_state = SimpleNamespace(layout=workspace, client=Fake(), config={})
    app.include_router(router)
    client = TestClient(app)
    response = client.post("/api/v1/templates/discuss", json={"draft": draft, "messages": [
        {"role": "user", "content": "适合复习"}, {"role": "assistant", "content": "先列概念。"},
        {"role": "user", "content": "再加易错点"}]})
    assert response.status_code == 200, response.text
    assert response.json()["draft"]["sections"] == ["概念", "易错点"]
    assert calls[0][-1]["content"] == "再加易错点"
    assert calls[0][-2]["content"] == "先列概念。"
    assert not (workspace.data_dir / "note-templates").exists()


def test_unconfigured_model_fails_clearly(workspace):
    from src.api.main import create_app
    app = create_app(workspace.root)
    app.include_router(router)
    st = app.state.app_state
    client = TestClient(app, base_url="http://127.0.0.1:8787", headers={"X-Local-Token": st.token_value})
    r = client.post("/api/v1/templates/discuss", json={"draft": TemplateDraft(name="阅读").model_dump(),
                                                       "messages": [{"role": "user", "content": "帮我设计"}]})
    assert r.status_code >= 400
    assert r.json()["error"] == "NO_MODEL"
    assert not (workspace.data_dir / "note-templates").exists()


def test_template_api_roundtrip_and_conflict(sample_doc):
    from src.api.main import create_app
    layout, doc_id = sample_doc
    app = create_app(layout.root)
    if not any(getattr(route, "path", "") == "/api/v1/templates" for route in app.routes):
        app.include_router(router)
    st = app.state.app_state
    client = TestClient(app, base_url="http://127.0.0.1:8787", headers={"X-Local-Token": st.token_value})
    draft = TemplateDraft(name="我的案例模板").model_dump()
    draft["type_ids"] = ["definition"]
    saved = client.post("/api/v1/templates", json=draft)
    assert saved.status_code == 200, saved.text
    saved = saved.json()
    assert saved["revision"] == 1
    assert len(client.get("/api/v1/templates").json()["items"]) == 1
    applied = client.put(f"/api/v1/docs/{doc_id}/template", json={"template_id": saved["id"], "revision": 1})
    assert applied.status_code == 200, applied.text
    changed = client.put(f"/api/v1/templates/{saved['id']}", json={"revision": 1, "draft": {**draft, "name": "修改后"}})
    assert changed.status_code == 200
    assert client.put(f"/api/v1/templates/{saved['id']}", json={"revision": 1, "draft": draft}).status_code == 409
    assert client.get(f"/api/v1/docs/{doc_id}/template").json()["template"]["name"] == "我的案例模板"
    assert client.post("/api/v1/templates", json={**draft, "type_ids": ["invented_unknown"]}).status_code == 400
    assert client.post("/api/v1/templates", json={**draft, "sections": []}).status_code == 422
    assert client.post("/api/v1/templates", json={**draft, "cover": {"color": "red;url(javascript:bad)"}}).status_code == 422
