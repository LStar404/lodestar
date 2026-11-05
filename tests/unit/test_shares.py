"""分享包（.ngshare）回归：导入只读收录、幂等、坏包说清原因。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.api.routes.shares import validate_share
from src.core.errors import InputError

PAYLOAD = {
    "format": "note-gen-share",
    "version": 1,
    "created_at": "2026-10-19T00:00:00Z",
    "title": "线性代数 · 第4章",
    "chapter_title": "Chapter 4",
    "blocks": [
        {"type": "theorem", "title": "线性变换的定义",
         "content": "设 $T:V\\to W$，若 $T(\\alpha x+\\beta y)=\\alpha T(x)+\\beta T(y)$ 则 $T$ 线性。",
         "sources": [{"page": 211, "quote": "线性变换"}]},
        {"type": "motivation", "title": "", "content": "直观理解：保持向量加法的映射。",
         "sources": []},
    ],
    "figures": {"fig_abc": "data:image/png;base64,iVBORw0KGgo="},
}


@pytest.mark.parametrize("changes", [
    {"version": 1.9},
    {"version": float("inf")},
    {"blocks": [{"content": "x", "sources": {"page": 1}}]},
    {"blocks": [{"content": "x", "sources": [{"page": "²"}]}]},
    {"blocks": [{"content": "\ud800"}]},
    {"figures": {"[": "data:image/png;base64,aA=="}},
    {"figures": {"fig_00000001": "data:image/png;base64,aA==\n"}},
])
def test_malformed_share_fields_are_rejected_as_input_errors(changes):
    with pytest.raises(InputError):
        validate_share({**PAYLOAD, **changes})


def _app(workspace):
    return create_app(workspace.root)


def _upload(client, headers, payload) -> any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return client.post("/api/v1/shares", files={"file": ("x.ngshare", body, "application/json")},
                       headers=headers)


def test_upload_list_get_delete_roundtrip(workspace):
    app = _app(workspace)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        h = {"X-Local-Token": app.state.app_state.token_value}
        r = _upload(client, h, PAYLOAD)
        assert r.status_code == 200, r.text
        sid = r.json()["share"]["share_id"]
        assert sid.startswith("share-")
        assert r.json()["share"]["blocks"] == 2

        lst = client.get("/api/v1/shares", headers=h).json()["items"]
        assert [x["share_id"] for x in lst] == [sid]
        assert lst[0]["title"] == "线性代数 · 第4章"

        got = client.get(f"/api/v1/shares/{sid}", headers=h).json()
        assert got["format"] == "note-gen-share"
        assert got["blocks"][0]["title"] == "线性变换的定义"
        assert got["blocks"][0]["sources"][0]["page"] == 211
        assert got["figures"]["fig_abc"].startswith("data:image/png;base64,")

        assert client.delete(f"/api/v1/shares/{sid}", headers=h).status_code == 200
        assert client.get("/api/v1/shares", headers=h).json()["items"] == []
        assert client.get(f"/api/v1/shares/{sid}", headers=h).status_code == 404


def test_upload_is_idempotent_by_content(workspace):
    app = _app(workspace)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        h = {"X-Local-Token": app.state.app_state.token_value}
        a = _upload(client, h, PAYLOAD).json()["share"]["share_id"]
        b = _upload(client, h, PAYLOAD).json()["share"]["share_id"]
        assert a == b, "同一个包重复导入不应产生副本"
        # 导出时间不同（同一份笔记重新导出）也必须是同一条：否则收件箱会堆副本
        again = dict(PAYLOAD, created_at="2026-10-20T09:00:00Z")
        c = _upload(client, h, again).json()["share"]["share_id"]
        assert c == a, "内容相同、导出时间不同 → 仍应去重"
        assert len(client.get("/api/v1/shares", headers=h).json()["items"]) == 1


def test_reject_bad_packages(workspace):
    app = _app(workspace)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        h = {"X-Local-Token": app.state.app_state.token_value}
        bad_cases = [
            {"format": "other", "version": 1, "blocks": [{"content": "x"}]},
            {"format": "note-gen-share", "version": 99, "blocks": [{"content": "x"}]},
            {"format": "note-gen-share", "version": 1, "blocks": []},
            {"format": "note-gen-share", "version": 1, "blocks": [{"content": "x"}],
             "figures": {"f": "http://evil/x.png"}},
        ]
        for payload in bad_cases:
            r = _upload(client, h, payload)
            assert r.status_code == 400, (payload, r.status_code, r.text)
        r = client.post("/api/v1/shares",
                        files={"file": ("x.ngshare", b"not json", "application/json")},
                        headers=h)
        assert r.status_code == 400
        assert client.get("/api/v1/shares/share-000000000000", headers=h).status_code == 404
        assert client.get("/api/v1/shares/..%2Fconfig", headers=h).status_code == 404
