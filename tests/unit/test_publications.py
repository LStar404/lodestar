import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.api.public_reader import create_reader
from src.core.errors import ConflictError, NotFound
from src.core.publications import PublicationStore


def test_durable_stable_update_revoke_and_reader_isolation(tmp_path):
    store = PublicationStore(tmp_path)
    item = store.save(title="书", html="<h1>v1</h1>", doc_id="test-book", chapter_id=None)
    sid = item["id"]
    reader = TestClient(create_reader(tmp_path))
    assert reader.get("/s/" + sid).text == "<h1>v1</h1>"
    assert "sandbox" in reader.get("/s/" + sid).headers["content-security-policy"]
    # Reconstruct both store and app, proving the URL survives restarts.
    restarted = PublicationStore(tmp_path)
    updated = restarted.save(title="书", html="v2", doc_id="test-book", chapter_id=None,
                             publication_id=sid, base_revision=1)
    assert updated["id"] == sid and updated["revision"] == 2
    assert TestClient(create_reader(tmp_path)).get("/s/" + sid).text == "v2"
    with pytest.raises(ConflictError):
        restarted.revoke(sid, 1)
    restarted.revoke(sid, 2)
    assert reader.get("/s/" + sid).status_code == 404
    assert "html" not in restarted.list()[0]
    assert restarted.read(sid)["html"] == ""
    with pytest.raises(ConflictError):
        restarted.save(title="书", html="v3", doc_id="test-book", chapter_id=None,
                       publication_id=sid, base_revision=3)
    for path in ("/api/v1/docs", "/api/v1/publications", "/docs", "/openapi.json", "/"):
        assert reader.get(path).status_code == 404
    assert reader.post("/s/" + sid, json={}).status_code == 405


def test_management_requires_local_token(workspace):
    app = create_app(workspace.root)
    client = TestClient(app, base_url="http://127.0.0.1:8787")
    body = dict(title="书", html="<p>hello</p>", doc_id="test-book")
    assert client.post("/api/v1/publications", json=body).status_code == 403
    headers = {"X-Local-Token": app.state.app_state.token_value}
    response = client.post("/api/v1/publications", json=body, headers=headers)
    assert response.status_code == 200, response.text
    sid = response.json()["id"]
    assert client.put("/api/v1/publications/" + sid, json=body, headers=headers).status_code == 409
    assert client.get("/api/v1/publications", headers=headers).json()["deployment"] == "not_configured"
    assert client.request("DELETE", "/api/v1/publications/" + sid, json={"base_revision":1}, headers=headers).status_code == 200


def test_reader_rejects_invalid_id_and_does_not_create_storage(tmp_path):
    root = tmp_path / "absent"
    reader = TestClient(create_reader(root))
    assert reader.get("/s/invalid").status_code == 404
    assert not root.exists()


def test_nonexistent_revoke_is_not_found_without_creating_storage(tmp_path):
    directory = tmp_path / "absent"
    with pytest.raises(NotFound):
        PublicationStore(directory).revoke("a" * 32, 1)
    assert not directory.exists()


def test_fixed_link_cannot_silently_change_book_or_scope(tmp_path):
    store = PublicationStore(tmp_path)
    item = store.save(title="书", html="v1", doc_id="test-book", chapter_id="ch01")
    for doc_id, chapter_id in (("other-book", "ch01"), ("test-book", None)):
        with pytest.raises(ConflictError):
            store.save(title="Other", html="v2", doc_id=doc_id, chapter_id=chapter_id,
                       publication_id=item["id"], base_revision=1)
    assert store.read(item["id"])["html"] == "v1"


@pytest.mark.parametrize("payload", ['{', '[]', '{}', '{"revoked": false}'])
def test_corrupt_snapshot_is_unavailable_without_breaking_other_links(tmp_path, payload):
    store = PublicationStore(tmp_path)
    healthy = store.save(title="书", html="<p>ok</p>", doc_id="test-book", chapter_id=None)
    broken_id = "a" * 32
    (tmp_path / (broken_id + ".json")).write_text(payload, encoding="utf-8")
    reader = TestClient(create_reader(tmp_path))
    assert reader.get("/s/" + broken_id).status_code == 404
    assert reader.get("/s/" + healthy["id"]).text == "<p>ok</p>"
    assert [item["id"] for item in store.list()] == [healthy["id"]]


def test_explicit_unknown_id_cannot_create_an_update(tmp_path):
    store = PublicationStore(tmp_path)
    with pytest.raises(NotFound):
        store.save(title="书", html="v1", doc_id="test-book", chapter_id=None,
                   publication_id="a" * 32)
    assert store.list() == []
