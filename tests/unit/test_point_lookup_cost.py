"""Point reads must not copy an entire book, and revision changes invalidate the memo."""

from types import SimpleNamespace

import pytest

from src.api.routes import blocks as routes
from src.core import store as store_module
from src.core.errors import NotFound
from src.core.jsonl import rewrite_json, rewrite_jsonl
from src.core.store import DocStore


@pytest.fixture
def lookup_book(workspace):
    doc_id = "lookup-book"
    rewrite_json(workspace.manifest(doc_id), {
        "doc_id": doc_id, "data_revision": 1,
        "chapters": [{"chapter_id": "ch01"}, {"chapter_id": "ch02"}],
    })
    for chapter, offset in (("ch01", 0), ("ch02", 200)):
        rewrite_jsonl(workspace.anchors(doc_id, chapter), [
            {"anchor_id": f"a_{i}", "chapter_id": chapter, "content": f"Source {i}",
             "bbox": [0, 0, 10, 10], "note_block_ids": [f"b_{i}"]}
            for i in range(offset, offset + 200)
        ])
        rewrite_jsonl(workspace.blocks(doc_id, chapter), [
            {"id": f"b_{i}", "chapter_id": chapter, "title": f"Note {i}",
             "order": float(i), "sources": [], "links": [], "deleted_at": None}
            for i in range(offset, offset + 200)
        ])
    return DocStore(workspace, doc_id)


def _state(store):
    return SimpleNamespace(layout=store.layout)


def _request(store):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(app_state=_state(store))))


def _count_clones(monkeypatch):
    calls = []
    original = store_module._clone_row

    def counted(row):
        calls.append(row)
        return original(row)

    monkeypatch.setattr(store_module, "_clone_row", counted)
    return calls


def test_warm_anchor_endpoint_copies_only_requested_row(lookup_book, monkeypatch):
    monkeypatch.setattr(routes, "_lookup_doc", lambda *_: lookup_book.doc_id)
    request = _request(lookup_book)
    assert routes.get_anchor(request, "a_250")["content"] == "Source 250"
    calls = _count_clones(monkeypatch)
    assert routes.get_anchor(request, "a_250")["content"] == "Source 250"
    assert len(calls) == 1, "Point lookup copied unrelated book contents"


def test_warm_block_resolver_copies_only_requested_row(lookup_book, monkeypatch):
    monkeypatch.setattr(routes, "_lookup_doc", lambda *_: lookup_book.doc_id)
    routes._find_store(_state(lookup_book), "b_250")
    calls = _count_clones(monkeypatch)
    assert routes._find_store(_state(lookup_book), "b_250").doc_id == lookup_book.doc_id
    assert len(calls) == 1, "Block location rebuilt an entire book mapping"


def test_anchor_result_does_not_mutate_cached_coordinates(lookup_book):
    first = lookup_book.get_anchor("a_250")
    first["bbox"][0] = 999
    first["note_block_ids"].append("injected")
    fresh = lookup_book.get_anchor("a_250")
    assert fresh["bbox"] == [0, 0, 10, 10]
    assert fresh["note_block_ids"] == ["b_250"]
    mapping = lookup_book.anchors_by_id()
    mapping["a_250"]["bbox"][0] = 888
    assert lookup_book.get_anchor("a_250")["bbox"][0] == 0


def test_revision_change_reloads_anchor_lookup(lookup_book):
    assert lookup_book.get_anchor("a_250")["content"] == "Source 250"
    rows = lookup_book.anchor_rows("ch02")
    rows[50]["content"] = "New source"
    rewrite_jsonl(lookup_book.layout.anchors(lookup_book.doc_id, "ch02"), rows)
    manifest = dict(lookup_book.manifest())
    manifest["data_revision"] = 2
    rewrite_json(lookup_book.layout.manifest(lookup_book.doc_id), manifest)
    assert lookup_book.get_anchor("a_250")["content"] == "New source"


def test_warm_missing_anchor_and_block_do_not_copy_book(lookup_book, monkeypatch):
    lookup_book.get_anchor("a_250")
    lookup_book.get_block("b_250")
    calls = _count_clones(monkeypatch)
    for _ in range(2):
        with pytest.raises(NotFound):
            lookup_book.get_anchor("missing")
        with pytest.raises(NotFound):
            lookup_book.get_block("missing")
    assert calls == []


def test_anchor_endpoint_falls_back_when_doc_map_is_stale(lookup_book, monkeypatch):
    monkeypatch.setattr(routes, "_lookup_doc", lambda *_: "missing-book")
    assert routes.get_anchor(_request(lookup_book), "a_250")["content"] == "Source 250"
