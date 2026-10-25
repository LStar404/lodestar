"""全书生成「跳过已生成章节」回归：complete 报告 + 锚点指纹未变 → 不重烧 LLM。"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from src.api.main import create_app
from src.core.store import DocStore


def _write_report(layout, doc_id: str, chapter_id: str, *, status: str, outline_hash: str,
                  **extra):
    d = layout.reports_doc_dir(doc_id)
    d.mkdir(parents=True, exist_ok=True)
    body = {"doc_id": doc_id, "chapter_id": chapter_id, "status": status,
            "outline_hash": outline_hash}
    body.update(extra)
    (d / f"{chapter_id}.generate.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8")


def _run_generate(client, headers, doc_id, body):
    resp = client.post(f"/api/v1/docs/{doc_id}/generate", json=body, headers=headers)
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]
    for _ in range(200):
        t = client.get(f"/api/v1/tasks/{task_id}", headers=headers).json()
        if t["status"] not in ("queued", "running"):
            return t
        time.sleep(0.05)
    raise AssertionError("任务超时")


def test_whole_book_skips_completed_chapters(sample_doc, monkeypatch):
    layout, doc_id = sample_doc
    manifest = DocStore(layout, doc_id).manifest()
    chapters = [c["chapter_id"] for c in manifest["chapters"]]
    assert len(chapters) >= 2, "样本书至少两章"
    done_ch = chapters[0]
    hash0 = next(c for c in manifest["chapters"] if c["chapter_id"] == done_ch).get("outline_hash") or ""
    _write_report(layout, doc_id, done_ch, status="complete", outline_hash=hash0)

    called: list[str] = []

    def fake_generate(doc_id_, ch, **kw):
        called.append(ch)
        return {"doc_id": doc_id_, "chapter_id": ch, "status": "complete",
                "cache_hit": False, "outline_complete": True, "created_at": "t",
                "model": {}, "failures": [], "created": [], "updated": []}

    monkeypatch.setattr("src.generate.engine.generate_chapter", fake_generate)
    app = create_app(layout.root)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        headers = {"X-Local-Token": app.state.app_state.token_value}
        t = _run_generate(client, headers, doc_id, {"skip_done": True})
        assert t["status"] == "succeeded", t
    assert done_ch not in called, f"已生成完的章节不得重烧: {called}"
    assert set(called) == set(chapters) - {done_ch}, called


def test_stale_report_not_skipped_and_single_chapter_never_skips(sample_doc, monkeypatch):
    """锚点指纹变了（重锚定）→ 旧 complete 报告不得跳过；单章点击是显式意图不跳。"""
    layout, doc_id = sample_doc
    manifest = DocStore(layout, doc_id).manifest()
    chapters = [c["chapter_id"] for c in manifest["chapters"]]
    done_ch = chapters[0]
    _write_report(layout, doc_id, done_ch, status="complete", outline_hash="sha256:old-fingerprint")

    called: list[str] = []

    def fake_generate(doc_id_, ch, **kw):
        called.append(ch)
        return {"doc_id": doc_id_, "chapter_id": ch, "status": "complete",
                "cache_hit": False, "outline_complete": True, "created_at": "t",
                "model": {}, "failures": [], "created": [], "updated": []}

    monkeypatch.setattr("src.generate.engine.generate_chapter", fake_generate)
    app = create_app(layout.root)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        headers = {"X-Local-Token": app.state.app_state.token_value}
        _run_generate(client, headers, doc_id, {"skip_done": True})
        assert done_ch in called, f"指纹变了必须重生成: {called}"
        called.clear()
        _run_generate(client, headers, doc_id, {"chapter_id": done_ch})
        assert called == [done_ch], f"单章点击不得跳过: {called}"


def test_partial_outline_report_not_skipped_by_whole_book(sample_doc, monkeypatch):
    """Review #3：提纲不完整（partial，含 outline_complete=False）的报告不得让全书生成跳过该章。"""
    layout, doc_id = sample_doc
    manifest = DocStore(layout, doc_id).manifest()
    chapters = [c["chapter_id"] for c in manifest["chapters"]]
    target = chapters[0]
    hash0 = next(c for c in manifest["chapters"]
                 if c["chapter_id"] == target).get("outline_hash") or ""
    _write_report(layout, doc_id, target, status="partial", outline_hash=hash0)

    called: list[str] = []

    def fake_generate(doc_id_, ch, **kw):
        called.append(ch)
        return {"doc_id": doc_id_, "chapter_id": ch, "status": "complete",
                "cache_hit": False, "outline_complete": True, "created_at": "t",
                "model": {}, "failures": [], "created": [], "updated": []}

    monkeypatch.setattr("src.generate.engine.generate_chapter", fake_generate)
    app = create_app(layout.root)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        headers = {"X-Local-Token": app.state.app_state.token_value}
        t = _run_generate(client, headers, doc_id, {"skip_done": True})
        assert t["status"] == "succeeded", t
    assert target in called, f"partial（提纲未完整）的章节不得被全书生成跳过: {called}"


def test_legacy_complete_report_with_incomplete_outline_not_skipped(sample_doc, monkeypatch):
    """Review #3 兼容面：修复前写下的「complete + outline_complete=false」报告不得跳过。

    旧代码在提纲缺批时仍把报告写成 complete（outline_complete 如实为 False），
    全书生成据此跳过会让缺失批次永远补不回来。skip 判定必须把该字段纳入，
    且旧报告没有该字段时按 True 处理（行为不变，见 test_whole_book_skips_completed_chapters）。
    """
    layout, doc_id = sample_doc
    manifest = DocStore(layout, doc_id).manifest()
    chapters = [c["chapter_id"] for c in manifest["chapters"]]
    target = chapters[0]
    hash0 = next(c for c in manifest["chapters"]
                 if c["chapter_id"] == target).get("outline_hash") or ""
    _write_report(layout, doc_id, target, status="complete", outline_hash=hash0,
                  outline_complete=False, outline_missing_batches=9)

    called: list[str] = []

    def fake_generate(doc_id_, ch, **kw):
        called.append(ch)
        return {"doc_id": doc_id_, "chapter_id": ch, "status": "complete",
                "cache_hit": False, "outline_complete": True, "created_at": "t",
                "model": {}, "failures": [], "created": [], "updated": []}

    monkeypatch.setattr("src.generate.engine.generate_chapter", fake_generate)
    app = create_app(layout.root)
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        headers = {"X-Local-Token": app.state.app_state.token_value}
        t = _run_generate(client, headers, doc_id, {"skip_done": True})
        assert t["status"] == "succeeded", t
    assert target in called, f"提纲未完整的旧 complete 报告不得被跳过: {called}"
