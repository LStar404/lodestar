"""教材改名回归：只改 manifest.title，走提交机制（单写者/CAS）。"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from src.api.main import create_app
from src.core.store import DocStore
from src.db.reindex import ensure_index_fresh, index_meta, reindex_doc


def test_rename_doc_updates_title(sample_doc):
    layout, doc_id = sample_doc
    app = create_app(layout.root)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        headers = {"X-Local-Token": app.state.app_state.token_value}
        r = client.post(f"/api/v1/docs/{doc_id}/rename", json={"title": "新书名"}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "新书名"
        man = client.get(f"/api/v1/docs/{doc_id}", headers=headers).json()
        assert man["title"] == "新书名", man
        assert DocStore(layout, doc_id).manifest()["title"] == "新书名"
        r2 = client.post(f"/api/v1/docs/{doc_id}/rename", json={"title": "   "}, headers=headers)
        assert r2.status_code >= 400, "空名必须拒绝"
        r3 = client.post("/api/v1/docs/doc-nope/rename", json={"title": "x"}, headers=headers)
        assert r3.status_code == 404, r3.text


def _fts_count(layout, doc_id: str, query: str) -> int:
    conn = sqlite3.connect(str(layout.index_db(doc_id)))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM anchors_fts WHERE anchors_fts MATCH ?", (query,)).fetchone()[0]
    finally:
        conn.close()


def test_rename_doc_keeps_anchors_fts(sample_doc):
    """改名只动 manifest：不得清空全文索引（Review #8）。

    基线复现：改名前 `matrix AND scalar` 命中 1 条，改名后 0 条；index_meta 指纹
    已更新，ensure_index_fresh() 也不再重建 → 检索永久变空。根因是 blocks_only=True
    且章节范围为空（manifest-only 提交）仍执行 anchors_fts 'delete-all'，而 FTS 重建
    只在不带 blocks_only 的分支里跑。
    """
    layout, doc_id = sample_doc
    reindex_doc(layout, doc_id)                     # 全量重建：FTS 建好
    query = "matrix AND scalar"
    before = _fts_count(layout, doc_id, query)
    assert before >= 1, "样本正文应能命中 matrix/scalar（FTS 未建成？）"
    meta_before = index_meta(layout, doc_id)
    assert meta_before and meta_before["source_hash"]

    app = create_app(layout.root)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        headers = {"X-Local-Token": app.state.app_state.token_value}
        r = client.post(f"/api/v1/docs/{doc_id}/rename", json={"title": "改名后的书名"},
                        headers=headers)
        assert r.status_code == 200, r.text

    # 1) 改名后检索命中数必须与改名前一致（核心断言：不得清空 FTS）
    assert _fts_count(layout, doc_id, query) == before, \
        "改名清空了 anchors_fts：改名前命中 %d，改名后 %d" % (before, _fts_count(layout, doc_id, query))
    # 2) 块索引更新路径不该动锚点段指纹；ensure_index_fresh 不应靠全量重建兜底
    assert ensure_index_fresh(layout, doc_id) is False, \
        "改名后指纹失配触发了全量重建（说明 blocks_only 快路径写坏了指纹）"
    assert _fts_count(layout, doc_id, query) == before
    meta_after = index_meta(layout, doc_id)
    assert meta_after and meta_after["source_hash"] == meta_before["source_hash"]
