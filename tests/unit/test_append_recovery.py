"""A1/A3 回归：追加 swap 崩溃恢复（journal）与 Windows rename 重试窗口。

覆盖：
- 崩溃态 a：只备份了 doc、真实 doc 缺失 → 恢复旧 doc，ingest/pdf 保持不动；
- 崩溃态 b：三件都已换入新产物（备份含三件旧产物）→ 三件全部回旧，revision 回到
  journal.expected_revision，且派生索引/runtime doc_map 被重建回旧 id（不复述 bug：
  swap 期间已 reindex 过的 doc_map 会指向已回滚的新 id）；
- 崩溃态 c：只有 journal/暂存、没有备份 → 只清理，真实数据逐字节不变；
- 幂等：重复 recover 不再改动任何数据；
- doc 已被删除（回收站有记录）→ 不擅自复活旧文档，只清 journal/暂存，备份保留；
- journal 路径注入（backup_root 在 data 之外）→ 拒绝恢复且不删 data 外数据；
- A3：_move 的指数退避重试窗口（短暂占用可恢复，持续占用在约 5s 内失败）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.core.errors import InputError, PipelineError
from src.core.store import DocStore
from src.ingest import append_stage
from src.ingest.append_stage import (
    BACKUP_PREFIX,
    STAGING_PREFIX,
    _journal_path,
    _write_journal,
    recover_interrupted_append,
)


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _close_runtime_after(tmp_path: Path):
    """关闭本测试打开的 runtime.db 连接（Windows 上否则 tmp 目录删不干净）。"""
    yield
    from src.db import runtime as runtime_mod

    root = str(tmp_path.resolve())
    for key in [k for k in list(runtime_mod._RUNTIME) if k.startswith(root)]:
        db = runtime_mod._RUNTIME.pop(key, None)
        if db is not None:
            db.close()


def _prepare_doc(sample_doc):
    """sample_doc 基础上补 textbooks PDF + 一个人工块（断言旧块内容与 doc_map）。"""
    layout, doc_id = sample_doc
    (layout.textbooks_dir / f"{doc_id}.pdf").write_bytes(b"%PDF-1.4 old\n")
    store = DocStore(layout, doc_id)
    block = store.create_block(
        {"type": "motivation", "title": "旧笔记", "content": "旧内容不能丢。",
         "sources": [], "basis": "synthesis", "order": 1.0},
        author="human", chapter_id=store.chapter_ids()[0])
    return layout, doc_id, store, block


def _snapshot_doc(layout, doc_id: str) -> dict[str, bytes]:
    """真实主数据逐字节快照（manifest + 全部块/锚点/历史 JSONL；index.db 是派生层）。"""
    doc_dir = layout.doc_dir(doc_id)
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


def _write_crash_journal(layout, doc_id: str, expected: int, *,
                         backup_root: Path | None, staging_root: Path | None,
                         phase: str = "swapping",
                         install_marker: str | None = None,
                         installed_revision: int | None = None) -> Path:
    journal = {
        "version": 1,
        "doc_id": doc_id,
        "expected_revision": expected,
        "backup_root": (append_stage._rel_to_root(layout, backup_root)
                        if backup_root is not None else None),
        "staging_root": (append_stage._rel_to_root(layout, staging_root)
                         if staging_root is not None else None),
        "artifacts": list(append_stage.REAL_ARTIFACT_KEYS),
        "phase": phase,
        "created_at": "2026-01-01T00:00:00Z",
        "install_marker": install_marker,
        "installed_revision": installed_revision,
    }
    path = _journal_path(layout, doc_id)
    _write_journal(path, journal)
    return path


def _snapshot_real(layout, doc_id: str) -> tuple[dict, dict, bytes]:
    return (_snapshot_doc(layout, doc_id),
            _snapshot_tree(layout.ingest_root / doc_id),
            (layout.textbooks_dir / f"{doc_id}.pdf").read_bytes())


# ---------------------------------------------------------------------------
# 崩溃态 b：三件已换入新产物 → 全部回旧 + 重建索引（data_revision == expected）
# ---------------------------------------------------------------------------
def test_recover_rolls_back_all_installed_artifacts(sample_doc, monkeypatch):
    layout, doc_id, store, block = _prepare_doc(sample_doc)
    expected = store.data_revision()
    before_doc, before_ingest, before_pdf = _snapshot_real(layout, doc_id)
    old_block_id = block["id"]

    # 构造崩溃现场：真实三件先移入备份，再换入「新产物」
    backup_root = layout.data_dir / f"{BACKUP_PREFIX}{doc_id}-test"
    backup_root.mkdir()
    shutil.move(str(layout.doc_dir(doc_id)), str(backup_root / "doc"))
    shutil.move(str(layout.ingest_root / doc_id), str(backup_root / "ingest"))
    shutil.move(str(layout.textbooks_dir / f"{doc_id}.pdf"),
                str(backup_root / "textbook.pdf"))
    shutil.copytree(backup_root / "doc", layout.doc_dir(doc_id))
    new_manifest = json.loads(layout.manifest(doc_id).read_text(encoding="utf-8"))
    new_manifest["data_revision"] = expected + 1
    new_manifest["title"] = "NEW TITLE"
    (layout.doc_dir(doc_id) / "manifest.json").write_text(
        json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (layout.doc_dir(doc_id) / "blocks" / "ch99.jsonl").write_text(
        json.dumps({"id": "b_ZZZZZZZZZZZZ", "content": "new"}) + "\n", encoding="utf-8")
    shutil.copytree(backup_root / "ingest", layout.ingest_root / doc_id)
    (layout.ingest_root / doc_id / "pages" / "9999.json").write_text(
        "{}\n", encoding="utf-8")
    (layout.textbooks_dir / f"{doc_id}.pdf").write_bytes(b"%PDF-1.4 new\n")

    # 复述残余风险：swap 期间的 reindex 已把 runtime.doc_map 改指向新块/新锚点 id
    from src.db.runtime import get_runtime

    runtime = get_runtime(layout.runtime_db)
    new_block_id = "b_NEWNEWNEWNEW"
    new_anchor_id = "a_00000000_0000000000000000"
    runtime.sync_doc_map(doc_id, [new_block_id], [new_anchor_id])

    staging_root = layout.data_dir / f"{STAGING_PREFIX}{doc_id}-test"
    staging_root.mkdir(parents=True)
    (staging_root / "leftover.txt").write_text("x", encoding="utf-8")
    # 真实的换入标记（run_append 换入 doc 时会写），恢复据此判定「这是本次未提交的
    # 新文档」而不是「中断后用户编辑过的旧文档」（P2-A3）
    marker = ".append-install-testtoken"
    (layout.doc_dir(doc_id) / marker).write_text("marker", encoding="utf-8")
    journal = _write_crash_journal(layout, doc_id, expected,
                                   backup_root=backup_root, staging_root=staging_root,
                                   install_marker=marker, installed_revision=expected + 1)

    # 断言恢复后会重建派生索引（index.db + runtime doc_map）
    import src.db.reindex as reindex_mod

    real_reindex = reindex_mod.reindex_doc
    rebuilt: list[str] = []

    def spy(layout_, doc_id_, *args, **kwargs):
        rebuilt.append(doc_id_)
        return real_reindex(layout_, doc_id_, *args, **kwargs)

    monkeypatch.setattr(reindex_mod, "reindex_doc", spy)

    report = recover_interrupted_append(layout)

    assert report["recovered"] == [doc_id], report
    assert rebuilt == [doc_id], "恢复后必须重建派生索引（doc_map 才会回到旧 id）"
    # 1) 三件旧产物逐字节回旧，data_revision 回到 journal 记录的 expected_revision
    assert _snapshot_real(layout, doc_id) == (before_doc, before_ingest, before_pdf)
    restored = DocStore(layout, doc_id)
    assert restored.data_revision() == expected
    assert restored.manifest()["title"] != "NEW TITLE"
    rows = restored.block_rows(restored.chapter_ids()[0])
    assert [b["id"] for b in rows] == [old_block_id]
    assert rows[0]["content"] == "旧内容不能丢。"
    # 2) runtime doc_map 与恢复后的数据一致（旧 id 在、被回滚的新 id 不在）
    assert runtime.lookup_doc("block", old_block_id) == doc_id
    assert runtime.lookup_doc("block", new_block_id) is None
    assert runtime.lookup_doc("anchor", new_anchor_id) is None
    # 3) journal/备份/暂存全部清理
    assert not journal.exists()
    assert not backup_root.exists()
    assert not staging_root.exists()

    # 幂等：重复恢复不再动任何数据
    again = recover_interrupted_append(layout)
    assert again == {"recovered": [], "cleaned": [], "committed": [],
                     "deferred": [], "failed": []}
    assert _snapshot_real(layout, doc_id) == (before_doc, before_ingest, before_pdf)


# ---------------------------------------------------------------------------
# 崩溃态 a：只备份了 doc（真实 doc 缺失）→ 恢复 doc，ingest/pdf 保持不动
# ---------------------------------------------------------------------------
def test_recover_restores_doc_when_only_backup_doc_exists(sample_doc):
    layout, doc_id, store, _block = _prepare_doc(sample_doc)
    expected = store.data_revision()
    before_doc, before_ingest, before_pdf = _snapshot_real(layout, doc_id)

    backup_root = layout.data_dir / f"{BACKUP_PREFIX}{doc_id}-test"
    backup_root.mkdir()
    shutil.move(str(layout.doc_dir(doc_id)), str(backup_root / "doc"))   # 真实 doc 缺失
    staging_root = layout.data_dir / f"{STAGING_PREFIX}{doc_id}-test"
    (staging_root / "data" / doc_id).mkdir(parents=True)
    (staging_root / "data" / doc_id / "marker.txt").write_text("new doc", encoding="utf-8")
    journal = _write_crash_journal(layout, doc_id, expected,
                                   backup_root=backup_root, staging_root=staging_root)

    report = recover_interrupted_append(layout)

    assert report["recovered"] == [doc_id], report
    assert _snapshot_real(layout, doc_id) == (before_doc, before_ingest, before_pdf)
    assert DocStore(layout, doc_id).data_revision() == expected
    assert not journal.exists() and not backup_root.exists() and not staging_root.exists()


# ---------------------------------------------------------------------------
# 崩溃态 c：只有 journal + 暂存（备份还不存在）→ 只清理，真实数据不动
# ---------------------------------------------------------------------------
def test_recover_cleans_journal_and_staging_without_backup(sample_doc):
    layout, doc_id, store, _block = _prepare_doc(sample_doc)
    before_doc, before_ingest, before_pdf = _snapshot_real(layout, doc_id)
    expected = store.data_revision()
    staging_root = layout.data_dir / f"{STAGING_PREFIX}{doc_id}-test"
    staging_root.mkdir(parents=True)
    (staging_root / "leftover.txt").write_text("x", encoding="utf-8")
    journal = _write_crash_journal(layout, doc_id, expected,
                                   backup_root=None, staging_root=staging_root)

    report = recover_interrupted_append(layout)

    assert report["cleaned"] == [doc_id], report
    assert _snapshot_real(layout, doc_id) == (before_doc, before_ingest, before_pdf)
    assert not journal.exists() and not staging_root.exists()


# ---------------------------------------------------------------------------
# 正常提交语义：phase=committed 只清理、不回滚已生效的追加
# ---------------------------------------------------------------------------
def test_recover_committed_journal_only_cleans_up(sample_doc):
    layout, doc_id, store, _block = _prepare_doc(sample_doc)
    expected = store.data_revision()
    before_doc, _before_ingest, _before_pdf = _snapshot_real(layout, doc_id)
    backup_root = layout.data_dir / f"{BACKUP_PREFIX}{doc_id}-test"
    (backup_root / "doc").mkdir(parents=True)
    (backup_root / "doc" / "old.txt").write_text("old", encoding="utf-8")
    staging_root = layout.data_dir / f"{STAGING_PREFIX}{doc_id}-test"
    staging_root.mkdir()
    journal = _write_crash_journal(layout, doc_id, expected, backup_root=backup_root,
                                   staging_root=staging_root, phase="committed")

    report = recover_interrupted_append(layout)

    assert report["committed"] == [doc_id], report
    # 已生效的真实数据不被回滚
    assert _snapshot_doc(layout, doc_id) == before_doc
    assert not journal.exists() and not backup_root.exists() and not staging_root.exists()


# ---------------------------------------------------------------------------
# 半恢复状态：doc 已回旧、ingest/pdf 仍是新产物（上次恢复到一半又断电）→ 继续恢复
# ---------------------------------------------------------------------------
def test_recover_completes_partially_restored_swap(sample_doc):
    layout, doc_id, store, _block = _prepare_doc(sample_doc)
    expected = store.data_revision()
    before_doc, before_ingest, before_pdf = _snapshot_real(layout, doc_id)

    # 备份里只剩 ingest/pdf（doc 已被上一次恢复换回真实路径），真实 ingest/pdf 仍是新产物
    backup_root = layout.data_dir / f"{BACKUP_PREFIX}{doc_id}-test"
    backup_root.mkdir()
    shutil.move(str(layout.ingest_root / doc_id), str(backup_root / "ingest"))
    shutil.move(str(layout.textbooks_dir / f"{doc_id}.pdf"),
                str(backup_root / "textbook.pdf"))
    (layout.ingest_root / doc_id / "pages").mkdir(parents=True)
    (layout.ingest_root / doc_id / "pages" / "9999.json").write_text(
        "{}\n", encoding="utf-8")
    (layout.textbooks_dir / f"{doc_id}.pdf").write_bytes(b"%PDF-1.4 new\n")
    staging_root = layout.data_dir / f"{STAGING_PREFIX}{doc_id}-test"
    staging_root.mkdir()
    journal = _write_crash_journal(layout, doc_id, expected,
                                   backup_root=backup_root, staging_root=staging_root)

    report = recover_interrupted_append(layout)

    assert report["recovered"] == [doc_id], report
    assert _snapshot_real(layout, doc_id) == (before_doc, before_ingest, before_pdf)
    assert DocStore(layout, doc_id).data_revision() == expected
    assert not journal.exists() and not backup_root.exists() and not staging_root.exists()


# ---------------------------------------------------------------------------
# 文档已被删除（回收站有记录）→ 不擅自复活，只清 journal/暂存，备份保留告警
# ---------------------------------------------------------------------------
def test_recover_does_not_resurrect_deleted_doc(sample_doc):
    layout, doc_id, store, _block = _prepare_doc(sample_doc)
    expected = store.data_revision()
    backup_root = layout.data_dir / f"{BACKUP_PREFIX}{doc_id}-test"
    backup_root.mkdir()
    shutil.move(str(layout.doc_dir(doc_id)), str(backup_root / "doc"))   # 崩溃：doc 在备份
    # 随后用户删除该教材：真实（新）doc 目录移入回收站 → 真实 doc 缺失
    shutil.copytree(backup_root / "doc", layout.doc_dir(doc_id))
    trash = layout.data_dir / ".trash" / f"{doc_id}-20260101T000000Z"
    trash.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(layout.doc_dir(doc_id)), str(trash))
    staging_root = layout.data_dir / f"{STAGING_PREFIX}{doc_id}-test"
    staging_root.mkdir()
    journal = _write_crash_journal(layout, doc_id, expected,
                                   backup_root=backup_root, staging_root=staging_root)

    report = recover_interrupted_append(layout)

    assert report["cleaned"] == [doc_id], report
    assert not layout.doc_dir(doc_id).exists(), "不得把已删除的文档用旧备份复活"
    assert not journal.exists() and not staging_root.exists()
    assert (backup_root / "doc").exists(), "旧数据备份必须保留（不得静默删除）"


# ---------------------------------------------------------------------------
# journal 指向不存在的 doc 目录 / 路径注入 → 安全跳过，不误删 data 之外的数据
# ---------------------------------------------------------------------------
def test_recover_missing_doc_dir_cleans_journal(workspace):
    doc_id = "doc-gone-0001"
    staging_root = workspace.data_dir / f"{STAGING_PREFIX}{doc_id}-test"
    staging_root.mkdir()
    journal = _write_crash_journal(workspace, doc_id, 3,
                                   backup_root=None, staging_root=staging_root)

    report = recover_interrupted_append(workspace)

    assert report["cleaned"] == [doc_id], report
    assert not journal.exists() and not staging_root.exists()


def test_recover_refuses_backup_root_outside_data(workspace, tmp_path):
    doc_id = "doc-inject-01"
    outside = tmp_path / "outside-backup"
    (outside / "doc").mkdir(parents=True)
    (outside / "doc" / "victim.txt").write_text("keep me", encoding="utf-8")
    journal = _journal_path(workspace, doc_id)
    _write_journal(journal, {
        "version": 1, "doc_id": doc_id, "expected_revision": 3,
        "backup_root": str(outside), "staging_root": None,
        "artifacts": list(append_stage.REAL_ARTIFACT_KEYS), "phase": "swapping",
    })

    report = recover_interrupted_append(workspace)

    assert report["failed"] == [doc_id], report
    assert journal.exists(), "拒绝恢复时必须保留 journal 供人工检查"
    assert (outside / "doc" / "victim.txt").read_text(encoding="utf-8") == "keep me"


def test_recover_ignores_staging_root_outside_data(workspace, tmp_path):
    """staging_root 指向 data 之外时只忽略（不删除），journal 照常清理。"""
    doc_id = "doc-inject-02"
    outside = tmp_path / "outside-staging"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep me", encoding="utf-8")
    journal = _journal_path(workspace, doc_id)
    _write_journal(journal, {
        "version": 1, "doc_id": doc_id, "expected_revision": 1,
        "backup_root": None, "staging_root": str(outside),
        "artifacts": list(append_stage.REAL_ARTIFACT_KEYS), "phase": "swapping",
    })

    report = recover_interrupted_append(workspace)

    assert report["cleaned"] == [doc_id], report
    assert not journal.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep me"


# ---------------------------------------------------------------------------
# 第三轮 P2：swap merge-back 规则（事务外活产物并回；事务管理文件绝不动）
# ---------------------------------------------------------------------------
def test_merge_back_live_artifacts_copies_only_live_outputs(tmp_path):
    """qbank 整体取备份版；quiz 全量覆盖/补齐；figures 只补缺失（同名新目录优先）。"""
    source = tmp_path / "backup-doc"
    target = tmp_path / "new-doc"
    (source / "quiz").mkdir(parents=True)
    (source / "figures").mkdir(parents=True)
    (source / "blocks").mkdir(parents=True)
    (target / "quiz").mkdir(parents=True)
    (target / "figures").mkdir(parents=True)
    (target / "blocks").mkdir(parents=True)

    # qbank：备份版本 = 快照 + 追加期间新增；目标只有快照版本 → 必须整体取备份版
    (source / "qbank.jsonl").write_bytes(b'{"text":"snapshot"}\n{"text":"during-append"}\n')
    (target / "qbank.jsonl").write_bytes(b'{"text":"snapshot"}\n')
    # quiz：备份里已有旧套题 + 追加期间新套题 → 覆盖/补齐
    (source / "quiz" / "quiz-old.json").write_bytes(b'{"quiz_id":"old"}\n')
    (source / "quiz" / "quiz-new.json").write_bytes(b'{"quiz_id":"new"}\n')
    (target / "quiz" / "quiz-old.json").write_bytes(b'{"quiz_id":"old"}\n')
    # figures：备份有旧 png + 追加期间新 png + 与新目录同名的 png（新目录优先）
    (source / "figures" / "fig_00000001.png").write_bytes(b"backup-old")
    (source / "figures" / "fig_99999999.png").write_bytes(b"during-append")
    (source / "figures" / "fig_00000002.png").write_bytes(b"backup-collides")
    (target / "figures" / "fig_00000002.png").write_bytes(b"newly-cropped")
    # 事务管理文件：备份里存在但绝不能被并回
    (source / "manifest.json").write_bytes(b'{"data_revision": 4}')
    (source / "blocks" / "ch01.jsonl").write_bytes(b'{"id":"old"}\n')
    (target / "manifest.json").write_bytes(b'{"data_revision": 5}')
    (target / "blocks" / "ch01.jsonl").write_bytes(b'{"id":"new"}\n')

    append_stage._merge_back_live_artifacts(source, target)

    assert (target / "qbank.jsonl").read_bytes() == \
        b'{"text":"snapshot"}\n{"text":"during-append"}\n'
    assert (target / "quiz" / "quiz-old.json").exists()
    assert (target / "quiz" / "quiz-new.json").read_bytes() == b'{"quiz_id":"new"}\n'
    assert (target / "figures" / "fig_00000001.png").read_bytes() == b"backup-old"
    assert (target / "figures" / "fig_99999999.png").read_bytes() == b"during-append"
    assert (target / "figures" / "fig_00000002.png").read_bytes() == b"newly-cropped", \
        "同名图必须以即将生效的新目录为准（不覆盖本次追加新裁的图）"
    # 事务管理文件保持 swap 语义：目标（新数据）不被备份覆盖
    assert (target / "manifest.json").read_bytes() == b'{"data_revision": 5}'
    assert (target / "blocks" / "ch01.jsonl").read_bytes() == b'{"id":"new"}\n'


def test_merge_back_live_artifacts_noop_when_backup_has_nothing(tmp_path):
    """备份里没有活产物时不得凭空创建 qbank/quiz/figures。"""
    source = tmp_path / "backup-doc"
    target = tmp_path / "new-doc"
    source.mkdir()
    target.mkdir()
    (target / "manifest.json").write_bytes(b"{}")

    append_stage._merge_back_live_artifacts(source, target)

    assert not (target / "qbank.jsonl").exists()
    assert not (target / "quiz").exists()
    assert not (target / "figures").exists()
    assert (target / "manifest.json").read_bytes() == b"{}"


# ---------------------------------------------------------------------------
# P2-A3：中断后服务继续、用户又编辑 → 恢复不得用旧备份覆盖新编辑
# ---------------------------------------------------------------------------
def test_recover_defers_when_real_data_has_newer_edits(sample_doc):
    layout, doc_id, store, _block = _prepare_doc(sample_doc)
    expected = store.data_revision()
    # 模拟「上次 swap 失败留下 journal+备份、真实数据已回滚到旧状态」
    backup_root = layout.data_dir / f"{BACKUP_PREFIX}{doc_id}-test"
    backup_root.mkdir()
    shutil.copytree(layout.doc_dir(doc_id), backup_root / "doc")
    shutil.copytree(layout.ingest_root / doc_id, backup_root / "ingest")
    shutil.copy2(layout.textbooks_dir / f"{doc_id}.pdf", backup_root / "textbook.pdf")
    # 服务继续可用：用户又编辑了一条笔记（data_revision 增长；真实目录没有换入标记）
    store.create_block({"type": "motivation", "title": "中断后的新编辑",
                        "content": "这条编辑不能被回滚覆盖。",
                        "sources": [], "basis": "synthesis", "order": 2.0},
                       author="human", chapter_id=store.chapter_ids()[0])
    edited_revision = DocStore(layout, doc_id).data_revision()
    assert edited_revision > expected
    before_doc, before_ingest, before_pdf = _snapshot_real(layout, doc_id)
    journal = _write_crash_journal(layout, doc_id, expected, backup_root=backup_root,
                                   staging_root=None)

    report = recover_interrupted_append(layout)

    assert report["deferred"] == [doc_id], report
    # 新编辑与真实产物逐字节保留，备份/journal 保留（现场）
    assert _snapshot_real(layout, doc_id) == (before_doc, before_ingest, before_pdf)
    assert DocStore(layout, doc_id).data_revision() == edited_revision
    assert journal.exists() and backup_root.exists()
    data = json.loads(journal.read_text(encoding="utf-8"))
    assert data["phase"] == "deferred"
    # 再次恢复仍拒绝（幂等、不动数据、不会把 deferred 变回回滚）
    again = recover_interrupted_append(layout)
    assert again["deferred"] == [doc_id], again
    assert _snapshot_real(layout, doc_id) == (before_doc, before_ingest, before_pdf)


def test_run_append_refuses_when_recovery_is_deferred(sample_doc):
    """journal 因新编辑被 deferred 时，再追加必须明确拒绝（提示人工确认）。"""
    layout, doc_id, store, _block = _prepare_doc(sample_doc)
    expected = store.data_revision()
    backup_root = layout.data_dir / f"{BACKUP_PREFIX}{doc_id}-test"
    (backup_root / "doc").mkdir(parents=True)
    (backup_root / "doc" / "old.txt").write_text("old", encoding="utf-8")
    store.create_block({"type": "motivation", "title": "中断后的新编辑",
                        "content": "不能丢", "sources": [], "basis": "synthesis",
                        "order": 3.0}, author="human", chapter_id=store.chapter_ids()[0])
    journal = _write_crash_journal(layout, doc_id, expected, backup_root=backup_root,
                                   staging_root=None)
    assert recover_interrupted_append(layout)["deferred"] == [doc_id]

    with pytest.raises(PipelineError) as excinfo:
        append_stage.run_append(layout, doc_id, b"%PDF-1.4 new\n",
                                {"parser": {"default": "pypdf"}})
    message = str(excinfo.value)
    assert "人工确认" in message and "未自动回滚" in message, message
    assert journal.exists()
    # 真实数据没被动过（新编辑仍在）
    assert any(b["title"] == "中断后的新编辑"
               for b in DocStore(layout, doc_id).all_block_rows())


# ---------------------------------------------------------------------------
# P3-A2：旧章页区间校验（脏 manifest 早失败）
# ---------------------------------------------------------------------------
def test_validate_old_chapters_rejects_dirty_manifest():
    with pytest.raises(InputError, match="重叠"):
        append_stage._validate_old_chapters(
            [{"chapter_id": "ch01", "page_start": 1, "page_end": 3},
             {"chapter_id": "ch02", "page_start": 3, "page_end": 4}], 4)
    with pytest.raises(InputError, match="非法"):
        append_stage._validate_old_chapters(
            [{"chapter_id": "ch01", "page_start": 1, "page_end": 9}], 4)
    with pytest.raises(InputError, match="缺少章节"):
        append_stage._validate_old_chapters([], 4)
    # 合法区间不得误报
    append_stage._validate_old_chapters(
        [{"chapter_id": "ch01", "page_start": 1, "page_end": 2},
         {"chapter_id": "ch02", "page_start": 3, "page_end": 4}], 4)


# ---------------------------------------------------------------------------
# A3：_move 重试窗口
# ---------------------------------------------------------------------------
def test_move_retries_transient_lock_then_succeeds(tmp_path, monkeypatch):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("payload", encoding="utf-8")
    real_replace = append_stage.os.replace
    calls = {"n": 0}

    def flaky_replace(a, b):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("file in use by concurrent reader")
        return real_replace(a, b)

    sleeps: list[float] = []
    monkeypatch.setattr(append_stage.os, "replace", flaky_replace)
    monkeypatch.setattr(append_stage.time, "sleep", sleeps.append)

    append_stage._move(src, dst)

    assert calls["n"] == 3
    assert dst.read_text(encoding="utf-8") == "payload"
    assert sleeps == [0.05, 0.1], "短暂占用只应按指数退避等两次"


def test_move_retry_window_is_bounded(tmp_path, monkeypatch):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("x", encoding="utf-8")
    calls = {"n": 0}

    def always_locked(a, b):
        calls["n"] += 1
        raise PermissionError("file in use")

    sleeps: list[float] = []
    monkeypatch.setattr(append_stage.os, "replace", always_locked)
    monkeypatch.setattr(append_stage.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError):
        append_stage._move(src, dst)

    assert calls["n"] == 8, "重试次数（A3 要求 8~10 次）"
    assert sleeps == [0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 2.0], "指数退避，单次上限 2s"
    assert sum(sleeps) <= 6.0, "总等待不得拖到几十秒"
    assert src.read_text(encoding="utf-8") == "x"
