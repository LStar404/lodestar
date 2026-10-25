"""追加讲义的事务化执行（Review #1）：暂存快照内完成合并/解析/分章/重锚定，成功后统一 swap。

背景：旧实现直接覆盖真实 PDF 再原地重解析重锚定，任何一步失败（尤其是
`_migrate_sources` 报「重锚定后存在无法配对的来源」）都会留下已改写的 PDF 与
manifest，原章节里的人工笔记随之不可见。本模块把整个追加过程放进
`data/.append-staging-<doc>-<uuid>/` 的独立 Layout（真实 layout 全程只读）：

1. 复制真实 `data/<doc>` 主数据（不含 index.db/transactions）到暂存，合并 PDF 也只写暂存；
2. 在暂存上解析、按「旧章身份不变 + 新页增量分章」合并 chapters、提交 manifest、逐章重锚定；
3. 全部成功后才在 `doc_lock` 内校验真实 data_revision 未变，备份旧产物并 swap；
   任何一步失败只删除暂存，真实数据零改动（swap 中途失败回滚备份）。

swap 非崩溃原子 + journal 恢复（A1）：真实换入是「备份 → 换入 doc/ingest/pdf → 归一并
data_revision → reindex → 清理」的多步 rename，进程被强杀在中间会留下部分换入的真实目录与
`data/.bak-append-*` 备份。因此动手前先原子写 `data/.append-journal-<doc>.json`（含
expected_revision/backup_root/staging_root/phase），正常成功后删除；启动钩子与 run_append
开头调用 `recover_interrupted_append` 按 journal 阶段回滚/清理，并重建派生索引。
崩溃点任意、恢复可重跑（幂等）。

解析器一致性（A2）：追加会重解析「旧书 + 新页」全书，解析器一旦与原始导入不同，旧页抽出的
文本/坐标随之变化，旧 anchor_id 漂移会让人工笔记来源失配。因此优先沿用旧 manifest 记录的
`parser.name`（如 pypdf/pymupdf/marker），仅当该名字不可用/解析失败时回退到当前配置默认。

章节身份保持（关键）：追加只会在尾部加页，旧页字节等价，因此旧 chapters 原样保留
（chapter_id/title/page_start/page_end 不变），只给新页区间 [old_pages+1, new_pages]
分配未被占用的最小 chNN；检测章节若跨旧/新边界则裁剪 page_start=old_pages+1。

事务外活产物 merge-back（第三轮 P2）：staging 阶段可能持续数分钟，期间对真实 doc 的
`qbank.jsonl`（上传题库）、`quiz/*.json`（AI 出题）、`figures/*.png`（刷新图）写入既不
经过事务也不 bump data_revision，整目录 swap 会把它们静默覆盖回快照状态。因此换入新
目录后、归一 revision 前，从 `backup/doc`（swap 前的真实目录）把这些「活产物」保守并回；
受事务管理的主数据（manifest/blocks/anchors/history/refs/index.db/transactions）绝不并回。

第四轮补充（对抗审查）：
- 相邻新章（P2-A1）：detect_chapters 的章节天然相邻（page_end=下章起始-1），merge_chapters
  只合并「真正重叠」区间，否则追加的多章讲义会被并成一章；
- 图资源兼容（P2-A2）：refresh_figures 事务外写入的 figure 块在暂存重解析（pypdf 不产图块）
  时会消失 → 锚定前把旧页 figure 块回填进暂存页，无法回填则明确 InputError（要求重建导入）；
- 恢复安全（P2-A3）：恢复旧 swap 前比较真实 data_revision 与 journal.expected_revision，
  真实更新（中断后用户又编辑）时拒绝自动回滚（phase=deferred，保留现场）；
- 遗留事务（P2-A4）：拍快照前先 recover_doc 前滚 COMMIT 未完成事务（快照排除 transactions/）；
- 失败诊断（P3-A1）：migration 报告从会被删的 staging 复制到真实 reports 并进异常 detail；
- 旧章校验（P3-A2）：追加前校验旧章页区间在 [1, old_pages] 且互不重叠。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import uuid
from io import BytesIO
from pathlib import Path

from src.anchor.chunker import chunk_and_anchor
from src.core.errors import ConflictError, InputError, NotFound, PipelineError, TaskCancelled
from src.core.jsonl import atomic_write_bytes
from src.core.paths import Layout, check_doc_id
from src.core.store import DocMutation, DocStore
from src.core.text import utc_now_iso
from src.core.transactions import doc_lock
from src.ingest.parse_pdf import get_adapter, parse_pdf_with_fallback

STAGING_PREFIX = ".append-staging-"
BACKUP_PREFIX = ".bak-append-"
JOURNAL_PREFIX = ".append-journal-"
JOURNAL_VERSION = 1
# swap 的三件真实产物（恢复时逐件独立判断）；backup 内相对名见 _backup_artifact_paths
REAL_ARTIFACT_KEYS = ("doc", "ingest", "pdf")
_FIGURE_ANCHOR_RE = re.compile(r"!\[(fig_\d{8})\]")
_COPY_IGNORE = shutil.ignore_patterns("index.db", "index.db-*", "index.db-wal", "index.db-shm",
                                      "transactions", "__pycache__", "*.lock",
                                      ".append-install-*")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run_append(layout: Layout, doc_id: str, new_pdf: bytes, config: dict, *,
               progress=None, is_cancelled=None, vision_ocr=None) -> dict:
    """在暂存快照中完成追加；成功 swap 后返回 {doc_id, chapters, anchors, appended}。

    A1：开工前先恢复本 doc 上次被强杀的 swap（journal 驱动）；恢复未完成、或恢复被
    推迟（真实编辑比中断基线新，不能覆盖）时拒绝开工。
    A2：解析优先沿用旧 manifest 记录的解析器；并把刷新过图资源的旧 figure 块回填到
    暂存页 JSON（否则 pypdf 重解析丢图块 → 旧 figure 锚点无法配对而整单失败）。
    A4：拍快照前先 recover_doc：上次进程在 COMMIT 之后 DONE 之前死掉的事务必须前滚，
    否则快照（排除 transactions/）与事后删备份会让它永久丢失。
    """
    recover_interrupted_append(layout, only_doc=doc_id)
    journal_path = _journal_path(layout, doc_id)
    if journal_path.exists():
        _raise_pending_recovery(journal_path)
    store = DocStore(layout, doc_id)
    # A4：doc_lock 内前滚 COMMIT 未完成事务（DocStore.recover 自带同一把 doc 锁）。
    # 必须早于 manifest 读取与快照复制：快照是「已提交状态」的读视图，半提交态进去会
    # 让该事务随 staging 换入/备份删除而永久丢失。
    store.recover()
    DocStore._invalidate(layout, doc_id)
    old_manifest = dict(store.manifest())
    expected_revision = int(old_manifest.get("data_revision", 1) or 1)
    old_pages = len(old_manifest.get("pages") or [])
    old_chapters = [dict(c) for c in old_manifest.get("chapters") or []]
    _validate_old_chapters(old_chapters, old_pages)      # P3-A2：脏 manifest 早失败
    old_pdf = layout.textbooks_dir / f"{doc_id}.pdf"
    if not old_pdf.exists():
        raise InputError("该教材没有可追加的 PDF（PPTX 教材暂不支持追加）")

    staging_root = layout.data_dir / f"{STAGING_PREFIX}{doc_id}-{uuid.uuid4().hex[:12]}"
    staging = Layout(staging_root)
    try:
        _prepare_staging(layout, staging, doc_id, old_pdf, new_pdf, old_pages)
        if progress:
            progress({"stage": "ingest", "pages_done": 0, "pages_total": None})

        parser_name, parser_fallback = _parser_selection(old_manifest, config)
        parsed = parse_pdf_with_fallback(
            staging.textbooks_dir / f"{doc_id}.pdf", staging.ingest_root,
            parser_name, fallback=parser_fallback,
            layout=staging, doc_id=doc_id,
            profile=old_manifest.get("profile", "textbook"),
            title=old_manifest.get("title"), config=config,
            progress=progress, vision_ocr=vision_ocr)

        total_pages = len(parsed.get("pages") or [])
        # P2-A2：把真实旧页 JSON 里 refresh_figures 写入的 figure 块回填进暂存页
        # （必须在锚定之前；旧图锚点靠内容哈希复用，缺块就会整单失败）。
        _restore_old_figure_blocks(layout, staging, doc_id, old_pages)
        chapters = merge_chapters(old_chapters, parsed.get("chapters") or [],
                                  old_pages, total_pages)
        staging_store = DocStore(staging, doc_id)
        staging_store.commit(
            DocMutation(manifest_updates=_merged_manifest_updates(
                old_manifest, parsed, chapters)),
            base_revision=staging_store.data_revision(), sync_backlinks=False)

        ordered = sorted(chapters, key=lambda c: (int(c["page_start"]), int(c["page_end"])))
        counts: dict[str, int] = {}
        for idx, ch in enumerate(ordered):
            if is_cancelled is not None and is_cancelled():
                raise TaskCancelled("任务已取消")
            if progress:
                progress({"stage": "anchor", "done": idx, "total": len(ordered),
                          "chapter_id": ch["chapter_id"]})
            # 重锚定（5.5）：旧章内容哈希稳定 → anchor_id 不变，_migrate_sources 无操作；
            # 新章产生新锚点。任一章失败都在此抛错，真实数据未动（只删暂存）。
            counts[ch["chapter_id"]] = chunk_and_anchor(
                doc_id, ch["chapter_id"], layout=staging)
        if progress:
            progress({"stage": "anchor", "done": len(ordered), "total": len(ordered)})
        if is_cancelled is not None and is_cancelled():
            raise TaskCancelled("任务已取消")

        chapters_out = [dict(c) for c in
                        DocStore(staging, doc_id).manifest().get("chapters") or []]
        _swap_into_place(layout, staging, doc_id, expected_revision)
        return {"doc_id": doc_id, "chapters": chapters_out,
                "anchors": counts, "appended": True}
    except PipelineError as exc:
        # P3-A1：migration 报告写在 staging（finally 会删目录）→ 失败时复制到真实
        # reports，并把消息里失效的 staging 路径改写成副本路径、摘要放进 detail。
        report_paths, summary, mapping = _preserve_staging_reports(layout, staging, doc_id)
        if not report_paths:
            raise
        message = exc.message
        for old_path, new_path in mapping.items():
            message = message.replace(old_path, new_path)
        message = message.replace(str(staging.reports_doc_dir(doc_id)),
                                  str(layout.reports_doc_dir(doc_id)))
        detail = dict(exc.detail)
        detail["reports"] = report_paths
        detail["migration"] = summary
        raise PipelineError(
            f"{message}（诊断报告副本：{'；'.join(report_paths)}）",
            detail=detail) from exc
    finally:
        _close_staging_runtime(staging_root)
        _rmtree_retry(staging_root)


# ---------------------------------------------------------------------------
# 暂存快照：复制主数据 + 合并 PDF
# ---------------------------------------------------------------------------
def _prepare_staging(layout: Layout, staging: Layout, doc_id: str,
                     old_pdf: Path, new_pdf: bytes, old_pages: int) -> None:
    staging.ensure_base()
    src = layout.doc_dir(doc_id)
    if not src.exists():
        raise NotFound(f"文档不存在: {doc_id}")
    shutil.copytree(src, staging.doc_dir(doc_id), ignore=_COPY_IGNORE)
    for sub in ("anchors", "blocks", "history", "figures", "refs"):
        (staging.doc_dir(doc_id) / sub).mkdir(parents=True, exist_ok=True)

    merged, old_pdf_pages = _merge_pdfs(old_pdf.read_bytes(), new_pdf)
    if old_pdf_pages != old_pages:
        # 页数是章节身份的坐标基准；manifest 与磁盘 PDF 不一致时继续追加会把旧页
        # 错误地划进新章节，宁可拒绝并让用户先重建导入
        raise InputError(
            f"现有 PDF 页数（{old_pdf_pages}）与 manifest 记录（{old_pages}）不一致，"
            "请先「重建导入」再追加")
    # 旧 PDF 在成功前不动：合并结果只落暂存
    (staging.textbooks_dir / f"{doc_id}.pdf").write_bytes(merged)


def _merge_pdfs(old_pdf: bytes, new_pdf: bytes) -> tuple[bytes, int]:
    """旧 PDF + 新讲义 → 合并字节；返回 (bytes, 旧 PDF 页数)。

    内存句柄避免 Windows 文件占用；pypdf 的 append 会一并搬运书签（import_outline），
    新书签正是分章策略改变、旧实现出事的来源。
    """
    from pypdf import PdfReader, PdfWriter

    try:
        old_reader = PdfReader(BytesIO(old_pdf))
        new_reader = PdfReader(BytesIO(new_pdf))
        old_pages = len(old_reader.pages)
        if old_pages == 0:
            raise InputError("现有 PDF 没有任何页面，无法追加")
        if len(new_reader.pages) == 0:
            raise InputError("追加的 PDF 没有任何页面")
        writer = PdfWriter()
        writer.append(old_reader)
        writer.append(new_reader)
        buf = BytesIO()
        writer.write(buf)
        return buf.getvalue(), old_pages
    except InputError:
        raise
    except Exception as exc:
        raise InputError(f"无法读取/合并 PDF：{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# P2-A2 图资源兼容：把刷新过图的旧页 figure 块回填进暂存页
# ---------------------------------------------------------------------------
def _is_figure_block(blk: dict) -> bool:
    bbox = blk.get("bbox")
    return (blk.get("type") == "figure" and bool(blk.get("figure_id"))
            and isinstance(bbox, (list, tuple)) and len(bbox) == 4)


def _bbox_key(bbox) -> tuple | None:
    try:
        return tuple(round(float(v), 1) for v in bbox[:4])
    except (TypeError, ValueError, IndexError):
        return None


def _figure_block_copy(blk: dict) -> dict:
    """旧 figure 块 → 暂存页块（保留锚点 id 依赖的 type/text/bbox/figure_id）。"""
    out = {
        "type": "figure",
        "level": int(blk.get("level") or 0),
        "text": str(blk.get("text") or ""),
        "bbox": [float(v) for v in blk["bbox"]],
        "figure_id": str(blk["figure_id"]),
    }
    if blk.get("image_path"):
        out["image_path"] = str(blk["image_path"])
    return out


def _staged_figure_ids(staging: Layout, doc_id: str, pages: int) -> set[str]:
    out: set[str] = set()
    pages_dir = staging.ingest_pages(doc_id)
    for page in range(1, int(pages) + 1):
        page_file = pages_dir / f"{page:04d}.json"
        try:
            data = json.loads(page_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for blk in data.get("blocks") or []:
            if blk.get("type") == "figure" and blk.get("figure_id"):
                out.add(str(blk["figure_id"]))
    return out


def _old_figure_anchor_ids(staging: Layout, doc_id: str) -> set[str]:
    """暂存里复制的旧锚点中，属于 figure 的 figure_id 集合（从锚点内容解析）。"""
    out: set[str] = set()
    store = DocStore(staging, doc_id)
    for chapter_id in store.chapter_ids():
        for row in store.anchor_rows(chapter_id):
            if row.get("content_type") != "figure":
                continue
            m = _FIGURE_ANCHOR_RE.search(row.get("content") or "")
            if m:
                out.add(m.group(1))
    return out


def _restore_old_figure_blocks(layout: Layout, staging: Layout, doc_id: str,
                               old_pages: int) -> int:
    """把真实旧页 JSON 里的 figure 块回填进暂存页 JSON（P2-A2）。

    refresh_figures 把 figure 块写进 ingest_out/<doc>/pages/*.json 并重锚定出 figure
    锚点；追加在暂存里重解析整本书时，pypdf 等文本解析器不产 figure 块 → 旧 figure
    锚点在新锚点里消失 → chunker._migrate_sources 报「重锚定后存在无法配对的来源」
    而整单失败（且报告指向随后被删的暂存目录）。figure png 已随 doc 目录复制进暂存
    figures，这里只需把旧页的 figure 块（type/text/bbox/figure_id 原样）回填，图锚点
    id（内容哈希）即可稳定复用；staging 自己新解析出的其它块保持不动。

    回填后做完整性校验：旧 figure 锚点必须都能在暂存页找到对应 figure 块；否则给
    明确 InputError（要求「重建导入」），不让 _migrate_sources 含糊失败。
    """
    real_pages_dir = layout.ingest_pages(doc_id)
    staging_pages_dir = staging.ingest_pages(doc_id)
    backfilled = 0
    if real_pages_dir.is_dir() and staging_pages_dir.is_dir():
        for page in range(1, int(old_pages) + 1):
            old_file = real_pages_dir / f"{page:04d}.json"
            new_file = staging_pages_dir / f"{page:04d}.json"
            if not old_file.is_file() or not new_file.is_file():
                continue
            try:
                old_data = json.loads(old_file.read_text(encoding="utf-8"))
                new_data = json.loads(new_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue          # 该页由下面完整性校验兜底（缺失→明确报错）
            old_figs = [b for b in (old_data.get("blocks") or []) if _is_figure_block(b)]
            if not old_figs:
                continue
            blocks = list(new_data.get("blocks") or [])
            ids = {str(b.get("figure_id")) for b in blocks
                   if b.get("type") == "figure" and b.get("figure_id")}
            boxes = {_bbox_key(b.get("bbox")) for b in blocks if b.get("type") == "figure"}
            added = 0
            for blk in old_figs:
                fid = str(blk["figure_id"])
                if fid in ids or _bbox_key(blk.get("bbox")) in boxes:
                    continue
                blocks.append(_figure_block_copy(blk))
                ids.add(fid)
                added += 1
            if added:
                new_data["blocks"] = blocks
                atomic_write_bytes(
                    new_file,
                    (json.dumps(new_data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                backfilled += added

    expected = _old_figure_anchor_ids(staging, doc_id)
    if expected:
        staged = _staged_figure_ids(staging, doc_id, old_pages)
        missing = sorted(expected - staged)
        if missing:
            raise InputError(
                f"该教材刷新过图资源（{len(expected)} 个图锚点），但旧页解析产物缺失/损坏，"
                f"无法回填 figure 块（缺 {len(missing)} 个），追加会丢图锚点；"
                "请先「重建导入」后再追加",
                detail={"figure_anchors": len(expected), "missing": missing[:8]})
    return backfilled


# ---------------------------------------------------------------------------
# A2 解析器一致性：沿用旧 manifest 记录的解析器，避免旧页 anchor_id 漂移
# ---------------------------------------------------------------------------
def _parser_selection(old_manifest: dict, config: dict) -> tuple[str, str]:
    """返回 (追加解析器, 解析失败时的兜底解析器)。

    追加会重解析「旧书 + 新页」全书：若用与原始导入不同的解析器，旧页抽出的文本/坐标
    会变化，旧 anchor_id 全部漂移、人工笔记的来源配不上而对整单失败。因此：

    - 旧 manifest `parser.name` 存在且不是 "auto" → 用它；
    - 该名字不可用（未安装/未知）→ 回退当前配置默认；
    - 解析失败的回退仍走 parse_pdf_with_fallback：记录名时兜底 = 当前配置默认
      （配置也是 auto 时用 "pypdf"，保持原有「pypdf 兜底」语义）。

    显式探测可用性（而不是把 InputError 交给 fallback）：parse_pdf_with_fallback 有意
    不回退 InputError（那是 PDF/参数本身的问题，换解析器只会掩盖真实错误）。
    """
    parser_cfg = (config.get("parser") or {}) if isinstance(config, dict) else {}
    default = str(parser_cfg.get("default") or "auto").strip().lower()
    recorded = ""
    if isinstance(old_manifest.get("parser"), dict):
        recorded = str(old_manifest["parser"].get("name") or "").strip().lower()
    fallback = default if default not in ("", "auto") else "pypdf"
    if not recorded or recorded == "auto":
        return default, "pypdf"
    try:
        get_adapter(recorded)
    except (InputError, PipelineError) as exc:
        print(f"[append] 旧 manifest 记录的解析器 {recorded} 不可用"
              f"（{str(exc)[:120]}），回退到当前配置 {fallback}", file=sys.stderr)
        return fallback, "pypdf"
    return recorded, fallback


# ---------------------------------------------------------------------------
# 章节身份保持
# ---------------------------------------------------------------------------
def merge_chapters(old_chapters: list[dict], detected: list[dict],
                   old_pages: int, total_pages: int) -> list[dict]:
    """旧章原样保留；检测章节只取新页区间 [old_pages+1, total_pages]，缺口补兜底章。"""
    final: list[dict] = [dict(c) for c in old_chapters]
    used = {c["chapter_id"] for c in final}
    if total_pages <= old_pages:
        return sorted(final, key=lambda c: (int(c["page_start"]), int(c["page_end"])))

    region_start = old_pages + 1
    segments: list[dict] = []
    for ch in sorted(detected, key=lambda c: (int(c.get("page_start", 1)),
                                              int(c.get("page_end", total_pages)))):
        start = max(region_start, int(ch.get("page_start", region_start)))
        end = min(total_pages, int(ch.get("page_end", total_pages)))
        if end < start:
            continue                      # 完全落在旧页区间：丢弃（旧章身份不变）
        if segments and start <= segments[-1]["page_end"]:
            # 只合并「真正重叠」的区间（防御 chapters_override 同页起章被钳成重叠）。
            # 注意不能用 start <= prev_end + 1：detect_chapters 的章节天然相邻
            # （page_end = 下章起始-1），那样会把追加讲义的多个新章并成一章、
            # 只剩第一章标题（P2-A1）。
            segments[-1]["page_end"] = max(segments[-1]["page_end"], end)
        else:
            segments.append({"title": str(ch.get("title") or "").strip()
                             or f"第 {len(segments) + 1} 部分",
                             "page_start": start, "page_end": end})

    # 新页没有任何检测章节覆盖的缺口：补兜底章，保证新页都有归属（可生成笔记）
    filled: list[dict] = []
    cursor = region_start
    for seg in segments:
        if seg["page_start"] > cursor:
            filled.append(_fallback_chapter(cursor, seg["page_start"] - 1))
        filled.append(seg)
        cursor = max(cursor, seg["page_end"] + 1)
    if cursor <= total_pages:
        filled.append(_fallback_chapter(cursor, total_pages))

    for item in filled:
        cid = _next_chapter_id(used)
        used.add(cid)
        final.append({"chapter_id": cid, "title": item["title"],
                      "page_start": int(item["page_start"]),
                      "page_end": int(item["page_end"]), "outline_hash": None})
    return sorted(final, key=lambda c: (int(c["page_start"]), int(c["page_end"])))


def _fallback_chapter(start: int, end: int) -> dict:
    span = f"第 {start} 页" if start == end else f"第 {start}–{end} 页"
    return {"title": f"追加内容（{span}）", "page_start": start, "page_end": end}


def _next_chapter_id(used: set[str]) -> str:
    """新章 id = 未被旧章占用的最小 chNN（无需依赖检测期的按序重排）。"""
    for n in range(1, 10000):
        cid = f"ch{n:02d}" if n < 100 else f"ch{n}"
        if cid not in used:
            return cid
    raise PipelineError("章节 id 已用尽（ch01–ch9999），无法追加")


def _validate_old_chapters(chapters: list[dict], old_pages: int) -> None:
    """P3-A2：旧章页区间必须覆盖在 [1, old_pages] 且互不重叠。

    脏 manifest（离线手工改写、旧版本 bug）会让新章与旧章重叠、同页重复锚定，
    追加后章节树错乱；宁可在拍快照前拒绝并提示「重建导入」，也不要产出坏数据。
    """
    if old_pages <= 0:
        return
    if not chapters:
        raise InputError(
            "manifest 缺少章节信息，无法安全追加；请先「重建导入」后再试",
            detail={"old_pages": int(old_pages)})
    spans: list[tuple[int, int, str]] = []
    for ch in chapters:
        cid = str(ch.get("chapter_id") or "?")
        try:
            start = int(ch.get("page_start"))
            end = int(ch.get("page_end"))
        except (TypeError, ValueError):
            raise InputError(
                f"manifest 中旧章节 {cid} 缺少合法页区间，无法安全追加；"
                "请先「重建导入」后再试",
                detail={"chapter_id": cid}) from None
        if start < 1 or end < start or end > int(old_pages):
            raise InputError(
                f"manifest 中旧章节 {cid} 的页区间非法（{start}-{end}，全书 {old_pages} 页），"
                "请先「重建导入」后再追加",
                detail={"chapter_id": cid, "page_start": start, "page_end": end,
                        "old_pages": int(old_pages)})
        spans.append((start, end, cid))
    spans.sort()
    for prev, cur in zip(spans, spans[1:]):
        if cur[0] <= prev[1]:
            raise InputError(
                f"manifest 中旧章节页区间重叠（{prev[2]} {prev[0]}-{prev[1]} 与 "
                f"{cur[2]} {cur[0]}-{cur[1]}），请先「重建导入」后再追加",
                detail={"chapters": [prev[2], cur[2]]})


def _merged_manifest_updates(old: dict, parsed: dict, chapters: list[dict]) -> dict:
    """以旧 manifest 为身份基准，只覆盖解析产物相关字段。

    parse_pdf 会重写整份 manifest，暂存上的当前 manifest 是「新书」视角；
    title/profile/created_at/collision_map（以及未来未知字段）必须从旧 manifest 带回。
    """
    updates = {k: v for k, v in old.items() if k not in ("data_revision", "updated_at")}
    stage_hashes = dict(old.get("stage_hashes") or {})
    ingest = (parsed.get("stage_hashes") or {}).get("ingest")
    if ingest:
        stage_hashes["ingest"] = ingest
    updates.update({
        "pages": parsed.get("pages") or [],
        "pdf_path": parsed.get("pdf_path") or old.get("pdf_path"),
        "pdf_sha256": parsed.get("pdf_sha256") or old.get("pdf_sha256"),
        "parser": parsed.get("parser") or old.get("parser"),
        "ocr": bool(parsed.get("ocr")),
        "chapters": chapters,
        "stage_hashes": stage_hashes,
        "schema_version": parsed.get("schema_version", old.get("schema_version")),
        # 身份字段显式兜底：旧 manifest 缺字段时也不能把标题/模板清成 null
        "title": old.get("title") or parsed.get("title"),
        "profile": old.get("profile") or parsed.get("profile", "textbook"),
    })
    return updates


# ---------------------------------------------------------------------------
# 统一提交（swap）：journal → 锁内校验 revision → 备份 → 换入 → 重建索引；失败回滚
# ---------------------------------------------------------------------------
def _swap_into_place(layout: Layout, staging: Layout, doc_id: str,
                     expected_revision: int) -> None:
    """把暂存产物统一换入真实路径（非崩溃原子操作，由 journal 支撑恢复）。

    journal 阶段语义见模块文档；`committed` 是提交点：写入成功之后任何崩溃都只清理、
    绝不回滚，避免把已经生效的追加再撤掉。swap 前先原子落盘 journal，正常成功后删除。
    """
    real_doc = layout.doc_dir(doc_id)
    real_ingest = layout.ingest_root / doc_id
    real_pdf = layout.textbooks_dir / f"{doc_id}.pdf"
    staging_doc = staging.doc_dir(doc_id)
    staging_ingest = staging.ingest_root / doc_id
    staging_pdf = staging.textbooks_dir / f"{doc_id}.pdf"
    backup_root = _unique_backup_dir(layout, doc_id)
    backup = _backup_artifact_paths(backup_root)
    moved = {"doc": False, "ingest": False, "pdf": False}
    installed = {"doc": False, "ingest": False, "pdf": False}
    real_store = DocStore(layout, doc_id)
    journal_path = _journal_path(layout, doc_id)
    journal = {
        "version": JOURNAL_VERSION,
        "doc_id": doc_id,
        "expected_revision": int(expected_revision),
        "backup_root": _rel_to_root(layout, backup_root),
        "staging_root": _rel_to_root(layout, staging.root),
        "artifacts": list(REAL_ARTIFACT_KEYS),
        "phase": "swapping",
        "created_at": utc_now_iso(),
        # 换入标记文件名 + 换入后的 staging revision：恢复时用来区分「这份真实文档是
        # 本次未提交 swap 换入的」与「中断后用户继续编辑的真实文档」（P2-A3）。
        "install_marker": f".append-install-{uuid.uuid4().hex[:12]}",
        "installed_revision": None,
    }

    with doc_lock(layout, doc_id):
        current = int(real_store.manifest().get("data_revision", 1) or 1)
        if current != expected_revision:
            raise ConflictError(
                "追加期间该教材已被其它操作修改（data_revision 已变化），请重试",
                detail={"expected": expected_revision, "current": current})
        if journal_path.exists():
            raise PipelineError(
                "上次追加中断的恢复尚未完成（备份与恢复日志已保留），请重启服务后重试",
                detail={"stage": "append-swap", "journal": journal_path.name})
        try:
            _write_journal(journal_path, journal)
        except OSError as exc:
            raise PipelineError(
                f"无法写入追加恢复日志，未改动任何数据：{exc}",
                detail={"stage": "append-swap"}) from exc
        try:
            # 换入标记随暂存 doc 目录一起换入真实路径（P2-A3 恢复判据之一）；写失败即
            # 直接失败（此时三件产物都没动，回滚是空操作）。
            atomic_write_bytes(staging_doc / journal["install_marker"],
                               journal["install_marker"].encode("utf-8"))
            backup_root.mkdir(parents=True, exist_ok=False)
            _move(real_doc, backup["doc"])
            moved["doc"] = True
            if real_ingest.exists():
                _move(real_ingest, backup["ingest"])
                moved["ingest"] = True
            if real_pdf.exists():
                _move(real_pdf, backup["pdf"])
                moved["pdf"] = True
            _move(staging_doc, real_doc)
            installed["doc"] = True
            # 记下换入瞬间的 revision：崩溃恢复据此识别「真实目录已是本次换入的新文档」
            journal["installed_revision"] = _real_data_revision(real_doc)
            _write_journal(journal_path, journal)
            if staging_ingest.exists():
                _move(staging_ingest, real_ingest)
                installed["ingest"] = True
            if staging_pdf.exists():
                _move(staging_pdf, real_pdf)
                installed["pdf"] = True
            # 第三轮 P2：staging 期间对真实 doc 的事务外写入（题库/套题/图）在整目录
            # swap 时会丢；从备份（swap 前的真实目录）保守并回新目录。失败抛 OSError →
            # 下面统一走回滚（宁可整单失败，不可静默丢并发写入）。
            _merge_back_live_artifacts(backup["doc"], real_doc)
            # 暂存快照内部提交过多次（解析后 manifest 提交 + 每章锚定），对外仍是一次
            # 逻辑追加：data_revision 归一到 expected+1，保持单调递增与乐观锁语义
            _normalize_revision(layout, doc_id, expected_revision)
            DocStore._invalidate(layout, doc_id)
            from src.db.reindex import reindex_doc

            reindex_doc(layout, doc_id)
            # 提交点：committed 落盘后才算追加生效；此前任何崩溃都按 swapping 回滚
            journal.update({"phase": "committed", "committed_at": utc_now_iso()})
            _write_journal(journal_path, journal)
        except BaseException as exc:
            status = _rollback(layout, staging, doc_id, backup_root, backup, moved, installed)
            if status == "ok":
                _unlink_quiet(journal_path)
            elif status == "index_pending":
                # 数据已回滚但索引重建失败：标记 restored，下次启动只重试索引、不再动数据
                journal.update({"phase": "restored", "restored_at": utc_now_iso()})
                try:
                    _write_journal(journal_path, journal)
                except OSError:
                    pass
            if isinstance(exc, OSError):
                # Windows 占用/磁盘错误：给出可操作结论（旧数据已回滚，重试即可）
                raise PipelineError(
                    f"追加提交失败（文件被占用或磁盘错误），原数据已回滚，请重试：{exc}",
                    detail={"stage": "append-swap"}) from exc
            raise
        # 已提交：先删换入标记，再删备份、最后删 journal（journal 最后删——任何时刻
        # 崩溃都能靠它判断阶段；标记删不掉则保留 journal，下次启动重试）。
        marker_ok = _remove_install_marker(real_doc, journal)
        _rmtree_retry(backup_root)
        if backup_root.exists():
            print(f"[append] 追加已生效，但备份目录清理失败（下次启动会重试）：{backup_root}",
                  file=sys.stderr)
        elif marker_ok:
            _unlink_quiet(journal_path)


def _normalize_revision(layout: Layout, doc_id: str, expected_revision: int) -> None:
    """swap 后把 manifest.data_revision 归一到「旧 revision + 1」（一次逻辑追加）。

    暂存解析从 revision=1 起步，若直接换入会让真实 revision 大幅回退——破坏
    「data_revision 单调」不变量，极端情况下让很旧的乐观锁基线重新变得有效。
    """
    path = layout.manifest(doc_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["data_revision"] = int(expected_revision) + 1
    data["updated_at"] = utc_now_iso()
    atomic_write_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                       .encode("utf-8"))


# ---------------------------------------------------------------------------
# 第三轮 P2：swap 期间事务外活产物 merge-back（题库 / 套题 / 图）
# ---------------------------------------------------------------------------
def _copy_bytes_retry(src: Path, dst: Path, attempts: int = 4) -> None:
    """源文件 → 目标原子写 + 短重试（Windows 目标文件可能被并发读者占用）。"""
    last: OSError | None = None
    for i in range(attempts):
        try:
            atomic_write_bytes(dst, src.read_bytes())
            return
        except OSError as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(min(1.0, 0.05 * (2 ** i)))
    if last is not None:
        raise last


def _merge_back_live_artifacts(source_doc: Path, target_doc: Path) -> None:
    """把 swap 期间事务外写入真实 doc 的活产物并回即将生效的新目录（第三轮 P2）。

    背景：追加的 staging 阶段（解析 + 逐章锚定）可能持续数分钟；以下写盘不经过事务、
    也不 bump data_revision，整目录 swap 会把它们静默覆盖回 staging 快照状态：
      - `qbank.jsonl`   上传题库：append_qbank 直接追加，快照后的新条目只在真实目录；
      - `quiz/*.json`   AI 出题：generate_quiz 直接落盘，生成本身就是漫长 LLM 调用；
      - `figures/*.png` 刷新图：refresh_figures 在事务外写 png（随后的提交会 bump
        revision，多数情况已触发追加冲突，但 png 落盘与提交之间有极窄竞态）。
    因此换入新目录后、归一 revision 前，从 `backup/doc`（swap 前的真实目录）保守并回。
    受事务管理的主数据（manifest/blocks/anchors/history/refs/index.db/transactions）绝不
    并回：它们以 staging 上已提交的新状态为准。

    合并策略（保守，任何一方都不丢）：
      - qbank：纯追加写语义 → 备份版本整体覆盖（= 旧条目 + staging 期间新增条目）；
      - quiz：文件名唯一（quiz_id 含时间戳）→ 备份里全部 *.json 覆盖/补齐；
      - figures：同名以新目录为准（本次追加重裁的图更新），只补目标缺失的 png。

    任何一件失败都抛 OSError：调用方回滚整次追加（宁可失败，不可静默丢数据）。
    """
    if not source_doc.is_dir():
        return                      # 无备份目录（非 swap 路径）：没有可并回的活产物
    target_doc.mkdir(parents=True, exist_ok=True)

    qbank_src = source_doc / "qbank.jsonl"
    if qbank_src.is_file():
        _copy_bytes_retry(qbank_src, target_doc / "qbank.jsonl")

    quiz_src = source_doc / "quiz"
    if quiz_src.is_dir():
        for src in sorted(quiz_src.glob("*.json")):
            if src.is_file():
                _copy_bytes_retry(src, target_doc / "quiz" / src.name)

    figures_src = source_doc / "figures"
    if figures_src.is_dir():
        for src in sorted(figures_src.glob("*.png")):
            dst = target_doc / "figures" / src.name
            if src.is_file() and not dst.exists():
                _copy_bytes_retry(src, dst)


def _move(src: Path, dst: Path, attempts: int = 8) -> None:
    """os.replace + 指数退避短重试（A3）：Windows 并发读者可能短暂占用源文件。

    并发读者（SQLite 索引/锚点面板）打开着文件时 rename 会短暂 PermissionError；
    正常换入是毫秒级，只有持续被占用才会用满重试窗口（0.05/0.1/0.2/0.4/0.8/1.6/2.0，
    合计约 5.1s，单次退避上限 2s）——比原来的 6 次线性（~0.75s）更能熬过读者的一次
    整章扫描，也不会把失败路径拖到几十秒。
    """
    last: OSError | None = None
    for i in range(attempts):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            return
        except OSError as exc:
            last = exc
            if i + 1 < attempts:
                time.sleep(min(2.0, 0.05 * (2 ** i)))
    if last is not None:
        raise last
    raise OSError(f"无法移动文件: {src} -> {dst}")


def _unique_backup_dir(layout: Layout, doc_id: str) -> Path:
    stamp = utc_now_iso().replace("-", "").replace(":", "").replace("T", "")
    base = layout.data_dir / f"{BACKUP_PREFIX}{doc_id}-{stamp}"
    out = base
    n = 1
    while out.exists():
        out = base.with_name(f"{base.name}-{n}")
        n += 1
    return out


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def _rmtree_retry(path: Path, attempts: int = 5) -> None:
    """删除目录 + 短重试：Windows 上并发读者可能短暂占用里面的 SQLite 文件。"""
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if i + 1 < attempts:
                time.sleep(0.05 * (i + 1))
    shutil.rmtree(path, ignore_errors=True)


def _rollback(layout: Layout, staging: Layout, doc_id: str, backup_root: Path,
              backup: dict[str, Path], moved: dict[str, bool],
              installed: dict[str, bool]) -> str:
    """swap 中途失败：把已换入的新产物送回暂存，再从备份恢复原始产物。

    同卷 rename 优先（Windows 占用时删不掉但能改名，用 _move 的重试窗口吸收短暂占用）；
    任何一步失败都保留备份目录并在 stderr 指明位置，绝不静默丢掉旧数据。
    返回 'ok'（旧数据与索引都已恢复）/ 'index_pending'（数据已恢复、索引待重建）/
    'failed'（有产物没恢复成功，备份目录已保留待 journal 重试）。
    """
    errors: list[str] = []
    pairs = {
        "doc": (layout.doc_dir(doc_id), staging.doc_dir(doc_id)),
        "ingest": (layout.ingest_root / doc_id, staging.ingest_root / doc_id),
        "pdf": (layout.textbooks_dir / f"{doc_id}.pdf",
                staging.textbooks_dir / f"{doc_id}.pdf"),
    }
    for key, (real_path, staging_path) in pairs.items():
        if not installed.get(key) or not real_path.exists():
            continue
        try:
            if staging_path.exists():
                _remove_path(staging_path)
            _move(real_path, staging_path)
        except OSError as exc:
            errors.append(f"新产物移回暂存失败 {real_path}: {exc}")
            _remove_path(real_path)
    for key, (real_path, _) in pairs.items():
        src = backup.get(key)
        if not moved.get(key) or src is None or not src.exists():
            continue
        try:
            if real_path.exists():
                _remove_path(real_path)
            _move(src, real_path)
        except OSError as exc:
            errors.append(f"从备份恢复失败 {real_path}: {exc}")
    try:
        DocStore._invalidate(layout, doc_id)
    except Exception:  # noqa: BLE001 - 回滚路径不得再抛新错
        pass
    if errors:
        print(f"[append] 回滚未完全成功，原始产物保留在 {backup_root}："
              + "；".join(errors), file=sys.stderr)
        return "failed"
    _rmtree_retry(backup_root)
    # 旧 index.db 随备份/恢复回退，但 data/runtime.db 的 doc_map 可能已被 swap 期间的
    # reindex 改指向新块/新锚点 id——回滚后必须重建，否则点查映射指向已不存在的 id。
    if not _rebuild_after_restore(layout, doc_id):
        return "index_pending"
    return "ok"


def _close_staging_runtime(staging_root: Path) -> None:
    """关闭暂存 layout 的 runtime.db 连接（Windows 上打开着就删不掉暂存目录）。

    reindex_doc 经 get_runtime 进程级缓存为暂存 runtime.db 建了长连接；暂存目录
    必须能整个删除，否则失败路径也会留下 .append-staging-* 残留。
    """
    try:
        from src.db import runtime as runtime_mod

        registry = getattr(runtime_mod, "_RUNTIME", None)
        if not isinstance(registry, dict):
            return
        key = str((staging_root / "data" / "runtime.db").resolve())
        db = registry.pop(key, None)
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001 - 清理路径不上抛
                pass
    except Exception:  # noqa: BLE001 - 清理路径不上抛
        pass


# ---------------------------------------------------------------------------
# A1 swap 崩溃恢复：journal（swapping → restored → committed）+ 幂等回滚
# ---------------------------------------------------------------------------
def _journal_path(layout: Layout, doc_id: str) -> Path:
    """每 doc 一份恢复日志（swap 由同一把 doc 锁串行，不会并发写）。"""
    return layout.data_dir / f"{JOURNAL_PREFIX}{doc_id}.json"


def _rel_to_root(layout: Layout, path: Path) -> str:
    """journal 内路径统一存「相对 layout.root 的 POSIX 路径」，项目目录搬家后仍可恢复。"""
    try:
        return Path(path).resolve().relative_to(layout.root).as_posix()
    except (OSError, ValueError):
        return str(path)


def _read_journal(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_journal(path: Path, payload: dict) -> None:
    atomic_write_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                       .encode("utf-8"))


def _unlink_quiet(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"[append] 删除恢复日志失败（下次启动会重试）：{path}: {exc}", file=sys.stderr)
        return False


def _journal_root_path(layout: Layout, raw, prefix: str) -> Path | None:
    """journal 里的 backup/staging 路径 → 绝对路径；只认 data 下带指定前缀的目录。

    防 journal 被损坏/篡改后诱导恢复流程删除 data 之外的数据（路径注入）。
    """
    if not raw:
        return None
    p = Path(str(raw))
    if not p.is_absolute():
        p = layout.root / p
    try:
        resolved = p.resolve()
        data_dir = layout.data_dir.resolve()
    except OSError:
        return None
    if resolved.parent != data_dir or not resolved.name.startswith(prefix):
        return None
    return resolved


def _backup_artifact_paths(backup_root: Path) -> dict[str, Path]:
    return {"doc": backup_root / "doc",
            "ingest": backup_root / "ingest",
            "pdf": backup_root / "textbook.pdf"}


def _real_artifact_paths(layout: Layout, doc_id: str) -> dict[str, Path]:
    return {"doc": layout.doc_dir(doc_id),
            "ingest": layout.ingest_root / doc_id,
            "pdf": layout.textbooks_dir / f"{doc_id}.pdf"}


def _staging_artifact_path(staging_root: Path | None, doc_id: str, key: str) -> Path | None:
    if staging_root is None:
        return None
    base = Layout(staging_root)
    if key == "doc":
        return base.doc_dir(doc_id)
    if key == "ingest":
        return base.ingest_root / doc_id
    return base.textbooks_dir / f"{doc_id}.pdf"


def _rebuild_after_restore(layout: Layout, doc_id: str) -> bool:
    """恢复旧产物后重建派生索引（index.db + runtime.db 的 doc_map）；失败返回 False。

    旧 index.db 随备份一起恢复，但真实 data/runtime.db 的 doc_map 可能已被 swap 期间
    的 reindex 改指向「新块/新锚点 id」。JSONL 是事实源：索引重建失败不改动已恢复的
    数据，保留 journal 待下次启动重试（查询侧 ensure_index_fresh 也会兜底重建）。
    """
    try:
        DocStore._invalidate(layout, doc_id)
        from src.db.reindex import reindex_doc

        reindex_doc(layout, doc_id)
        return True
    except Exception as exc:  # noqa: BLE001 - 索引是派生层，失败不得再动已恢复的数据
        print(f"[append] 数据已恢复，索引待重建（下次启动/查询会自动重建）：{doc_id}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    finally:
        try:
            DocStore._invalidate(layout, doc_id)
        except Exception:  # noqa: BLE001
            pass


def _doc_trashed(layout: Layout, doc_id: str) -> bool:
    return any((layout.data_dir / ".trash").glob(f"{doc_id}-*"))


def _backup_has_any(backup: dict[str, Path]) -> bool:
    return any(p.exists() for p in backup.values())


def _clear_real_artifact(real_path: Path, side_path: Path | None) -> None:
    """把真实路径上的当前件（可能已被换成新产物）移开。

    优先同卷 rename 到暂存（Windows 上删除可能被占用，改名通常可行），失败再删除；
    仍然移不开就抛 OSError——调用方据此保留备份，绝不带病覆盖。
    """
    if not real_path.exists():
        return
    if side_path is not None:
        try:
            side_path.parent.mkdir(parents=True, exist_ok=True)
            if side_path.exists():
                _remove_path(side_path)
            os.replace(real_path, side_path)
            return
        except OSError:
            pass
    _remove_path(real_path)
    if real_path.exists():
        raise OSError(f"无法移开当前产物: {real_path}")


def _cleanup_swap_artifacts(staging_root: Path | None, backup_root: Path | None,
                            journal_path: Path, *, keep_journal: bool = False) -> bool:
    """清理暂存与备份；两者都删干净后才删 journal（否则留待下次启动重试）。"""
    clean = True
    if staging_root is not None:
        _close_staging_runtime(staging_root)
        _rmtree_retry(staging_root)
        if staging_root.exists():
            clean = False
            print(f"[append] 追加暂存目录清理失败（下次启动重试）：{staging_root}",
                  file=sys.stderr)
    if backup_root is not None:
        _rmtree_retry(backup_root)
        if backup_root.exists():
            clean = False
            print(f"[append] 追加备份目录清理失败（下次启动重试）：{backup_root}",
                  file=sys.stderr)
    if clean and not keep_journal:
        _unlink_quiet(journal_path)
    return clean


def _real_data_revision(doc_dir: Path) -> int | None:
    """读真实 doc 的 manifest.data_revision；缺失/损坏返回 None。"""
    try:
        data = json.loads((doc_dir / "manifest.json").read_text(encoding="utf-8"))
        return int(data.get("data_revision", 0) or 0)
    except (OSError, ValueError, TypeError):
        return None


def _remove_install_marker(doc_dir: Path, journal: dict) -> bool:
    """删除 swap 换入标记；返回是否已清理干净（失败时保留 journal 下次重试）。

    只接受本模块生成的 basename（前缀 + 无路径分隔符），防 journal 被篡改后误删。
    """
    name = str(journal.get("install_marker") or "")
    if not name or "/" in name or "\\" in name or not name.startswith(".append-install-"):
        return True
    path = doc_dir / name
    if not path.exists():
        return True
    try:
        path.unlink()
        return True
    except OSError as exc:
        print(f"[append] 删除换入标记失败（下次启动重试）：{path}: {exc}", file=sys.stderr)
        return False


def _newer_than_interrupted_swap(journal: dict, real_doc: Path,
                                 real_revision: int) -> bool:
    """P2-A3：真实数据是否已超出「本次中断 swap 的基线」（=用户中断后又编辑过）。

    仅按 revision > expected 判断会把「换入到一半的新暂存文档」误判成用户编辑
    （staging 内部提交多次，其 revision 本就可能高于旧 revision）。因此结合两项证据：

    - 换入标记文件仍在真实 doc 目录里 → 这份文档确实是本次 swap 换入的新文档；
    - 其 revision 不超过「换入瞬间记录的 installed_revision」或「归一化目标
      expected+1」→ 之后没有新提交。

    标记缺失（真实目录是旧数据/被恢复过）或 revision 更高 → 视为用户已有新编辑，
    不得用备份覆盖。
    """
    expected = journal.get("expected_revision")
    if expected is None:
        return True                     # 无法判断：保守拒绝自动回滚
    expected = int(expected)
    if int(real_revision) <= expected:
        return False                    # 不比基线新：没有中断后的新编辑
    marker = journal.get("install_marker")
    marker_present = False
    if isinstance(marker, str) and marker and "/" not in marker and "\\" not in marker:
        marker_present = (real_doc / marker).exists()
    if not marker_present:
        return True                     # 真实目录不是本次换入的新文档 → 有用户新编辑
    installed = journal.get("installed_revision")
    candidates = [expected + 1]
    if installed is not None:
        candidates.append(int(installed))
    return int(real_revision) > max(candidates)


def _raise_pending_recovery(journal_path: Path) -> None:
    """run_append 开工时 journal 仍在 → 拒绝在半恢复/被推迟的现场上再叠一次换入。"""
    data = _read_journal(journal_path) or {}
    phase = str(data.get("phase") or "swapping")
    detail = {"stage": "append-recover", "journal": journal_path.name, "phase": phase}
    if phase == "deferred":
        raise PipelineError(
            "上次追加中断后该教材又被编辑过（真实 data_revision 已超过中断时的基线），"
            "为避免覆盖新编辑，备份与恢复日志已保留且未自动回滚；请先人工确认/导出，"
            "再清理 data/ 下对应的 .bak-append-* 与 .append-journal-* 后重试",
            detail=detail)
    raise PipelineError(
        "上次追加中断的恢复尚未完成（备份与恢复日志已保留），请重启服务后重试",
        detail=detail)


def _preserve_staging_reports(layout: Layout, staging: Layout, doc_id: str
                              ) -> tuple[list[str], dict, dict[str, str]]:
    """P3-A1：把失败诊断件从暂存复制到真实 reports（暂存 finally 会被删）。

    chunker 把 migration 报告写到 staging layout 的 reports/<doc>/，异常消息里的路径
    随暂存目录删除而失效。失败时复制到真实 reports/<doc>/（文件名加 .append-failed，
    避免覆盖上次报告），返回 (副本路径, 摘要, 原路径→副本路径 映射) 供调用方改写
    异常消息里的失效路径。
    """
    src_dir = staging.reports_doc_dir(doc_id)
    if not src_dir.is_dir():
        return [], {}, {}
    paths: list[str] = []
    mapping: dict[str, str] = {}
    unresolved = broken = 0
    for src in sorted(src_dir.glob("*.migration.json")):
        try:
            payload = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        unresolved += len(payload.get("unresolved") or [])
        broken += len(payload.get("broken_sources") or [])
        name = src.name
        if name.endswith(".migration.json"):
            name = name[: -len(".migration.json")] + ".append-failed.migration.json"
        else:
            name = src.stem + ".append-failed" + src.suffix
        target = layout.reports_doc_dir(doc_id) / name
        try:
            atomic_write_bytes(target, src.read_bytes())
            paths.append(str(target))
            mapping[str(src)] = str(target)
        except OSError as exc:
            print(f"[append] 失败诊断件复制失败：{target}: {exc}", file=sys.stderr)
    return paths, {"unresolved": unresolved, "broken_sources": broken}, mapping


def _recover_one(layout: Layout, doc_id: str, journal_path: Path, journal: dict) -> str:
    """恢复单条 journal；返回 recovered/cleaned/committed/deferred/failed。

    逐件判断 doc/ingest/pdf：备份里存在哪件就把真实路径上的当前件移开并换回该件；
    备份里没有的件说明崩溃发生在移动它之前，真实路径仍是旧数据，保持不动。
    若真实 data_revision 已超过 journal.expected_revision（中断后用户又编辑过），
    不用备份覆盖真实数据，只标 deferred 并保留现场（P2-A3）。
    """
    phase = str(journal.get("phase") or "swapping")
    raw_backup = journal.get("backup_root")
    backup_root = _journal_root_path(layout, raw_backup, BACKUP_PREFIX)
    staging_root = _journal_root_path(layout, journal.get("staging_root"), STAGING_PREFIX)
    if raw_backup and backup_root is None:
        # journal 指向 data 之外的路径：拒绝自动恢复，保留一切现场（防路径注入误删）
        print(f"[append] 恢复日志的备份路径不在 data 下，拒绝自动恢复：{journal_path}", file=sys.stderr)
        return "failed"
    real_paths = _real_artifact_paths(layout, doc_id)
    backup = _backup_artifact_paths(backup_root) if backup_root is not None else {}

    if phase == "committed":
        # 新数据已换入且 reindex 成功：只清理，绝不回滚已生效的追加
        marker_ok = _remove_install_marker(real_paths["doc"], journal)
        _cleanup_swap_artifacts(staging_root, backup_root, journal_path,
                                keep_journal=not marker_ok)
        return "committed"

    # P2-A3：若上次 swap 失败后服务继续运行、用户又编辑（data_revision 增长），备份已经
    # 落后于真实数据；此时自动回滚会覆盖新编辑。判断依据不只是 revision（换入一半的
    # 暂存新文档 revision 也可能更高），而是「真实目录是否还是本次换入的未提交新文档」
    # （换入标记 + installed_revision，见 _newer_than_interrupted_swap）。
    if phase in ("swapping", "deferred") and _backup_has_any(backup):
        real_revision = _real_data_revision(real_paths["doc"])
        if (real_revision is not None
                and _newer_than_interrupted_swap(journal, real_paths["doc"], real_revision)):
            journal.update({"phase": "deferred", "deferred_at": utc_now_iso(),
                            "deferred_real_revision": int(real_revision)})
            try:
                _write_journal(journal_path, journal)
            except OSError as exc:
                print(f"[append] 写入 deferred 恢复日志失败：{journal_path}: {exc}",
                      file=sys.stderr)
            print(f"[append] 该教材在中断后又有新编辑（data_revision={real_revision}，"
                  f"中断基线 {journal.get('expected_revision')}），已跳过自动回滚并保留现场："
                  f"{journal_path}", file=sys.stderr)
            return "deferred"

    if not real_paths["doc"].exists():
        if _doc_trashed(layout, doc_id) or not backup.get("doc") or not backup["doc"].exists():
            # 文档已被删除/移入回收站，或备份里没有 doc：不擅自把旧文档「复活」；
            # 只清 journal 与暂存，备份若含旧数据则保留在 data 下并告警（不静默删数据）
            keep = backup_root if backup_root is not None and _backup_has_any(backup) else None
            print(f"[append] 文档 {doc_id} 的目录不存在（已删除/移入回收站，或备份不含 doc），"
                  "跳过崩溃回滚" + (f"；旧数据备份保留在 {keep}" if keep else ""), file=sys.stderr)
            _cleanup_swap_artifacts(staging_root, None, journal_path)
            return "cleaned"

    if phase == "restored":
        # 旧数据本已恢复，只差索引重建：只重试索引，绝不再动已恢复的数据
        if not _rebuild_after_restore(layout, doc_id):
            print(f"[append] 数据已恢复但索引重建仍失败，保留恢复日志待下次启动重试："
                  f"{journal_path}", file=sys.stderr)
            return "deferred"
        _cleanup_swap_artifacts(staging_root, backup_root, journal_path)
        return "recovered"

    # phase == "swapping"：备份里没有任何产物 → 崩溃在移动前，真实数据未被改动
    if backup_root is None or not backup_root.exists() or not _backup_has_any(backup):
        _cleanup_swap_artifacts(staging_root, backup_root, journal_path)
        return "cleaned"

    side_root = staging_root if staging_root is not None and staging_root.exists() else None
    errors: list[str] = []
    for key in REAL_ARTIFACT_KEYS:
        src = backup[key]
        if not src.exists():
            continue
        real_path = real_paths[key]
        try:
            _clear_real_artifact(real_path, _staging_artifact_path(side_root, doc_id, key))
            _move(src, real_path)
        except OSError as exc:
            errors.append(f"{key} -> {real_path}: {exc}")
    DocStore._invalidate(layout, doc_id)
    if errors:
        print(f"[append] 崩溃恢复未完成（下次启动重试），备份保留在 {backup_root}："
              + "；".join(errors), file=sys.stderr)
        return "failed"

    # 数据已恢复：先落 restored 阶段再重建索引（索引失败时可重启重试，不再动数据）
    journal.update({"phase": "restored", "restored_at": utc_now_iso()})
    try:
        _write_journal(journal_path, journal)
    except OSError as exc:
        print(f"[append] 写入恢复阶段失败（继续重建索引）：{journal_path}: {exc}", file=sys.stderr)
    expected = journal.get("expected_revision")
    if expected is not None:
        try:
            restored_revision = int(DocStore(layout, doc_id).manifest().get("data_revision", 0))
        except Exception:  # noqa: BLE001 - 诊断信息，读不到就算了
            restored_revision = -1
        if restored_revision != int(expected):
            # 备份里的 manifest 本应是旧 revision；不一致只告警，以备份数据为准（不额外改写）
            print(f"[append] 恢复后 data_revision={restored_revision} 与 journal 记录 "
                  f"{expected} 不一致（以备份数据为准）", file=sys.stderr)
    if not _rebuild_after_restore(layout, doc_id):
        print(f"[append] 旧数据已回滚但索引重建失败，保留备份与恢复日志待下次启动重试："
              f"{backup_root}", file=sys.stderr)
        return "deferred"
    _cleanup_swap_artifacts(staging_root, backup_root, journal_path)
    return "recovered"


def recover_interrupted_append(layout: Layout, *, only_doc: str | None = None) -> dict:
    """扫描 data/.append-journal-*.json，恢复被强杀中断的追加 swap（A1）。

    swap 不是崩溃原子操作（备份 → 换入三件产物 → 归一 revision → reindex → 清理），
    进程被强杀会留下部分换入的真实目录与 `.bak-append-*` 备份。本函数按 journal 阶段
    在 doc_lock 内逐条恢复，幂等可重跑：

    - `swapping`：按备份逐件回滚旧产物，然后重建派生索引；
    - `restored`：旧数据已回滚、索引重建失败 → 只重试重建索引；
    - `committed`：追加已生效 → 只清理备份/日志，绝不回滚；
    - `deferred`：真实 data_revision 已超过中断基线（中断后用户又编辑过）→ 不自动
      回滚、保留备份与日志，等人工确认；恢复不会覆盖新编辑（P2-A3）。

    调用点：src/api/main.py 的 lifespan 启动钩子（只在实际服务启动时，避免测试/并行
    进程误恢复）+ run_append 开头（防御上次崩溃未恢复就再次追加）。
    单条失败只告警并保留备份/日志，绝不静默删旧数据；`only_doc` 未命中时直接返回。
    """
    report: dict[str, list[str]] = {"recovered": [], "cleaned": [], "committed": [],
                                    "deferred": [], "failed": []}
    if only_doc is not None:
        candidate = _journal_path(layout, only_doc)
        journals = [candidate] if candidate.exists() else []
    else:
        journals = sorted(layout.data_dir.glob(f"{JOURNAL_PREFIX}*.json"))
    for journal_path in journals:
        data = _read_journal(journal_path)
        if data is None:
            report["failed"].append(journal_path.name)
            print(f"[append] 恢复日志无法解析，已保留原样（需人工检查）：{journal_path}",
                  file=sys.stderr)
            continue
        doc_id = str(data.get("doc_id")
                     or journal_path.name[len(JOURNAL_PREFIX):-len(".json")])
        try:
            check_doc_id(doc_id)
        except InputError:
            report["failed"].append(journal_path.name)
            print(f"[append] 恢复日志的 doc_id 非法，已保留原样：{journal_path}", file=sys.stderr)
            continue
        try:
            with doc_lock(layout, doc_id):
                status = _recover_one(layout, doc_id, journal_path, data)
        except Exception as exc:  # noqa: BLE001 - 启动恢复失败不得炸穿服务启动
            report["failed"].append(doc_id)
            print(f"[append] 恢复中断追加失败（已保留备份与日志）：{doc_id}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        report[status].append(doc_id)
    return report
