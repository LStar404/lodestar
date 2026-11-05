from types import SimpleNamespace

import pytest

from src.api.routes.blocks import batch_anchors, block_titles
from src.core.errors import InputError
from src.core.store import DocMutation


def request_for(store):
    state = SimpleNamespace(layout=store.layout)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(app_state=state)))


def test_anchor_lookup_reuses_index_and_returns_isolated_rows(store, monkeypatch):
    anchors = store.all_anchor_rows()
    ids = [a["anchor_id"] for a in anchors]
    original = store.all_anchor_rows
    scans = []

    def scan():
        scans.append(True)
        return original()

    monkeypatch.setattr(store, "all_anchor_rows", scan)
    _, found = store.get_anchors(ids)
    expected = list(found[ids[0]]["bbox"])
    found[ids[0]]["bbox"][0] = -999
    found[ids[0]]["note_block_ids"].append("not-a-real-block")
    for anchor_id in ids * 5:
        _, result = store.get_anchors([anchor_id])
        assert result[anchor_id]["bbox"][0] != -999
        assert result[anchor_id]["note_block_ids"] == []
    assert len(scans) == 1
    assert store.all_anchor_rows()[0]["bbox"] == expected


def test_anchor_index_refreshes_after_commit(store):
    rows = store.anchor_rows("ch01")
    anchor_id = rows[0]["anchor_id"]
    old_revision, _ = store.get_anchors([anchor_id])
    rows[0]["bbox"][0] += 0.1
    store.commit(DocMutation(anchors={"ch01": rows}))
    revision, result = store.get_anchors([anchor_id])
    assert revision > old_revision
    assert result[anchor_id]["bbox"] == rows[0]["bbox"]


def test_batch_preserves_unique_order_and_reports_missing(store):
    ids = [a["anchor_id"] for a in store.all_anchor_rows()]
    missing = "a_00000000_0000000000000000"
    result = batch_anchors(request_for(store), store.doc_id, {"ids": [ids[-1], ids[0], ids[-1], missing]})
    assert [a["anchor_id"] for a in result["items"]] == [ids[-1], ids[0]]
    assert result["missing"] == [missing]
    assert result["data_revision"] == store.data_revision()
    assert all(a["doc_id"] == store.doc_id for a in result["items"])


@pytest.mark.parametrize("ids", [None, "id", [None], ["../outside"], ["a_00000000_0000000000000000"] * 201])
def test_batch_rejects_invalid_or_oversized_requests(store, ids):
    with pytest.raises(InputError):
        batch_anchors(request_for(store), store.doc_id, {"ids": ids})


def test_title_index_omits_content_and_deleted_notes(store):
    payload = {"type": "motivation", "title": "Keep", "content": "long content" * 1000,
               "basis": "synthesis", "order": 1}
    kept = store.create_block(payload, author="ai", chapter_id="ch01")
    removed = store.create_block({**payload, "title": "Removed", "order": 2}, author="ai", chapter_id="ch01")
    store.soft_delete_block(removed["id"])
    assert block_titles(request_for(store), store.doc_id) == {"items": [{"id": kept["id"], "title": "Keep"}]}


def test_warm_block_lookup_does_not_clone_the_entire_book(store, monkeypatch):
    from src.api.routes.blocks import get_block
    from src.core.store import DocStore

    block = store.create_block({"type": "motivation", "title": "One", "content": "Text",
                                "basis": "synthesis", "order": 1}, author="ai", chapter_id="ch01")
    store.get_block(block["id"])

    def unexpected_full_copy(*args, **kwargs):
        raise AssertionError("warm point lookup must not clone the complete book")

    monkeypatch.setattr(DocStore, "blocks_by_id", unexpected_full_copy)
    monkeypatch.setattr("src.api.routes.blocks._lookup_doc", lambda *args: store.doc_id)
    assert get_block(request_for(store), block["id"])["block"]["title"] == "One"
