"""追加讲义事务回归（Review #1）：章节身份保持 + 失败零改动 + 并发冲突。

主场景复现 Review 的原始问题：原教材无书签按正文分为 ch01/ch02，在 ch02 建人工笔记；
追加一份带 "Chapter 3 New Lecture" 书签的 PDF。旧实现直接覆盖真实 PDF/重解析，
manifest 从两章变一章、旧笔记不可达；修复后旧章身份与笔记原样保留，新页得到新章。

原子性：锚定第 2 章时抛 PipelineError → 真实 PDF/manifest/块/锚点逐字节不变，
且不残留 data/.append-staging-*（暂存）与 data/.bak-append-*（备份）。
并发：swap 前真实 data_revision 变化 → ConflictError（任务失败并提示重试），
并发写入本身不丢、追加不生效。
"""

from __future__ import annotations

import copy
import io
import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.anchor.chunker import chunk_and_anchor
from src.api.main import create_app
from src.core.store import DocStore
from src.ingest.parse_pdf import parse_pdf

DOC_ID = "doc-append-atomic"

OLD_PAGES: list[list[tuple[str, float, float, float]]] = [
    [
        ("Chapter 1 Old Notes", 18, 72, 720),
        ("The old material in chapter one stays byte identical after the append.", 11, 72, 680),
        ("Definition: an old definition that anchors chapter one.", 11, 72, 650),
    ],
    [
        ("Chapter 2 Old Notes", 18, 72, 720),
        ("The manual note written here must survive the append.", 11, 72, 680),
        ("Theorem: the old theorem that the manual note cites.", 11, 72, 650),
    ],
]

NEW_PAGES: list[list[tuple[str, float, float, float]]] = [
    [
        ("New lecture material about fresh topics.", 11, 72, 680),
        ("Definition: a fresh definition on the appended page.", 11, 72, 650),
    ],
]


# ---------------------------------------------------------------------------
# 确定性 PDF 构造（可带 pypdf 能读的书签；tests/fixtures 的样本书签缺 /First，
# pypdf 读不到 —— Review 场景依赖书签切换分章策略，必须用可读书签复现）
# ---------------------------------------------------------------------------
def build_pdf(path: Path, pages, outlines: list[tuple[str, int]] | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    objs: dict[int, bytes] = {}
    counter = {"n": 1}

    def alloc() -> int:
        num = counter["n"]
        counter["n"] += 1
        return num

    catalog_id = alloc()
    pages_id = alloc()
    font_id = alloc()
    content_ids = [alloc() for _ in pages]
    page_ids = [alloc() for _ in pages]
    outline_ids: list[int] = []
    outlines_id = None
    if outlines:
        outlines_id = alloc()
        outline_ids = [alloc() for _ in outlines]

    objs[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for idx, lines in enumerate(pages):
        ops = []
        for text, size, x, y in lines:
            safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"BT /F1 {size} Tf {x} {y} Td ({safe}) Tj ET")
        stream = "\n".join(ops).encode("latin-1")
        objs[content_ids[idx]] = (f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
                                  + stream + b"\nendstream")
    for idx, pid in enumerate(page_ids):
        objs[pid] = (
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_ids[idx]} 0 R >>").encode("latin-1")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[pages_id] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")
    if outlines:
        for i, (title, page_no) in enumerate(outlines):
            target = page_ids[page_no - 1]
            nxt = (f"/Next {outline_ids[i + 1]} 0 R " if i + 1 < len(outlines) else "")
            prev = (f"/Prev {outline_ids[i - 1]} 0 R " if i > 0 else "")
            objs[outline_ids[i]] = (
                f"<< /Title ({title}) /Parent {outlines_id} 0 R {nxt}{prev}"
                f"/Dest [{target} 0 R /XYZ null null null] >>").encode("latin-1")
        outline_kids = " ".join(f"{n} 0 R" for n in outline_ids)
        objs[outlines_id] = (f"<< /Type /Outlines /First {outline_ids[0]} 0 R "
                             f"/Last {outline_ids[-1]} 0 R /Kids [{outline_kids}] "
                             f"/Count {len(outline_ids)} >>").encode("latin-1")
        objs[catalog_id] = (f"<< /Type /Catalog /Pages {pages_id} 0 R "
                            f"/Outlines {outlines_id} 0 R >>").encode("latin-1")
    else:
        objs[catalog_id] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1")

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref_pos = len(out)
    max_num = max(objs) + 1
    out += f"xref\n0 {max_num}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, max_num):
        out += (f"{offsets[num]:010d} 00000 n \n".encode() if num in offsets
                else b"0000000000 65535 f \n")
    out += (f"trailer\n<< /Size {max_num} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))
    return path


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------
def _make_old_doc(workspace) -> dict:
    """旧教材：无书签、按正文标题分 ch01/ch02；返回 (manifest, 手写块)。"""
    old_pdf = build_pdf(workspace.root / "old_book.pdf", OLD_PAGES)
    man = parse_pdf(old_pdf, workspace.ingest_root, "pypdf", layout=workspace,
                    doc_id=DOC_ID, title="Old Book")
    for ch in man["chapters"]:
        chunk_and_anchor(DOC_ID, ch["chapter_id"], layout=workspace)
    shutil.copy(old_pdf, workspace.textbooks_dir / f"{DOC_ID}.pdf")
    return man


def _make_new_lecture_pdf(workspace) -> bytes:
    """追加讲义：1 页带 "Chapter 3 New Lecture" 书签（pypdf 可读书签）。"""
    path = build_pdf(workspace.root / "new_lecture.pdf", NEW_PAGES,
                     outlines=[("Chapter 3 New Lecture", 1)])
    return path.read_bytes()


def _add_manual_note(workspace) -> tuple[DocStore, dict]:
    store = DocStore(workspace, DOC_ID)
    ch2 = store.chapter_ids()[1]
    anchor = store.anchor_rows(ch2)[0]
    block = store.create_block(
        {"type": "motivation", "title": "我的手写心得", "content": "这是人工写的重要心得。",
         "basis": "anchored", "order": 1.0,
         "sources": [{"anchor_id": anchor["anchor_id"],
                      "quote": anchor["content"][:20], "relation": "motivates"}]},
        author="human", chapter_id=ch2)
    return store, block


def _wait(c, task_id: str, budget: float = 180.0) -> dict:
    task: dict = {"status": "n/a"}
    deadline = time.time() + budget
    while time.time() < deadline:
        task = c.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in ("succeeded", "failed", "partial", "cancelled", "interrupted"):
            return task
        time.sleep(0.1)
    return task


def _client(workspace):
    # 测试只关心追加事务，不走真实解析器选择：默认 config 的 marker 在本机会先探
    # docker/模型再回退 pypdf（单次 ~25s），显式 pypdf 让断言保持秒级、完全离线
    (workspace.root / "config.yaml").write_text("parser:\n  default: pypdf\n", encoding="utf-8")
    app = create_app(workspace.root)
    client = TestClient(app, base_url="http://127.0.0.1:8787")
    client.headers.update({"X-Local-Token": app.state.app_state.token_value})
    return app, client


def _append_task(client, pdf_bytes: bytes) -> dict:
    resp = client.post(f"/api/v1/docs/{DOC_ID}/append",
                       files={"file": ("new_lecture.pdf", io.BytesIO(pdf_bytes),
                                       "application/pdf")})
    assert resp.status_code == 202, resp.text
    return _wait(client, resp.json()["task_id"])


def _snapshot_doc(workspace) -> dict[str, bytes]:
    """真实主数据逐字节快照（manifest + 全部块/锚点/历史 JSONL）。"""
    doc_dir = workspace.doc_dir(DOC_ID)
    out = {"manifest.json": (doc_dir / "manifest.json").read_bytes()}
    for folder in ("blocks", "anchors", "history"):
        for p in sorted((doc_dir / folder).glob("*.jsonl")):
            out[f"{folder}/{p.name}"] = p.read_bytes()
    return out


def _snapshot_tree(path: Path) -> dict[str, bytes]:
    if not path.exists():
        return {}
    return {p.relative_to(path).as_posix(): p.read_bytes()
            for p in sorted(path.rglob("*")) if p.is_file()}


def _assert_no_staging_or_backup(workspace) -> None:
    assert not list(workspace.data_dir.glob(".append-staging-*")), "不得残留暂存目录"
    assert not list(workspace.data_dir.glob(".bak-append-*")), "不得残留备份目录"
    assert not list(workspace.data_dir.glob(".append-journal-*")), "不得残留恢复日志"
    assert not list(workspace.doc_dir(DOC_ID).glob(".append-install-*")), "不得残留换入标记"


def _assert_sources_alive(store: DocStore, block: dict) -> None:
    anchor_ids = store.anchors_by_id()
    assert block.get("sources"), "人工笔记必须保留来源"
    for src in block["sources"]:
        assert src["anchor_id"] in anchor_ids, f"来源锚点失效: {src['anchor_id']}"


# ---------------------------------------------------------------------------
# 主场景：旧章身份与人工笔记保留，新页出现新章
# ---------------------------------------------------------------------------
def test_append_preserves_chapter_identity_and_manual_note(workspace):
    old_manifest = _make_old_doc(workspace)
    store, block = _add_manual_note(workspace)
    old_chapters = [dict(c) for c in old_manifest["chapters"]]
    assert [c["chapter_id"] for c in old_chapters] == ["ch01", "ch02"]
    old_rev = store.data_revision()
    old_pdf = (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes()
    new_pdf = _make_new_lecture_pdf(workspace)

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)
        assert task["status"] == "succeeded", task
        assert task["result"]["appended"] is True
        # 进度事件仍是 ingest/anchor 语义
        assert task["progress"].get("stage") == "done"

    manifest = DocStore(workspace, DOC_ID).manifest()
    by_id = {c["chapter_id"]: c for c in manifest["chapters"]}

    # 1) 旧章身份不变（id/title/页区间），人工笔记仍可达且未被无谓改 revision
    for old in old_chapters:
        now = by_id[old["chapter_id"]]
        assert now["title"] == old["title"], "旧章标题被改写"
        assert (now["page_start"], now["page_end"]) == (old["page_start"], old["page_end"]), \
            f"旧章页区间被改动: {old} -> {now}"
    rows = DocStore(workspace, DOC_ID).block_rows("ch02")
    kept = [b for b in rows if b["id"] == block["id"]]
    assert kept, "追加后 ch02 人工笔记必须仍可读到"
    assert kept[0]["content"] == "这是人工写的重要心得。"
    assert kept[0]["revision"] == 1, "内容未变的重锚定不得无谓提升 revision"
    _assert_sources_alive(DocStore(workspace, DOC_ID), kept[0])

    # 2) 新页出现新章（未被旧章占用的最小 chNN），旧 id 不被抢占
    assert len(manifest["pages"]) == 3
    new_ids = [cid for cid in by_id if cid not in {c["chapter_id"] for c in old_chapters}]
    assert new_ids == ["ch03"], f"新页应得到 ch03: {sorted(by_id)}"
    assert by_id["ch03"]["page_start"] == by_id["ch03"]["page_end"] == 3
    assert "Chapter 3 New Lecture" == by_id["ch03"]["title"]
    anchors = DocStore(workspace, DOC_ID).anchor_rows("ch03")
    assert anchors and all(a["page"] == 3 for a in anchors)

    # 3) 真实 PDF 已合并（旧页还在、新页追加），data_revision 单调（一次逻辑追加 +1）
    from pypdf import PdfReader

    merged = (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes()
    assert merged != old_pdf
    assert len(PdfReader(io.BytesIO(merged)).pages) == 3
    assert int(manifest["data_revision"]) == int(old_rev) + 1
    _assert_no_staging_or_backup(workspace)


# ---------------------------------------------------------------------------
# 原子性：锚定中途失败 → 真实数据逐字节不变，暂存清干净
# ---------------------------------------------------------------------------
def test_append_failure_leaves_real_data_byte_identical(workspace, monkeypatch):
    _make_old_doc(workspace)
    _add_manual_note(workspace)
    before_files = _snapshot_doc(workspace)
    before_pdf = (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes()
    before_manifest = json.loads(before_files["manifest.json"])
    new_pdf = _make_new_lecture_pdf(workspace)

    from src.ingest import append_stage

    real_anchor = append_stage.chunk_and_anchor
    calls: list[str] = []

    def failing_anchor(doc_id, chapter_id, **kwargs):
        calls.append(chapter_id)
        if len(calls) >= 2:
            from src.core.errors import PipelineError

            raise PipelineError("boom: 第 2 章锚定中途失败")
        return real_anchor(doc_id, chapter_id, **kwargs)

    monkeypatch.setattr(append_stage, "chunk_and_anchor", failing_anchor)

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)
    assert task["status"] == "failed", task
    assert "boom" in (task.get("error") or {}).get("message", ""), task
    assert len(calls) >= 2, "失败必须发生在暂存内的逐章锚定阶段"

    # 真实产物逐字节不变（PDF / manifest / 全部块与锚点文件）
    assert (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes() == before_pdf
    assert _snapshot_doc(workspace) == before_files
    after_manifest = json.loads((workspace.doc_dir(DOC_ID) / "manifest.json")
                                .read_text(encoding="utf-8"))
    assert after_manifest["data_revision"] == before_manifest["data_revision"]
    assert len(after_manifest["chapters"]) == 2
    assert [c["chapter_id"] for c in after_manifest["chapters"]] == ["ch01", "ch02"]
    _assert_no_staging_or_backup(workspace)


# ---------------------------------------------------------------------------
# 并发：swap 前 data_revision 变化 → 冲突失败，追加不生效、并发写入不丢
# ---------------------------------------------------------------------------
def test_append_conflicts_when_doc_changes_midway(workspace, monkeypatch):
    _make_old_doc(workspace)
    _add_manual_note(workspace)
    before_pdf = (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes()
    before_rev = DocStore(workspace, DOC_ID).data_revision()
    new_pdf = _make_new_lecture_pdf(workspace)

    from src.ingest import append_stage

    real_anchor = append_stage.chunk_and_anchor
    fired = {"done": False}
    concurrent_block: dict = {}

    def anchor_then_concurrent_edit(doc_id, chapter_id, **kwargs):
        count = real_anchor(doc_id, chapter_id, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            # 模拟追加运行期间有人在界面里新建笔记：真实 data_revision +1
            concurrent_block.update(DocStore(workspace, DOC_ID).create_block(
                {"type": "motivation", "title": "并发时的新笔记", "content": "不能被我覆盖。",
                 "sources": [], "basis": "synthesis", "order": 99.0},
                author="human", chapter_id="ch01"))
        return count

    monkeypatch.setattr(append_stage, "chunk_and_anchor", anchor_then_concurrent_edit)

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)
    assert task["status"] == "failed", task
    message = (task.get("error") or {}).get("message", "")
    assert "data_revision" in message and "重试" in message, message

    # 追加整体未生效（页数/章节/PDF 仍是旧状态），并发新建的笔记还在
    store = DocStore(workspace, DOC_ID)
    manifest = store.manifest()
    assert len(manifest["pages"]) == 2
    assert [c["chapter_id"] for c in manifest["chapters"]] == ["ch01", "ch02"]
    assert (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes() == before_pdf
    assert int(manifest["data_revision"]) == int(before_rev) + 1      # 只来自并发编辑
    got = [b for b in store.block_rows("ch01") if b["id"] == concurrent_block["id"]]
    assert got and got[0]["content"] == "不能被我覆盖。", "并发新增笔记不得被追加覆盖"
    _assert_no_staging_or_backup(workspace)


# ---------------------------------------------------------------------------
# 章节合并纯函数：跨界裁剪 / 缺口兜底 / 最小未占用 id（无需起任务）
# ---------------------------------------------------------------------------
def test_merge_chapters_clips_crossing_and_keeps_old_identity():
    from src.ingest.append_stage import merge_chapters

    old = [{"chapter_id": "ch01", "title": "旧一", "page_start": 1, "page_end": 2},
           {"chapter_id": "ch02", "title": "旧二", "page_start": 3, "page_end": 4}]
    # 新书签把全书重排为「大章 1（1-3）」「大章 2（4-6）」：旧页 1-4 不动，
    # 旧章 2 的页 4 不能因新检测被吞掉；新页 5-6 得到新章
    detected = [{"chapter_id": "ch01", "title": "大章 1", "page_start": 1, "page_end": 3},
                {"chapter_id": "ch02", "title": "大章 2", "page_start": 4, "page_end": 6}]
    out = merge_chapters(old, detected, 4, 6)
    assert [(c["chapter_id"], c["page_start"], c["page_end"]) for c in out] == [
        ("ch01", 1, 2), ("ch02", 3, 4), ("ch03", 5, 6)]
    assert out[2]["title"] == "大章 2"          # 跨界章裁剪后保留原标题


def test_merge_chapters_falls_back_when_detection_covers_only_old_pages():
    from src.ingest.append_stage import merge_chapters

    old = [{"chapter_id": "ch01", "title": "旧一", "page_start": 1, "page_end": 2},
           {"chapter_id": "ch03", "title": "旧三", "page_start": 3, "page_end": 4}]
    detected = [{"chapter_id": "ch01", "title": "旧一", "page_start": 1, "page_end": 2},
                {"chapter_id": "ch02", "title": "旧三", "page_start": 3, "page_end": 4}]
    out = merge_chapters(old, detected, 4, 5)
    # 新页 5 没被任何检测章覆盖 → 兜底章；id 取未被旧章占用的最小 ch02
    assert [c["chapter_id"] for c in out] == ["ch01", "ch03", "ch02"]
    assert (out[2]["page_start"], out[2]["page_end"]) == (5, 5)


def test_merge_chapters_keeps_old_when_no_new_pages():
    from src.ingest.append_stage import merge_chapters

    old = [{"chapter_id": "ch01", "title": "旧一", "page_start": 1, "page_end": 2}]
    out = merge_chapters(old, [{"chapter_id": "ch01", "title": "x", "page_start": 1,
                               "page_end": 2}], 2, 2)
    assert out == old


# ---------------------------------------------------------------------------
# A3 swap 失败回滚：换入阶段文件被占用 → 旧数据回滚、错误信息可操作、无 journal 残留
# ---------------------------------------------------------------------------
def test_append_swap_failure_rolls_back_with_clear_error(workspace, monkeypatch):
    _make_old_doc(workspace)
    _add_manual_note(workspace)
    before_files = _snapshot_doc(workspace)
    before_pdf = (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes()
    before_rev = DocStore(workspace, DOC_ID).data_revision()
    new_pdf = _make_new_lecture_pdf(workspace)

    from src.ingest import append_stage

    real_move = append_stage._move
    fired = {"done": False}

    def is_staging_doc(src: Path) -> bool:
        return src.name == DOC_ID and any(
            part.startswith(append_stage.STAGING_PREFIX) for part in src.parts)

    def fail_install_doc(src, dst, attempts=8):
        if (not fired["done"] and is_staging_doc(Path(src))
                and Path(dst) == workspace.doc_dir(DOC_ID)):
            fired["done"] = True
            raise PermissionError("file is used by another process")
        return real_move(src, dst, attempts=attempts)

    monkeypatch.setattr(append_stage, "_move", fail_install_doc)

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "failed", task
    message = (task.get("error") or {}).get("message", "")
    assert "原数据已回滚" in message and "重试" in message, message
    # 旧数据逐字节不变（含 revision），journal/备份/暂存都不残留
    assert _snapshot_doc(workspace) == before_files
    assert (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes() == before_pdf
    assert DocStore(workspace, DOC_ID).data_revision() == before_rev
    _assert_no_staging_or_backup(workspace)


# ---------------------------------------------------------------------------
# A2 解析器一致性：追加沿用旧 manifest 记录的解析器（不被当前配置换掉）
# ---------------------------------------------------------------------------
def _client_with_parser_default(workspace, parser_name: str):
    (workspace.root / "config.yaml").write_text(
        f"parser:\n  default: {parser_name}\n", encoding="utf-8")
    app = create_app(workspace.root)
    client = TestClient(app, base_url="http://127.0.0.1:8787")
    client.headers.update({"X-Local-Token": app.state.app_state.token_value})
    return app, client


def test_append_uses_parser_recorded_in_old_manifest(workspace, monkeypatch):
    """旧 manifest 记录 pypdf、当前配置 marker：追加必须仍传 pypdf（旧页 anchor 不漂移）。"""
    old_manifest = _make_old_doc(workspace)
    assert old_manifest["parser"]["name"] == "pypdf"
    _add_manual_note(workspace)
    new_pdf = _make_new_lecture_pdf(workspace)

    from src.ingest import append_stage

    real_parse = append_stage.parse_pdf_with_fallback
    calls: list[tuple[str, str]] = []

    def spy(pdf_path, out_dir, parser="auto", **kwargs):
        calls.append((parser, str(kwargs.get("fallback"))))
        return real_parse(pdf_path, out_dir, parser, **kwargs)

    monkeypatch.setattr(append_stage, "parse_pdf_with_fallback", spy)

    _app, client = _client_with_parser_default(workspace, "marker")
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "succeeded", task
    # 实际传参：parser=旧记录 pypdf（不是当前配置 marker）；失败兜底才是当前配置
    assert calls == [("pypdf", "marker")], calls
    manifest = DocStore(workspace, DOC_ID).manifest()
    assert manifest["parser"]["name"] == "pypdf"
    assert len(manifest["pages"]) == 3


def test_append_falls_back_when_recorded_parser_unavailable(workspace, monkeypatch):
    """旧 manifest 记录 marker（不可用）+ 当前配置 pypdf → 回退当前配置默认并成功。"""
    _make_old_doc(workspace)
    doc_dir = workspace.doc_dir(DOC_ID)
    raw = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
    raw["parser"]["name"] = "marker"          # 记录名不可用（模拟未安装 marker）
    (doc_dir / "manifest.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    DocStore._invalidate(workspace, DOC_ID)
    new_pdf = _make_new_lecture_pdf(workspace)

    from src.core.errors import InputError
    from src.ingest import append_stage

    real_get_adapter = append_stage.get_adapter

    def fake_get_adapter(name, **kwargs):
        if name == "marker":
            raise InputError("marker 不可用：未安装")
        return real_get_adapter(name, **kwargs)

    monkeypatch.setattr(append_stage, "get_adapter", fake_get_adapter)
    real_parse = append_stage.parse_pdf_with_fallback
    calls: list[tuple[str, str]] = []

    def spy(pdf_path, out_dir, parser="auto", **kwargs):
        calls.append((parser, str(kwargs.get("fallback"))))
        return real_parse(pdf_path, out_dir, parser, **kwargs)

    monkeypatch.setattr(append_stage, "parse_pdf_with_fallback", spy)

    _app, client = _client_with_parser_default(workspace, "pypdf")
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "succeeded", task
    assert calls == [("pypdf", "pypdf")], calls
    assert DocStore(workspace, DOC_ID).manifest()["parser"]["name"] == "pypdf"


# ---------------------------------------------------------------------------
# A1 崩溃恢复：swap 换入 doc 之后被硬杀 → journal/备份残留，recover 回滚旧数据
# ---------------------------------------------------------------------------
def _write_crash_journal(workspace, doc_id: str, expected: int, *,
                         backup_root: Path, staging_root: Path,
                         phase: str = "swapping") -> Path:
    from src.ingest import append_stage

    journal = append_stage._journal_path(workspace, doc_id)
    append_stage._write_journal(journal, {
        "version": 1,
        "doc_id": doc_id,
        "expected_revision": expected,
        "backup_root": append_stage._rel_to_root(workspace, backup_root),
        "staging_root": append_stage._rel_to_root(workspace, staging_root),
        "artifacts": list(append_stage.REAL_ARTIFACT_KEYS),
        "phase": phase,
        "created_at": "2026-01-01T00:00:00Z",
    })
    return journal


def _make_interrupted_doc_backup(workspace, doc_id: str, expected: int):
    """崩溃态 a：doc 已移入备份、真实 doc 缺失、journal 已落盘。"""
    from src.ingest import append_stage

    backup_root = workspace.data_dir / f"{append_stage.BACKUP_PREFIX}{doc_id}-test"
    backup_root.mkdir()
    shutil.move(str(workspace.doc_dir(doc_id)), str(backup_root / "doc"))
    staging_root = workspace.data_dir / f"{append_stage.STAGING_PREFIX}{doc_id}-test"
    staging_root.mkdir()
    journal = _write_crash_journal(workspace, doc_id, expected,
                                   backup_root=backup_root, staging_root=staging_root)
    return backup_root, journal


def test_append_recovers_after_hard_kill_mid_swap(workspace, monkeypatch):
    """模拟硬杀：换入 doc 之后进程死掉（回滚没跑）→ 重启恢复把真实数据回滚到旧状态。"""
    _make_old_doc(workspace)
    _add_manual_note(workspace)
    before_doc = _snapshot_doc(workspace)
    before_ingest = _snapshot_tree(workspace.ingest_root / DOC_ID)
    before_pdf = (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes()
    expected = DocStore(workspace, DOC_ID).data_revision()
    real_doc_path = workspace.doc_dir(DOC_ID)
    new_pdf = _make_new_lecture_pdf(workspace)

    from src.ingest import append_stage

    class HardKill(BaseException):
        pass

    real_move = append_stage._move

    def is_staging_doc(src: Path) -> bool:
        return src.name == DOC_ID and any(
            part.startswith(append_stage.STAGING_PREFIX) for part in src.parts)

    def killing_move(src, dst, attempts=8):
        real_move(src, dst, attempts=attempts)
        if is_staging_doc(Path(src)) and Path(dst) == real_doc_path:
            raise HardKill("模拟断电：换入 doc 之后进程被硬杀")

    def no_rollback(*args, **kwargs):
        raise HardKill("模拟断电：回滚没机会执行")

    monkeypatch.setattr(append_stage, "_move", killing_move)
    monkeypatch.setattr(append_stage, "_rollback", no_rollback)

    with pytest.raises(HardKill):
        append_stage.run_append(workspace, DOC_ID, new_pdf,
                                {"parser": {"default": "pypdf"}})

    # 崩溃现场：journal + 备份在，真实 doc 已换成新产物，ingest/pdf 已移走
    journals = list(workspace.data_dir.glob(f"{append_stage.JOURNAL_PREFIX}*.json"))
    backups = list(workspace.data_dir.glob(f"{append_stage.BACKUP_PREFIX}*"))
    assert len(journals) == 1 and len(backups) == 1, (journals, backups)
    assert real_doc_path.exists() and _snapshot_doc(workspace) != before_doc
    assert not (workspace.ingest_root / DOC_ID).exists()
    assert not (workspace.textbooks_dir / f"{DOC_ID}.pdf").exists()

    monkeypatch.setattr(append_stage, "_move", real_move)   # 恢复路径不再注入硬杀
    report = append_stage.recover_interrupted_append(workspace)

    assert report["recovered"] == [DOC_ID], report
    assert _snapshot_doc(workspace) == before_doc
    assert _snapshot_tree(workspace.ingest_root / DOC_ID) == before_ingest
    assert (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes() == before_pdf
    assert DocStore(workspace, DOC_ID).data_revision() == expected
    _assert_no_staging_or_backup(workspace)


def test_lifespan_startup_recovers_interrupted_swap(workspace):
    """恢复钩子挂在 lifespan（而非 AppState.__init__）：服务启动即自动回滚。"""
    _make_old_doc(workspace)
    before_doc = _snapshot_doc(workspace)
    expected = DocStore(workspace, DOC_ID).data_revision()
    backup_root, journal = _make_interrupted_doc_backup(workspace, DOC_ID, expected)
    assert not workspace.doc_dir(DOC_ID).exists(), "崩溃现场：真实 doc 在备份里"

    _app, client = _client(workspace)
    with client:                    # TestClient 上下文管理器触发 lifespan startup
        pass

    assert _snapshot_doc(workspace) == before_doc
    assert DocStore(workspace, DOC_ID).data_revision() == expected
    assert not journal.exists() and not backup_root.exists()


# ---------------------------------------------------------------------------
# 第三轮 P2：追加 staging 期间事务外的题库/套题/图写入不得被 swap 覆盖
# ---------------------------------------------------------------------------
def _qbank_line(text: str) -> str:
    return json.dumps({"text": text, "source": "old.txt",
                       "created_at": "2026-01-01T00:00:00Z"}, ensure_ascii=False) + "\n"


def _during_staging_once(real_fn, fired: dict, action):
    """包装 chunk_and_anchor：锚定第 1 章成功后执行一次 action（模拟并发写盘）。"""

    def wrapped(doc_id, chapter_id, **kwargs):
        count = real_fn(doc_id, chapter_id, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            action()
        return count

    return wrapped


def test_append_keeps_qbank_and_quiz_written_during_staging(workspace, monkeypatch):
    """审查 P2：追加期间上传题库/生成套题（不 bump revision）不得被整目录 swap 覆盖。"""
    _make_old_doc(workspace)
    _add_manual_note(workspace)
    doc_dir = workspace.doc_dir(DOC_ID)
    (doc_dir / "qbank.jsonl").write_text(_qbank_line("旧题库条目：追加前上传"),
                                       encoding="utf-8")
    quiz_dir = doc_dir / "quiz"
    quiz_dir.mkdir()
    (quiz_dir / "quiz-old.json").write_text('{"quiz_id": "quiz-old"}\n', encoding="utf-8")
    before_rev = DocStore(workspace, DOC_ID).data_revision()
    new_pdf = _make_new_lecture_pdf(workspace)

    from src.ingest import append_stage

    fired = {"done": False}

    def concurrent_upload():
        # append_qbank 语义：直接追加 qbank.jsonl；generate_quiz 语义：直接落 quiz/*.json
        with (doc_dir / "qbank.jsonl").open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(_qbank_line("追加期间新上传的题库条目"))
        (quiz_dir / "quiz-new.json").write_text('{"quiz_id": "quiz-new"}\n', encoding="utf-8")

    monkeypatch.setattr(append_stage, "chunk_and_anchor",
                        _during_staging_once(append_stage.chunk_and_anchor, fired,
                                            concurrent_upload))

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "succeeded", task
    assert fired["done"], "并发写必须发生在 staging 锚定阶段（swap 之前）"
    qbank_text = (doc_dir / "qbank.jsonl").read_text(encoding="utf-8")
    assert "旧题库条目：追加前上传" in qbank_text, "旧题库条目不得丢"
    assert "追加期间新上传的题库条目" in qbank_text, \
        "追加期间上传的题库条目被 swap 覆盖（P2 回归）"
    assert (quiz_dir / "quiz-old.json").exists(), "旧套题不得丢"
    got_new = quiz_dir / "quiz-new.json"
    assert got_new.exists(), "追加期间生成的套题被 swap 覆盖（P2 回归）"
    assert json.loads(got_new.read_text(encoding="utf-8"))["quiz_id"] == "quiz-new"
    manifest = DocStore(workspace, DOC_ID).manifest()
    assert int(manifest["data_revision"]) == before_rev + 1
    assert len(manifest["pages"]) == 3
    _assert_no_staging_or_backup(workspace)


def test_append_keeps_figure_png_written_during_staging(workspace, monkeypatch):
    """审查 P2 同类：追加期间 refresh-figures 事务外写的 png 不得被 swap 覆盖。"""
    _make_old_doc(workspace)
    _add_manual_note(workspace)
    doc_dir = workspace.doc_dir(DOC_ID)
    fig_dir = doc_dir / "figures"
    fig_dir.mkdir(exist_ok=True)
    new_png = fig_dir / "fig_99999999.png"
    new_pdf = _make_new_lecture_pdf(workspace)

    from src.ingest import append_stage

    fired = {"done": False}

    def concurrent_refresh():
        new_png.write_bytes(b"\x89PNG\r\n\x1a\nnewly-refreshed-figure")

    monkeypatch.setattr(append_stage, "chunk_and_anchor",
                        _during_staging_once(append_stage.chunk_and_anchor, fired,
                                            concurrent_refresh))

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "succeeded", task
    assert fired["done"]
    assert new_png.exists(), "追加期间刷新的图 png 被 swap 覆盖（P2 同类回归）"
    assert new_png.read_bytes() == b"\x89PNG\r\n\x1a\nnewly-refreshed-figure"
    _assert_no_staging_or_backup(workspace)


def test_append_fails_and_rolls_back_when_merge_back_fails(workspace, monkeypatch):
    """merge-back 任何一件失败都必须整单回滚（宁可失败，不可静默丢并发写入）。"""
    _make_old_doc(workspace)
    _add_manual_note(workspace)
    before_files = _snapshot_doc(workspace)
    before_pdf = (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes()
    before_rev = DocStore(workspace, DOC_ID).data_revision()
    new_pdf = _make_new_lecture_pdf(workspace)

    from src.ingest import append_stage

    def boom(*args, **kwargs):
        raise OSError("disk full while merging live artifacts")

    monkeypatch.setattr(append_stage, "_merge_back_live_artifacts", boom)

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "failed", task
    message = (task.get("error") or {}).get("message", "")
    assert "原数据已回滚" in message and "重试" in message, message
    assert _snapshot_doc(workspace) == before_files
    assert (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes() == before_pdf
    assert DocStore(workspace, DOC_ID).data_revision() == before_rev
    _assert_no_staging_or_backup(workspace)


# ---------------------------------------------------------------------------
# P2-A1：追加 PDF 带两个相邻书签 → 必须是两个新章，不能被并成一章
# ---------------------------------------------------------------------------
TWO_CHAPTER_PAGES: list[list[tuple[str, float, float, float]]] = [
    [("New lecture text on the third page.", 11, 72, 680),
     ("A second line for the first appended chapter.", 11, 72, 650)],
    [("New lecture text on the fourth page.", 11, 72, 680),
     ("A second line for the second appended chapter.", 11, 72, 650)],
]


def test_append_two_adjacent_bookmarks_keep_two_new_chapters(workspace):
    old_manifest = _make_old_doc(workspace)
    old_ids = [c["chapter_id"] for c in old_manifest["chapters"]]
    assert old_ids == ["ch01", "ch02"]
    _add_manual_note(workspace)
    new_pdf = build_pdf(
        workspace.root / "new_two_chapters.pdf", TWO_CHAPTER_PAGES,
        outlines=[("Chapter 3 New Lecture", 1), ("Chapter 4 Another Lecture", 2)]
        ).read_bytes()

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "succeeded", task
    manifest = DocStore(workspace, DOC_ID).manifest()
    by_id = {c["chapter_id"]: c for c in manifest["chapters"]}
    new_ids = sorted(cid for cid in by_id if cid not in old_ids)
    assert new_ids == ["ch03", "ch04"], \
        f"相邻的两个新检测章被并成一章（P2-A1 回归）: {new_ids}"
    assert by_id["ch03"]["title"] == "Chapter 3 New Lecture"
    assert (by_id["ch03"]["page_start"], by_id["ch03"]["page_end"]) == (3, 3)
    assert by_id["ch04"]["title"] == "Chapter 4 Another Lecture"
    assert (by_id["ch04"]["page_start"], by_id["ch04"]["page_end"]) == (4, 4)
    assert len(manifest["pages"]) == 4
    _assert_no_staging_or_backup(workspace)


# ---------------------------------------------------------------------------
# P2-A4：崩溃遗留「COMMIT 已落、apply 未做」的事务，追加开工前必须前滚
# ---------------------------------------------------------------------------
def test_append_rolls_forward_pending_committed_transaction(workspace, monkeypatch):
    _make_old_doc(workspace)
    store, block = _add_manual_note(workspace)
    ch = store.chapter_ids()[0]
    rows = store.block_rows(ch, include_deleted=True)
    pending = copy.deepcopy(block)
    pending["id"] = "b_PENDINGTX001"
    pending["title"] = "崩溃前已 COMMIT"
    pending["content"] = "崩溃前已 COMMIT 的编辑，追加后必须还在"
    pending["sources"] = []          # 反向索引只由事务维护，手工行不带 sources 才自洽
    pending["basis"] = "synthesis"
    rows.append(pending)
    base_rev = store.data_revision()

    import src.core.transactions as tx

    real_apply = tx._apply_intent

    def crash_after_commit(layout, doc_id, intent, changes_by_rel):
        raise RuntimeError("模拟：COMMIT 已落盘、apply 前进程死亡")

    monkeypatch.setattr(tx, "_apply_intent", crash_after_commit)
    with pytest.raises(RuntimeError):
        tx.commit_doc(workspace, DOC_ID, base_rev,
                      [tx.replace_change(workspace, DOC_ID, f"blocks/{ch}.jsonl", rows)])
    monkeypatch.setattr(tx, "_apply_intent", real_apply)
    tx_root = workspace.transactions_dir(DOC_ID)
    assert tx_root.is_dir() and list(tx_root.iterdir()), "现场应留有未完成事务"

    new_pdf = _make_new_lecture_pdf(workspace)
    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "succeeded", task.get("error") or task
    fresh = DocStore(workspace, DOC_ID)
    assert fresh.get_block("b_PENDINGTX001")["content"] == \
        "崩溃前已 COMMIT 的编辑，追加后必须还在", "COMMIT 遗留事务被快照/换入丢弃（P2-A4 回归）"
    # pending tx 前滚 +1，一次逻辑追加再 +1
    assert fresh.data_revision() == base_rev + 2
    assert not tx_root.exists() or not list(tx_root.iterdir()), "前滚后事务日志应清理"
    _assert_no_staging_or_backup(workspace)


# ---------------------------------------------------------------------------
# P3-A1：失败诊断件（migration 报告）必须复制出会被删的 staging
# ---------------------------------------------------------------------------
def test_append_failure_preserves_migration_report_outside_staging(workspace):
    _make_old_doc(workspace)
    store, block = _add_manual_note(workspace)
    ch = store.chapter_ids()[0]
    bad = copy.deepcopy(block)
    bad["id"] = "b_DEADSOURCE01"
    bad["title"] = "悬空来源"
    bad["content"] = "来源指向不存在的锚点"
    bad["sources"] = [{"anchor_id": "a_00000000_0000000000000000",
                       "quote": "x", "relation": "defines"}]
    with workspace.blocks(DOC_ID, ch).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(bad, ensure_ascii=False) + "\n")
    DocStore._invalidate(workspace, DOC_ID)
    before_files = _snapshot_doc(workspace)
    before_pdf = (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes()
    before_rev = DocStore(workspace, DOC_ID).data_revision()
    new_pdf = _make_new_lecture_pdf(workspace)

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "failed", task
    message = (task.get("error") or {}).get("message", "")
    assert "重锚定后存在无法配对的来源" in message, message
    assert ".append-staging-" not in message, "报错路径不得指向随后被删除的暂存目录"
    assert "append-failed.migration.json" in message, message
    reports = list(workspace.reports_doc_dir(DOC_ID).glob("*.append-failed.migration.json"))
    assert reports, "失败诊断件必须复制到真实 reports"
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload.get("broken_sources"), payload
    # 任务错误目前只落 message（TaskManager 不存 detail）；报告内容本身可人工排查，数据必须零改动
    assert _snapshot_doc(workspace) == before_files
    assert (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes() == before_pdf
    assert DocStore(workspace, DOC_ID).data_revision() == before_rev
    _assert_no_staging_or_backup(workspace)


# ---------------------------------------------------------------------------
# P2-A2：刷新过图资源的教材追加（pymupdf 内嵌图；worker 不可用则 skip）
# ---------------------------------------------------------------------------
IMAGE_OLD_PAGES: list[list[tuple[str, float, float, float]]] = [
    [("Chapter 1 Image Notes", 18, 72, 720),
     ("A text paragraph above the embedded figure on page one.", 11, 72, 700),
     ("Another paragraph that must stay byte identical after append.", 11, 72, 620)],
]


def build_image_pdf(path: Path, pages) -> Path:
    """原始 PDF：每页文本 + 第 1 页一个 220x160pt 的内嵌 RGB 位图（供 pymupdf 提取）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    objs: dict[int, bytes] = {}
    counter = {"n": 1}

    def alloc() -> int:
        num = counter["n"]
        counter["n"] += 1
        return num

    catalog_id = alloc()
    pages_id = alloc()
    font_id = alloc()
    image_id = alloc()
    raw = bytes([255, 0, 0, 0, 0, 255]) * 8          # 4x4 RGB 条纹
    objs[image_id] = (b"<< /Type /XObject /Subtype /Image /Width 4 /Height 4 "
                      b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length "
                      + str(len(raw)).encode() + b" >>\nstream\n" + raw + b"\nendstream")
    content_ids = [alloc() for _ in pages]
    for idx, lines in enumerate(pages):
        ops = []
        for text, size, x, y in lines:
            safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"BT /F1 {size} Tf {x} {y} Td ({safe}) Tj ET")
        if idx == 0:
            ops.insert(0, "q 220 0 0 160 72 380 cm /Im1 Do Q")
        stream = "\n".join(ops).encode("latin-1")
        objs[content_ids[idx]] = (b"<< /Length " + str(len(stream)).encode()
                                  + b" >>\nstream\n" + stream + b"\nendstream")
    page_ids = [alloc() for _ in pages]
    for idx, pid in enumerate(page_ids):
        objs[pid] = (
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> "
            f"/XObject << /Im1 {image_id} 0 R >> >> "
            f"/Contents {content_ids[idx]} 0 R >>").encode("latin-1")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[pages_id] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")
    objs[catalog_id] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1")
    objs[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref_pos = len(out)
    max_num = max(objs) + 1
    out += f"xref\n0 {max_num}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, max_num):
        out += (f"{offsets[num]:010d} 00000 n \n".encode() if num in offsets
                else b"0000000000 65535 f \n")
    out += (f"trailer\n<< /Size {max_num} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))
    return path


def _prepare_refreshed_figure_doc(workspace):
    """旧教材导入 → refresh_figures 补图块/图锚点 → 建一条引用图锚点的笔记。"""
    import re as _re

    from src.ingest.figures import refresh_figures

    old_pdf = build_image_pdf(workspace.root / "image_book.pdf", IMAGE_OLD_PAGES)
    man = parse_pdf(old_pdf, workspace.ingest_root, "pypdf", layout=workspace,
                    doc_id=DOC_ID, title="Image Book")
    for ch in man["chapters"]:
        chunk_and_anchor(DOC_ID, ch["chapter_id"], layout=workspace)
    shutil.copy(old_pdf, workspace.textbooks_dir / f"{DOC_ID}.pdf")
    try:
        figs = refresh_figures(old_pdf, workspace, DOC_ID)
    except Exception as exc:      # .venv-parsers 缺失/pymupdf 不可用
        pytest.skip(f"pymupdf 图提取不可用：{exc}")
    if not figs:
        pytest.skip("pymupdf 图提取不可用（未识别到内嵌图）")
    store = DocStore(workspace, DOC_ID)
    for ch in store.chapter_ids():
        chunk_and_anchor(DOC_ID, ch, layout=workspace)
    store = DocStore(workspace, DOC_ID)
    fig_anchors = [a for a in store.anchors_by_id().values()
                   if a.get("content_type") == "figure"]
    assert fig_anchors, "refresh_figures 后应生成 figure 锚点"
    fig_anchor = fig_anchors[0]
    fig_id = _re.search(r"fig_\d{8}", fig_anchor["content"]).group(0)
    block = store.create_block(
        {"type": "motivation", "title": "图引用",
         "content": f"见 ![{fig_id}] 示意图",
         "sources": [{"anchor_id": fig_anchor["anchor_id"],
                      "quote": fig_anchor["content"][:20], "relation": "motivates"}],
         "basis": "anchored", "order": 1.0},
        author="human", chapter_id=fig_anchor["chapter_id"])
    return store, fig_anchor, block, fig_id, store.data_revision()


def test_append_after_refresh_figures_keeps_figure_anchors(workspace):
    """refresh_figures 过的教材追加不能因「重解析丢图块 → 图锚点无法配对」而失败。"""
    store, fig_anchor, block, fig_id, before_rev = _prepare_refreshed_figure_doc(workspace)
    new_pdf = _make_new_lecture_pdf(workspace)

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "succeeded", task
    fresh = DocStore(workspace, DOC_ID)
    manifest = fresh.manifest()
    assert len(manifest["pages"]) == 2, "追加页必须生效"
    anchor_ids = fresh.anchors_by_id()
    assert fig_anchor["anchor_id"] in anchor_ids, "旧 figure 锚点被重解析丢掉了（P2-A2 回归）"
    kept = [b for b in fresh.block_rows(fig_anchor["chapter_id"]) if b["id"] == block["id"]]
    assert kept and kept[0]["content"] == f"见 ![{fig_id}] 示意图"
    assert kept[0]["sources"][0]["anchor_id"] == fig_anchor["anchor_id"], "图锚点引用断裂"
    assert (workspace.figures_dir(DOC_ID) / f"{fig_id}.png").exists(), "图 png 不得丢"
    assert int(manifest["data_revision"]) == before_rev + 1
    _assert_no_staging_or_backup(workspace)


def test_append_after_refresh_figures_fails_clearly_when_page_json_missing(workspace):
    """旧页 JSON 缺失导致无法回填 figure 块 → 明确 InputError，真实数据零改动。"""
    store, _fig_anchor, _block, _fig_id, before_rev = \
        _prepare_refreshed_figure_doc(workspace)
    before_files = _snapshot_doc(workspace)
    before_pdf = (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes()
    page_file = workspace.ingest_pages(DOC_ID) / "0001.json"
    assert page_file.exists()
    page_file.unlink()
    new_pdf = _make_new_lecture_pdf(workspace)

    _app, client = _client(workspace)
    with client:
        task = _append_task(client, new_pdf)

    assert task["status"] == "failed", task
    message = (task.get("error") or {}).get("message", "")
    assert "重建导入" in message and "图锚点" in message, message
    assert _snapshot_doc(workspace) == before_files
    assert (workspace.textbooks_dir / f"{DOC_ID}.pdf").read_bytes() == before_pdf
    assert DocStore(workspace, DOC_ID).data_revision() == before_rev
    _assert_no_staging_or_backup(workspace)

