"""文档/PDF/图/页/选区/检索/生成触发端点（9.2）。"""

from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from src.core.errors import (ConflictError, InputError, NoteGenError,
                             NotFound, PipelineError, PriceUnknown,
                             TaskCancelled)
from src.core.llm import operation_client
from src.core.paths import DOC_ID_RE, FIGURE_ID_RE
from src.core.store import DocMutation, DocStore
from src.db import queries
from src.ingest.parse_pdf import parse_pdf_with_fallback

router = APIRouter()


def state(request: Request):
    return request.app.state.app_state


@router.get("/api/v1/docs")
def list_docs(request: Request):
    st = state(request)
    out = []
    for path in sorted(st.layout.data_dir.glob("*/manifest.json")):
        if not DOC_ID_RE.match(path.parent.name):
            continue            # 跳过 .bak-* 等备份目录
        try:
            import json

            m = json.loads(path.read_text(encoding="utf-8"))
            out.append({"doc_id": m["doc_id"], "title": m.get("title"),
                        "profile": m.get("profile", "textbook"),
                        "chapters": m.get("chapters", []),
                        "data_revision": m.get("data_revision"),
                        "updated_at": m.get("updated_at")})
        except Exception:
            continue
    return {"docs": out}


@router.post("/api/v1/docs")
async def create_doc(request: Request, file: UploadFile = File(...), title: str | None = Form(None),
                     profile: str = Form("textbook"), doc_id: str | None = Form(None),
                     overwrite: bool = Form(False)):
    st = state(request)
    profile = profile or ((st.config.get("generate", {}) or {}).get("profile") or "textbook")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".pdf", ".pptx"):
        raise InputError("只支持 PDF 与 PPTX 讲义")
    if not (file.filename or "").strip():
        raise InputError("文件名为空")
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise InputError("PDF 超过 200 MiB 限制")
    import hashlib

    doc = doc_id or ("doc-" + hashlib.sha256(data).hexdigest()[:12])
    if not DOC_ID_RE.match(doc):
        raise InputError(f"doc_id 非法: {doc}")
    # 重复导入（同哈希/同 doc_id）：默认拒绝，避免覆盖已有笔记；overwrite 显式确认后重建
    if st.layout.manifest(doc).exists():
        if not overwrite:
            raise ConflictError(
                f"文档已存在：{doc}（同名/同哈希 PDF 已导入）",
                detail={"doc_id": doc,
                        "hint": "如需重新导入，请先在 UI 删除该教材，或传 overwrite=true（将重建解析产物）"})
        import shutil

        shutil.rmtree(st.layout.doc_dir(doc), ignore_errors=True)
    st.layout.textbooks_dir.mkdir(parents=True, exist_ok=True)
    target = st.layout.textbooks_dir / f"{doc}{suffix}"
    target.write_bytes(data)

    def run(req, progress, is_cancelled, register_artifact, task_id=None):
        progress({"stage": "ingest", "pages_done": 0, "pages_total": None})
        if suffix == ".pptx":
            # PPTX 讲义：python-pptx 直接抽幻灯片（文本/几何/插图），合成与 PDF
            # 同形的 pages JSON——锚点/生成/检索管线原样复用
            from src.ingest.parse_pptx import parse_pptx

            manifest = parse_pptx(target, st.layout.ingest_root, layout=st.layout, doc_id=doc,
                                  profile=profile, title=title or Path(file.filename).stem,
                                  chapters_override=(st.config.get("chapters_override") or None),
                                  progress=progress)
        else:
            manifest = parse_pdf_with_fallback(target, st.layout.ingest_root,
                                               st.config.get("parser", {}).get("default", "auto"),
                                               layout=st.layout, doc_id=doc, profile=profile,
                                               title=title or Path(file.filename).stem,
                                               config=st.config, progress=progress,
                                               vision_ocr=_vision_ocr_for(st))
        from src.anchor.chunker import chunk_and_anchor

        counts = {}
        chapters = manifest["chapters"]
        for idx, ch in enumerate(chapters):
            # 按章报锚定进度（9.2）：导入是两段式，进度条要能持续推进
            progress({"stage": "anchor", "done": idx, "total": len(chapters),
                      "chapter_id": ch["chapter_id"]})
            counts[ch["chapter_id"]] = chunk_and_anchor(doc, ch["chapter_id"], layout=st.layout)
        progress({"stage": "anchor", "done": len(chapters), "total": len(chapters)})
        return {"doc_id": doc, "chapters": manifest["chapters"], "anchors": counts}

    task = st.tasks.submit("import", run, {"doc_id": doc, "filename": file.filename},
                           doc_id=doc, idempotency_key=None)
    return JSONResponse({"task_id": task["task_id"], "doc_id": doc}, status_code=202)


@router.post("/api/v1/docs/{doc_id}/rebuild")
def rebuild_doc(request: Request, doc_id: str, body: dict | None = None):
    """重建导入（不重新上传 PDF）：重跑解析 + 分章 + 锚定，
    用于拾取新的解析器（如中文保真抽取）与新的分章规则。
    仅允许无笔记块的文档——已有块按旧 chapter_id 归属，重建会错位。"""
    st = state(request)
    store = DocStore(st.layout, doc_id)
    manifest = store.manifest()
    if store.all_block_rows(include_deleted=False):
        raise ConflictError(
            "文档已有笔记块，重建导入会使其与新章节错位；请先导出笔记或删除块后再试",
            code="HAS_BLOCKS")
    pdf = st.layout.root / manifest["pdf_path"]
    if not pdf.exists():
        raise NotFound("PDF 文件不存在，无法重建")
    title = manifest.get("title")
    profile = manifest.get("profile", "textbook")

    def run(req, progress, is_cancelled, register_artifact, task_id=None):
        import shutil

        from src.anchor.chunker import chunk_and_anchor
        from src.db.runtime import get_runtime

        # 旧解析/锚定/图资源整体作废（PDF 保留在 data/textbooks 不动）
        shutil.rmtree(st.layout.doc_dir(doc_id), ignore_errors=True)
        shutil.rmtree(st.layout.ingest_root / doc_id, ignore_errors=True)
        get_runtime(st.layout.runtime_db).forget_doc(doc_id)
        progress({"stage": "ingest", "pages_done": 0, "pages_total": None})
        manifest_new = parse_pdf_with_fallback(
            pdf, st.layout.ingest_root, st.config.get("parser", {}).get("default", "auto"),
            layout=st.layout, doc_id=doc_id, profile=profile, title=title,
            config=st.config, progress=progress, vision_ocr=_vision_ocr_for(st))
        chapters = manifest_new["chapters"]
        counts = {}
        for idx, ch in enumerate(chapters):
            if is_cancelled():
                raise TaskCancelled("任务已取消")
            progress({"stage": "anchor", "done": idx, "total": len(chapters),
                      "chapter_id": ch["chapter_id"]})
            counts[ch["chapter_id"]] = chunk_and_anchor(doc_id, ch["chapter_id"], layout=st.layout)
        progress({"stage": "anchor", "done": len(chapters), "total": len(chapters)})
        return {"doc_id": doc_id, "chapters": chapters, "anchors": counts}

    task = st.tasks.submit("import", run, {"doc_id": doc_id, "mode": "rebuild"},
                           doc_id=doc_id)
    return JSONResponse({"task_id": task["task_id"], "doc_id": doc_id}, status_code=202)


@router.post("/api/v1/docs/{doc_id}/refresh-figures")
def refresh_figures_api(request: Request, doc_id: str, body: dict | None = None):
    """刷新图资源（不重新导入）：从 PDF 内嵌图登记 figures/ 与图锚点，
    并把既有笔记里悬空的 ![{fig}] 占位引用改指到真实图（图在源锚点同页且唯一时）。"""
    import re

    from src.anchor.chunker import chunk_and_anchor
    from src.core.errors import RevisionConflict
    from src.core.text import utc_now_iso
    from src.ingest.figures import pick_ref_target, refresh_figures

    st = state(request)
    store = DocStore(st.layout, doc_id)
    manifest = store.manifest()
    pdf = st.layout.root / manifest["pdf_path"]
    figs = refresh_figures(pdf, st.layout, doc_id,
                           parser_python=(st.config.get("parser", {}) or {}).get("python"))

    # 重锚定（5.5 来源迁移）：pages JSON 补了 figure 块 → 生成图锚点，
    # 既有块的 sources 由迁移对配自动维护（锚点 id 是内容哈希，稳定）
    for ch in store.chapter_ids():
        chunk_and_anchor(doc_id, ch, layout=st.layout)

    # 既有笔记的悬空图片引用修复：源锚点同页恰有一张图 → 改指真实 id
    fig_dir = st.layout.figures_dir(doc_id)
    pages_dir = st.layout.ingest_root / doc_id / "pages"
    fig_by_page: dict[int, list[tuple[str, list]]] = {}
    for pf in pages_dir.glob("*.json"):
        try:
            d = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for b in d.get("blocks", []):
            if b.get("figure_id"):
                fig_by_page.setdefault(int(d.get("page", 0)), []).append(
                    (b["figure_id"], b.get("bbox") or [0, 0, 0, 0]))
    ref_re = re.compile(r"!\[(fig_\d{8})\]")
    now = utc_now_iso()
    patched = 0

    def _vgap(a: list, f: list) -> float:
        """锚点与图的纵向间距（重叠为 0）：源文本通常紧贴所引插图。"""
        return max(0.0, float(f[1]) - float(a[3]), float(a[1]) - float(f[3]))
    for ch in store.chapter_ids():
        for attempt in range(2):
            # CAS 基线取在读取时刻（I7/11.6.3 丢更新防线）：基线取在提交时刻时，
            # 读-改-写窗口内的并发改动会被整文件替换静默冲掉；现在冲突即整章重做一次
            base_rev = store.data_revision()
            rows = [dict(b) for b in store.block_rows(ch, include_deleted=True)]
            anchors_by_id = {a["anchor_id"]: a for a in store.anchor_rows(ch)}
            history = []
            changed = False
            n_patched = 0
            for b in rows:
                if b.get("deleted_at"):
                    continue
                content = b.get("content") or ""
                refs = ref_re.findall(content)
                unresolved = [r for r in set(refs) if not (fig_dir / f"{r}.png").exists()]
                if not unresolved:
                    continue
                src_anchors = [anchors_by_id.get(a.get("anchor_id"), {})
                               for a in (b.get("sources") or [])]
                primary_page = next((a.get("page") for a in src_anchors if a.get("page")), None)
                scored: list[tuple[float, int, str]] = []
                for pg in {a.get("page") for a in src_anchors}:
                    if not pg:
                        continue
                    same_page = [a for a in src_anchors if a.get("page") == pg and a.get("bbox")]
                    for fid, fbbox in fig_by_page.get(int(pg), []):
                        gaps = [_vgap(a["bbox"], fbbox) for a in same_page]
                        if gaps:
                            # 只在同页锚点间比距离（跨页坐标不可比）；首选来源页破平
                            scored.append((min(gaps), 0 if pg == primary_page else 1, fid))
                # 歧义（同破平级且与次近差距 <60pt）不猜——判定收进 figures.pick_ref_target
                real = pick_ref_target(scored)
                if real is None:
                    continue
                for r in unresolved:
                    content = content.replace(f"![{r}]", f"![{real}]")
                if content != b["content"]:
                    b["content"] = content
                    b["revision"] = int(b["revision"]) + 1
                    b["updated_at"] = now
                    history.append(store.make_history_row(b, "ai", "图片引用修复", now))
                    changed = True
                    n_patched += 1
            if not changed:
                break
            try:
                store.commit(DocMutation(blocks={ch: rows}, history=history),
                             base_revision=base_rev, sync_backlinks=True)
                patched += n_patched
                break
            except RevisionConflict:
                if attempt == 1:
                    raise
    return {"figures": len(figs), "patched_blocks": patched}


@router.get("/api/v1/docs/{doc_id}")
def get_doc(request: Request, doc_id: str):
    st = state(request)
    return DocStore(st.layout, doc_id).manifest()


@router.post("/api/v1/docs/{doc_id}/rename")
def rename_doc(request: Request, doc_id: str, body: dict):
    """教材改名（只改 manifest.title，解析产物与笔记不动；走提交机制保单写者）。"""
    from src.core.store import DocMutation

    st = state(request)
    title = str((body or {}).get("title") or "").strip()
    if not title:
        raise InputError("名称不能为空")
    if len(title) > 200:
        raise InputError("名称不能超过 200 字")
    if not st.layout.manifest(doc_id).exists():
        raise NotFound(f"文档不存在: {doc_id}")
    store = DocStore(st.layout, doc_id)
    base = store.data_revision()
    store.commit(DocMutation(manifest_updates={"title": title}),
                 base_revision=base, sync_backlinks=False)
    return {"doc_id": doc_id, "title": title}


AI_EDIT_PROMPT = """你是笔记编辑助手。请按用户指令修改下面这块笔记。
要求：只输出修改后的笔记正文（Markdown 文本，公式用 LaTeX）；不要输出 JSON、不要输出代码围栏；
保持引文标记 [n]、图片占位符 ![fig_xxxxxxxx]、公式 LaTeX 不受破坏；不得编造笔记之外的事实；
没有要求修改的部分保持原样。
块标题：{title}
块类型：{type_id}
用户指令：{instruction}

<original>
{content}
</original>"""


def _unwrap_proposal(text: str) -> str:
    """提案解壳：模型偶尔把正文包成 JSON（如 {"md_latex": "..."}——实测于真实模型，
    指令里的格式名被当成字段名）或代码围栏；不剥掉用户就会把 JSON 壳应用进正文。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    if t.startswith("{") and t.endswith("}"):
        try:
            obj = json.loads(t)
            if isinstance(obj, dict):
                vals = [str(v).strip() for v in obj.values() if isinstance(v, str) and str(v).strip()]
                if len(vals) == 1:
                    return vals[0]
        except Exception:
            pass
    return t


@router.post("/api/v1/docs/{doc_id}/blocks/{block_id}/ai-edit")
def block_ai_edit(request: Request, doc_id: str, block_id: str, body: dict):
    """单块 AI 编辑：按指令产出修改建议（**不落盘**）；应用是显式人工动作（5.2）。"""
    from src.core.store import DocStore as _DS

    st = state(request)
    store = _DS(st.layout, doc_id)
    # include_deleted=False：blocks_by_id 默认含软删块，曾对已删块出 AI 建议
    block = store.blocks_by_id(include_deleted=False).get(block_id)
    if not block:
        raise NotFound(f"块不存在: {block_id}")
    instruction = str((body or {}).get("instruction") or "").strip()
    if not instruction:
        raise InputError("缺少修改指令")
    prompt = AI_EDIT_PROMPT.format(title=block.get("title", ""), type_id=block.get("type", ""),
                                   instruction=instruction, content=block.get("content", ""))
    # 不用 JSON schema：改写要的是纯文本，强制 json_schema 又禁止 JSON 会约束打架——
    # 实测模型回一串空格摆烂；纯文本 + 解壳兜底才是稳的
    models = (st.config.get("generate", {}).get("models", {}) or {})
    model = models.get("fill") or models.get("default")
    resp = operation_client(st.client).complete("edit", [{"role": "user", "content": prompt}], schema=None,
                              max_tokens=6000, temperature=0.3, model=model)
    out = resp.get("output")
    # output 形状不稳（实测真实模型）：纯文本 / {content: ...} / {md_latex: ...} 套壳都出现过
    if isinstance(out, str):
        raw = out
    else:
        raw = str((out or {}).get("content") or "") or str((out or {}).get("text") or "")
        if not raw.strip():
            vals = [str(v).strip() for v in (out or {}).values()
                    if isinstance(v, str) and str(v).strip()]
            raw = max(vals, key=len) if vals else ""
    proposal = _unwrap_proposal(raw)
    if not proposal:
        raise InputError("AI 没有产出修改建议，请换一种指令")
    return {"block_id": block_id, "proposal": proposal, "original": block.get("content", "")}


@router.post("/api/v1/docs/{doc_id}/blocks/{block_id}/apply-edit")
def block_apply_edit(request: Request, doc_id: str, block_id: str, body: dict):
    """应用修改：用户点确认 = 人工动作（author=human → manually_edited + 保护位，5.2），
    走 update_block 历史与 CAS（5.6/7.3），原内容进历史可回溯。"""
    from src.core.store import DocStore as _DS

    st = state(request)
    store = _DS(st.layout, doc_id)
    if not store.blocks_by_id(include_deleted=False).get(block_id):
        raise NotFound(f"块不存在: {block_id}")
    content = str((body or {}).get("content") or "")
    if not content.strip():
        raise InputError("修改内容为空")
    patch = {"content": content}
    if str((body or {}).get("title") or "").strip():
        patch["title"] = str(body["title"]).strip()
    block = store.update_block(block_id, patch,
                               expected_revision=(body or {}).get("base_revision"),
                               author="human", message="AI 编辑（人工确认应用）")
    return {"block": block}


@router.post("/api/v1/docs/{doc_id}/blocks/{block_id}/links")
def block_add_link(request: Request, doc_id: str, block_id: str, body: dict):
    """关联知识点：把目标块加入本块 links（跨章可链，回指检查）。"""
    from src.core.store import DocStore as _DS

    st = state(request)
    store = _DS(st.layout, doc_id)
    block = store.blocks_by_id(include_deleted=False).get(block_id)
    if not block:
        raise NotFound(f"块不存在: {block_id}")
    target = str((body or {}).get("target_id") or "").strip()
    if not target or target == block_id:
        raise InputError("目标块非法（不能关联自己）")
    if not store.blocks_by_id(include_deleted=False).get(target):
        raise NotFound(f"目标块不存在: {target}")
    links = list(dict.fromkeys(list(block.get("links") or []) + [target]))
    updated = store.update_block(block_id, {"links": links},
                                 expected_revision=(body or {}).get("base_revision"),
                                 author="human", message="关联知识点")
    return {"block": updated}


@router.delete("/api/v1/docs/{doc_id}/blocks/{block_id}/links/{target_id}")
def block_remove_link(request: Request, doc_id: str, block_id: str, target_id: str,
                      base_revision: int | None = None):
    """取消关联。"""
    from src.core.store import DocStore as _DS

    st = state(request)
    store = _DS(st.layout, doc_id)
    block = store.blocks_by_id(include_deleted=False).get(block_id)
    if not block:
        raise NotFound(f"块不存在: {block_id}")
    links = [ln for ln in (block.get("links") or []) if ln != target_id]
    updated = store.update_block(block_id, {"links": links},
                                 expected_revision=base_revision,
                                 author="human", message="取消关联")
    return {"block": updated}


@router.get("/api/v1/docs/{doc_id}/qbank")
def qbank_list(request: Request, doc_id: str):
    """参考题库（用户上传的课后习题/往年题）：条目列表与数量。"""
    st = state(request)
    if not st.layout.manifest(doc_id).exists():
        raise NotFound(f"文档不存在: {doc_id}")
    from src.generate.quiz import load_qbank

    entries = load_qbank(st.layout.doc_dir(doc_id))
    return {"count": len(entries), "entries": entries[:200]}


@router.post("/api/v1/docs/{doc_id}/qbank")
async def qbank_upload(request: Request, doc_id: str, file: UploadFile = File(...)):
    """上传参考题目（PDF/txt/md）：按段落入题库，供出题时定向风格/考点。"""
    st = state(request)
    if not st.layout.manifest(doc_id).exists():
        raise NotFound(f"文档不存在: {doc_id}")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".pdf", ".txt", ".md"):
        raise InputError("题库只支持 PDF / txt / md")
    data = await file.read()
    if not data:
        raise InputError("文件为空")
    import tempfile

    from src.generate.quiz import append_qbank, extract_qbank_entries, read_qbank_file

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(data)
        tmp = Path(tf.name)
    try:
        text = read_qbank_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    entries = extract_qbank_entries(file.filename or "upload", text)
    if not entries:
        raise InputError("没有从文件中识别出可用的题目段落")
    # 题库写在事务外（不 bump data_revision），必须与追加讲义的 swap 串行：
    # 否则追加 staging 期间上传的题库会被整目录 swap 静默覆盖回快照（数据丢失）。
    # 注意 qbank_upload 是 async 路由：FileLock 是阻塞调用，必须丢线程池，否则
    # 等锁期间会冻结整个事件循环（所有请求一起卡住）。
    from fastapi.concurrency import run_in_threadpool
    from src.core.transactions import doc_lock

    def _locked_append() -> int:
        with doc_lock(st.layout, doc_id):
            # 等锁期间文档可能被删/移入回收站：不要凭空重建 doc 目录
            if not st.layout.manifest(doc_id).exists():
                raise NotFound(f"文档不存在: {doc_id}")
            return append_qbank(st.layout.doc_dir(doc_id), entries)

    added = await run_in_threadpool(_locked_append)
    return {"added": added, "count": len(entries), "file": file.filename}


@router.post("/api/v1/docs/{doc_id}/quiz")
def quiz_generate(request: Request, doc_id: str, body: dict):
    """AI 出题：基于笔记内容出练习题（题库样本定向风格）；同步返回题目与答案。"""
    from src.core.store import DocStore as _DS
    from src.generate.quiz import generate_quiz, load_qbank

    st = state(request)
    if not st.layout.manifest(doc_id).exists():
        raise NotFound(f"文档不存在: {doc_id}")
    store = _DS(st.layout, doc_id)
    chapter_id = (body or {}).get("chapter_id")
    chapters = [chapter_id] if chapter_id else [c["chapter_id"] for c in store.manifest().get("chapters", [])]
    kinds = (body or {}).get("kinds") or ["choice", "short"]
    if not isinstance(kinds, list):
        kinds = ["choice", "short"]
    try:
        count = int((body or {}).get("count") or 10)
    except (TypeError, ValueError):
        raise InputError("count 必须是整数")
    difficulty = str((body or {}).get("difficulty") or "期末水平")
    models = (st.config.get("generate", {}).get("models", {}) or {})
    model = models.get("quiz") or models.get("fill")
    try:
        # 只生成不落盘：与追加讲义 swap 共用 doc 锁写盘（生成过程不持锁，
        # LLM 调用可达分钟级；只有最终写文件需要串行化）
        quiz = generate_quiz(operation_client(st.client), store=store, chapter_ids=chapters, kinds=kinds,
                             count=count, difficulty=difficulty, model=model,
                             config=st.config, qbank=load_qbank(st.layout.doc_dir(doc_id)),
                             persist=False)
    except ValueError as exc:
        # 产品规则类错误（无笔记/结果为空）归 4xx，不给用户 500
        raise InputError(str(exc))
    from src.core.transactions import doc_lock
    from src.generate.quiz import save_quiz

    with doc_lock(st.layout, doc_id):
        # 生成期间文档可能被删/被追加 swap：等锁后复核，避免把套题写进已删除文档
        if st.layout.manifest(doc_id).exists():
            save_quiz(store, quiz)
    return quiz


@router.post("/api/v1/docs/{doc_id}/append")
async def append_pdf(request: Request, doc_id: str, file: UploadFile = File(...)):
    """追加讲义：把新 PDF 接到现有教材尾部（老师逐份发放的讲义场景）。

    事务化（Review #1）：合并/解析/分章/重锚定全部在 data/.append-staging-* 的独立
    快照里完成，真实 PDF 与主数据在成功前只读；全部成功后才在 doc_lock 内校验
    data_revision、备份旧产物并统一 swap。旧章 chapter_id/title/页区间原样保留
    （追加只在尾部加页），只有新页分配新章——人工笔记、生成缓存、报告全部原样保留。
    任何一步失败只删除暂存目录，真实数据零改动。"""
    st = state(request)
    if not (file.filename or "").lower().endswith(".pdf"):
        raise InputError("追加只支持 PDF（PPTX 讲义请合并后重新导入）")
    data = await file.read()
    if not data:
        raise InputError("文件为空")
    if len(data) > 200 * 1024 * 1024:
        raise InputError("PDF 超过 200 MiB 限制")
    if not st.layout.manifest(doc_id).exists():
        raise NotFound(f"文档不存在: {doc_id}")
    existing = st.layout.textbooks_dir / f"{doc_id}.pdf"
    if not existing.exists():
        raise InputError("该教材没有可追加的 PDF（PPTX 教材暂不支持追加）")
    man = DocStore(st.layout, doc_id).manifest()

    def run(req, progress, is_cancelled, register_artifact, task_id=None):
        from src.ingest.append_stage import run_append

        return run_append(st.layout, doc_id, data, st.config, progress=progress,
                          is_cancelled=is_cancelled, vision_ocr=_vision_ocr_for(st))

    task = st.tasks.submit("import", run, {"doc_id": doc_id, "mode": "append",
                                           "filename": file.filename},
                           doc_id=doc_id, name=(man.get("title") or doc_id) + " · 追加讲义")
    return JSONResponse({"task_id": task["task_id"], "doc_id": doc_id}, status_code=202)


@router.delete("/api/v1/docs/{doc_id}")
def delete_doc(request: Request, doc_id: str, body: dict | None = None):
    """删除教材（移入回收站，可恢复）。需 confirmed=true 显式确认（4.1 文档事实源）。"""
    body = body or {}
    if not body.get("confirmed"):
        raise InputError("删除教材需要 confirmed=true（将移入回收站，可在 data/.trash 找回）")
    st = state(request)
    store = DocStore(st.layout, doc_id)
    summary = {
        "blocks": len(store.all_block_rows(include_deleted=True)),
        "anchors": len(store.all_anchor_rows()),
        "chapters": len(store.chapter_ids()),
    }
    result = store.trash_document()
    result["removed"] = summary
    return result


@router.get("/api/v1/docs/{doc_id}/generate-reports")
def generate_reports(request: Request, doc_id: str):
    """生成记录（9.2 报告透传）：各章生成的时间/模型/结果，供 UI「生成记录」展示。"""
    st = state(request)
    DocStore(st.layout, doc_id).manifest()          # 不存在的文档 → 404
    out = []
    d = st.layout.reports_doc_dir(doc_id)
    if d.exists():
        for p in sorted(d.glob("*.generate.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue                            # 单个报告损坏不影响列表
    out.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
    return {"reports": out}


@router.get("/api/v1/docs/{doc_id}/share")
def share_doc(request: Request, doc_id: str, chapter_id: str | None = None):
    """分享用载荷：与文件导出同一套 Markdown 渲染 + 结构化块（前端据此生成离线 HTML）。

    chapter_id 省略 = 整本。图片以 data URL 内嵌，分享文件离线可看。
    """
    import base64
    import re as _re

    st = state(request)
    store = DocStore(st.layout, doc_id)
    manifest = store.manifest()
    all_ch = [c["chapter_id"] for c in manifest.get("chapters", [])]
    if chapter_id and chapter_id not in all_ch:
        raise NotFound(f"章节不存在: {chapter_id}")
    from src.export.md_export import render_md_book_text, render_md_chapter_text

    if chapter_id:
        filename, markdown = render_md_chapter_text(store, chapter_id)
        chapters = [chapter_id]
    else:
        filename, markdown = render_md_book_text(store)
        chapters = all_ch
    anchors_by_id = {a["anchor_id"]: a for a in store.all_anchor_rows()}
    blocks_out: list[dict] = []
    fig_ids: set[str] = set()
    for ch in chapters:
        for b in store.block_rows(ch):
            if b.get("deleted_at"):
                continue
            srcs = []
            for s in b.get("sources") or []:
                a = anchors_by_id.get(s.get("anchor_id"))
                if a is None:
                    continue
                srcs.append({"page": a.get("page"),
                             "quote": (s.get("quote") or "")[:200]})
                if s.get("figure_id"):
                    fig_ids.add(str(s["figure_id"]))
            content = b.get("content") or ""
            for m in _re.finditer(r"!\[(fig_[A-Za-z0-9_]+)\]", content):
                fig_ids.add(m.group(1))
            blocks_out.append({"id": b["id"], "type": b.get("type"),
                               "title": b.get("title") or "",
                               "content": content, "status": b.get("status"),
                               "sources": srcs})
    figures: dict[str, str] = {}
    for fid in sorted(fig_ids):
        p = st.layout.figure_png(doc_id, fid)
        if p.exists():
            figures[fid] = ("data:image/png;base64,"
                            + base64.b64encode(p.read_bytes()).decode("ascii"))
    from src.core.note_templates import book_template

    presentation = book_template(st.layout, doc_id)
    return {"doc_id": doc_id, "title": manifest.get("title") or doc_id,
            "presentation": {k: presentation.get(k) for k in ("name", "note_type", "cover")} if presentation else None,
            "filename": filename, "markdown": markdown,
            "blocks": blocks_out, "figures": figures,
            "generated_at": manifest.get("updated_at")}


@router.get("/api/v1/docs/{doc_id}/pdf")
def get_pdf(request: Request, doc_id: str):
    """PDF 静态服务：支持 HTTP Range（206/416）与完整 GET（200）。"""
    st = state(request)
    store = DocStore(st.layout, doc_id)
    manifest = store.manifest()
    path = (st.layout.root / manifest["pdf_path"]).resolve()
    if not path.exists():
        raise NotFound("PDF 文件不存在")
    return FileResponse(path, media_type="application/pdf", filename=f"{doc_id}.pdf")


@router.get("/api/v1/docs/{doc_id}/figures/{figure_id}")
def get_figure(request: Request, doc_id: str, figure_id: str):
    st = state(request)
    if not FIGURE_ID_RE.match(figure_id):
        raise InputError(f"figure_id 非法: {figure_id}")
    path = st.layout.safe_join(st.layout.doc_dir(doc_id), "figures", f"{figure_id}.png")
    if not path.exists():
        raise NotFound("图资源不存在")
    return FileResponse(path, media_type="image/png")


@router.get("/api/v1/docs/{doc_id}/pages/{page}/blocks")
def page_blocks(request: Request, doc_id: str, page: int):
    """教材→笔记：该页关联块摘要（响应 < 200ms 由索引保证）。"""
    st = state(request)
    rows = queries.blocks_for_page(st.layout, doc_id, page)
    store = DocStore(st.layout, doc_id)
    out = []
    for r in rows:
        try:
            # 索引可能残留已删块：教材侧栏不得把删除的笔记当活块展示
            block = store.get_block(r["block_id"], include_deleted=False)
        except Exception:
            continue
        out.append({"block_id": block["id"], "type": block["type"], "title": block["title"],
                    "summary": (block.get("content") or "")[:120], "status": block["status"]})
    return out


@router.post("/api/v1/docs/{doc_id}/match")
def match_selection(request: Request, doc_id: str, body: dict):
    """选区匹配（6.3.2）；rects 为 PDF 用户空间坐标，v1 仅支持单页选区。"""
    st = state(request)
    try:
        page = int(body.get("page", 0) or 0)
    except (TypeError, ValueError):
        raise InputError("page 必须是整数")
    rects = body.get("rects") or []
    text = body.get("text") or ""
    if page < 1 or not rects:
        raise InputError("请求体必须含合法 page 与 rects")
    # 选区矩形严格校验（6.3.2）：每项 [x0,y0,x1,y1] 有限数值，非法即 400 而非 500
    clean_rects = []
    for r in rects:
        if not isinstance(r, (list, tuple)) or len(r) != 4:
            raise InputError("rects 元素必须是 [x0,y0,x1,y1]")
        try:
            vals = [float(v) for v in r]
        except (TypeError, ValueError):
            raise InputError("rects 必须是数值")
        if not all(math.isfinite(v) for v in vals):
            raise InputError("rects 含非有限数值")
        clean_rects.append(vals)
    rects = clean_rects
    hits = queries.match_selection(st.layout, doc_id, page, rects, text, st.config)
    out = []
    for h in hits:
        store = DocStore(st.layout, doc_id)
        blocks = []
        for bid in h.get("note_block_ids", []) if isinstance(h.get("note_block_ids"), list) else []:
            try:
                b = store.get_block(bid, include_deleted=False)   # 已删块不进选区匹配
                blocks.append({"block_id": b["id"], "title": b["title"], "type": b["type"],
                               "summary": (b.get("content") or "")[:120], "status": b["status"]})
            except Exception:
                continue
        out.append({"anchor_id": h["anchor_id"], "page": h["page"],
                    "bbox": [h["bbox_x0"], h["bbox_y0"], h["bbox_x1"], h["bbox_y1"]],
                    "content": h.get("content", "")[:200], "scores": h.get("scores"),
                    "match": h.get("match"), "blocks": blocks})
    return {"hits": out}


@router.get("/api/v1/search")
def search(request: Request, doc_id: str, q: str, mode: str = "fts"):
    """检索（6.3）：mode=vec 走 sqlite-vec 语义兜底，未命中自动回落 FTS。"""
    st = state(request)
    store = DocStore(st.layout, doc_id)
    q = q.strip()
    if not q:
        store.manifest()
        return {"anchors": [], "blocks": [], "mode": mode}
    top_k = int(st.config.get("match", {}).get("top_k", 8))
    anchors: list[dict] = []
    if mode == "vec":
        from src.anchor.embed import embedder_from_config

        embedder = embedder_from_config(st.config)
        vec = (embedder.encode([q]) or [None])[0]
        if vec:
            anchors = queries.search_semantic(
                st.layout, doc_id, vec, k=top_k,
                threshold=float(st.config.get("embedding", {}).get("threshold", 0.55)))
    if not anchors:
        anchors = queries.search_anchors(st.layout, doc_id, q, limit=top_k)
    blocks = store.search_blocks(q)
    return {
        "anchors": [{"anchor_id": a["anchor_id"], "page": a["page"], "content": a["content"],
                     "note_block_ids": a.get("note_block_ids", []), "match": a.get("match", "fts")}
                    for a in anchors],
        "blocks": blocks,
        "mode": mode,
    }


def _vision_ocr_for(st):
    """视觉补 OCR 识别器（弱识别页重识别，每页少量 token）。

    需要 vision 能力模型（generate.models.ocr 显式指定，或 fill/default 里有视觉能力的）；
    parser.ocr_vision=off 关闭。构造失败不阻塞导入（本地 OCR 照常）。"""
    try:
        if (st.config.get("parser", {}) or {}).get("ocr_vision", "auto") == "off":
            return None
        models = (st.config.get("generate", {}).get("models", {}) or {})
        cand = models.get("ocr")
        if not cand:
            for slot in ("fill", "default"):
                name = models.get(slot)
                if name and (st.registry.capabilities(name) or {}).get("vision"):
                    cand = name
                    break
        if not cand or not (st.registry.capabilities(cand) or {}).get("vision"):
            return None
        from src.ingest.vision_ocr import build_vision_ocr

        return build_vision_ocr(operation_client(st.client), cand)
    except Exception:
        return None


def _build_generate_run(st, doc_id: str, chapter_id, model, resume: bool = False,
                        skip_done: bool = False):
    """生成任务执行体（触发与续跑共用）。

    resume=True：吃掉该章已有缓存的完成块、只补缺口（换模型/升级提示词
    不让已完成的 115/117 重烧），并复用原任务记录（同 task_id）。
    skip_done=True（全书生成默认）：已有 complete 报告且锚点指纹未变的章节跳过，
    不重烧 LLM；换提示词/模型想全量重生成时置 False。
    """
    def run(req, progress, is_cancelled, register_artifact, task_id=None):
        import json as _json

        from src.generate.engine import generate_chapter

        client = operation_client(st.client)
        store = DocStore(st.layout, doc_id)
        chapters = [chapter_id] if chapter_id else store.chapter_ids()
        # 并行分章（提速）：整本生成时多章同时跑 outline/fill，写入由引擎内
        # per-doc 锁串行（4.2.2 单写者），不会互撞 RevisionConflict
        workers = 1
        if len(chapters) > 1:
            workers = int(st.config.get("generate", {}).get("chapter_concurrency", 2) or 2)
            workers = max(1, min(workers, len(chapters)))

        # 跨章进度聚合：并行时各章同时上报 outline/fill，直接透传进度条会来回跳，
        # 按章累计后合并成全局 done/total，保证单调推进
        agg: dict[str, dict] = {}
        agg_lock = threading.Lock()

        def make_progress(ch: str):
            def _p(evt: dict) -> None:
                evt = dict(evt or {})
                stage = evt.get("stage")
                with agg_lock:
                    if stage in ("outline", "fill"):
                        agg.setdefault(ch, {})[stage] = evt
                        rows = [x.get(stage) for x in agg.values() if x.get(stage)]
                        if stage == "outline":
                            progress({"stage": "outline",
                                      "batch": sum(r.get("batch") or 0 for r in rows),
                                      "batches": sum(r.get("batches") or 0 for r in rows),
                                      "chapter_id": ch})
                            return
                        progress({"stage": "fill",
                                  "done": sum(r.get("done") or 0 for r in rows),
                                  "total": sum(r.get("total") or 0 for r in rows),
                                  "chapter_id": ch})
                        return
                    evt.setdefault("chapter_id", ch)
                    progress(evt)
            return _p

        def failed_report(ch: str, reason: str) -> dict:
            return {"doc_id": doc_id, "chapter_id": ch, "status": "failed",
                    "failures": [{"stage": "generate", "reason": reason}]}

        # 全局性故障（余额耗尽/密钥失效）一经出现就中止余下章节：
        # 曾逐章重试到全部失败，用户只看到「部分完成」而不知是没余额
        stop: dict = {"reason": None}

        def outcome(ch: str) -> dict:
            """执行一章；可隔离的失败转成失败报告，不可隔离的继续上抛。"""
            if is_cancelled():
                raise TaskCancelled("任务已取消")
            if stop["reason"]:
                return failed_report(ch, "已中止（" + stop["reason"][:120] + "），本章未尝试")
            if skip_done:
                # 已生成完才跳：complete 报告 + 锚点指纹未变（报告即事实源）；
                # 重锚定/换提示词想重生成 → skip_done=False 全量重跑
                prev = None
                rep_path = st.layout.reports_doc_dir(doc_id) / f"{ch}.generate.json"
                try:
                    if rep_path.exists():
                        prev = _json.loads(rep_path.read_text(encoding="utf-8"))
                except Exception:
                    prev = None
                ch_meta = next((c for c in store.manifest().get("chapters", [])
                                if c.get("chapter_id") == ch), {})
                # outline_complete 缺省视为 True（旧报告无此字段）；修复前写下的
                # 「complete + 提纲不全」报告不得跳过——否则缺失的提纲批次永远补不回来
                if (prev and prev.get("status") == "complete"
                        and prev.get("outline_complete", True)
                        and prev.get("outline_hash") == (ch_meta.get("outline_hash") or "")):
                    progress({"stage": "generate", "chapter_id": ch, "skipped": True})
                    return {"doc_id": doc_id, "chapter_id": ch, "status": "skipped",
                            "reason": "已有完整生成记录且锚点未变化，跳过"}
            progress({"stage": "generate", "chapter_id": ch})
            try:
                return generate_chapter(doc_id, ch, layout=st.layout, client=client,
                                        config=st.config, registry=st.registry, model=model,
                                        merge_mode="merge", resume=resume, task_id=task_id,
                                        progress=make_progress(ch), is_cancelled=is_cancelled)
            except TaskCancelled:
                raise
            except PriceUnknown as exc:
                if exc.code != "BUDGET_EXCEEDED":
                    raise        # 配置类（未配置模型/价格未知）→ 整个任务清晰失败
                return failed_report(ch, str(exc))
            except NoteGenError as exc:
                # 4.7 PipelineError 语义：阶段性失败记录后继续其他章节——
                # 一章缺锚点/提交冲突/校验失败不得拖垮整本书的生成任务
                # （原实现只隔离 PipelineError/ConflictError，一章 quote 校验
                # 失败会让后面所有章节连跑的机会都没有）
                if getattr(exc, "code", "") in ("LLM_QUOTA_EXHAUSTED", "LLM_AUTH_FAILED"):
                    # 余额/密钥是全局故障：不中止的话余下每章都白跑一遍再失败
                    stop["reason"] = str(exc)
                return failed_report(ch, str(exc))

        reports: list[dict] = []
        if workers <= 1:
            for ch in chapters:
                reports.append(outcome(ch))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            by_ch: dict[str, dict] = {}
            pool = ThreadPoolExecutor(max_workers=workers)
            try:
                futs = {pool.submit(outcome, ch): ch for ch in chapters}
                for fut in as_completed(futs):
                    by_ch[futs[fut]] = fut.result()
            finally:
                # 取消/意外异常：不再等未完成章节（它们会在下一个检查点自行退出）
                pool.shutdown(wait=False, cancel_futures=True)
            reports = [by_ch[ch] for ch in chapters if ch in by_ch]
        if stop["reason"]:
            done = sum(1 for r in reports if r.get("status") in ("complete", "skipped"))
            raise PipelineError(
                f"生成中止：{stop['reason'][:300]}——已完成 {done} 章；"
                f"充值或更换模型（设置 → 供应商/任务路由）后点「继续生成」，"
                f"会自动跳过已完成章节")
        if reports and all(r.get("status") == "failed" for r in reports):
            # 全军覆没要以 failed 结局收场（10.4 可解释），不能谎报 succeeded
            reasons = "；".join(
                f"{r['chapter_id']}: " + ("; ".join(str(f.get("reason", "")) for f in r.get("failures", []))
                                          or r.get("status"))
                for r in reports)
            raise PipelineError(f"全部章节生成失败：{reasons}"[:600])
        # 章内块级失败（report status=partial）必须反映到任务结局：
        # 原实现无条件返回 succeeded，22/61 块失败时任务盘照样亮绿灯
        return {"reports": reports,
                # skipped = 已生成完被跳过，不算 partial（否则全书生成永远亮黄灯）
                "partial": any(r.get("status") not in ("complete", "skipped") for r in reports)}

    return run


@router.post("/api/v1/docs/{doc_id}/generate")
def trigger_generate(request: Request, doc_id: str, body: dict | None = None):
    """触发章节再生成（异步任务）；固定走 5.7 合并策略。"""
    st = state(request)
    body = body or {}
    chapter_id = body.get("chapter_id")
    model = body.get("model")
    # 全书生成默认跳过已生成完的章节（complete 报告 + 锚点未变）；单章点击是显式意图不跳。
    # 想全量重生成（如换提示词/模型）：body.skip_done=false
    skip_done = bool(body.get("skip_done")) if "skip_done" in body else (chapter_id is None)

    # 防重复触发：同文档已有排队/运行中的生成任务时直接复用（避免重复烧钱）。
    # 收紧为文档级而非章节级：两个生成任务并跑 = 两个写者抢 data_revision（I7 单写者），
    # 后提交者必 RevisionConflict，白烧一章的 LLM 费用（probe3 实测两任务并跑）。
    # 注：单任务内的多章并行是安全的——写入段由引擎内 per-doc 锁串行（4.2.2）。
    for t in st.runtime.list_tasks(doc_id=doc_id):
        if t.get("kind") == "generate" and t.get("status") in ("queued", "running"):
            return JSONResponse({"task_id": t["task_id"]}, status_code=202)

    # 任务显示名：手动命名优先（body.name），否则书名 · 章节名
    custom_name = str((body or {}).get("name") or "").strip()
    try:
        _man = DocStore(st.layout, doc_id).manifest()
        _title = _man.get("title") or doc_id
        _ch = next((c for c in _man.get("chapters", []) if c.get("chapter_id") == chapter_id), None)
        task_name = _title + " · " + ((_ch or {}).get("title") or chapter_id or "整本")
    except Exception:
        task_name = doc_id + (" · " + chapter_id if chapter_id else "")
    task_name = custom_name or task_name
    run = _build_generate_run(st, doc_id, chapter_id, model, skip_done=skip_done)
    task = st.tasks.submit("generate", run, {"doc_id": doc_id, "chapter_id": chapter_id},
                           doc_id=doc_id, idempotency_key=None, name=task_name)
    return JSONResponse({"task_id": task["task_id"]}, status_code=202)


@router.post("/api/v1/tasks/{task_id}/resume")
def resume_generate_task(request: Request, task_id: str):
    """续跑生成任务：复用原任务记录（同 task_id/名字），引擎只补缺口。"""
    st = state(request)
    try:
        task = st.tasks.get(task_id)
    except KeyError:
        raise NotFound(f"任务不存在: {task_id}")
    if task.get("kind") != "generate":
        raise InputError("仅生成任务支持续跑", code="NOT_RESUMABLE")
    if task.get("status") in ("queued", "running"):
        return JSONResponse({"task_id": task_id, "status": task.get("status")}, status_code=202)
    req = task.get("request") or {}
    doc_id = req.get("doc_id")
    chapter_id = req.get("chapter_id")
    if not doc_id:
        raise InputError("任务缺少文档参数，无法续跑", code="NOT_RESUMABLE")
    run = _build_generate_run(st, doc_id, chapter_id, None, resume=True)
    try:
        t = st.tasks.resume(task_id, run, {"doc_id": doc_id, "chapter_id": chapter_id})
    except KeyError:      # get 与 resume 之间被并发删除
        raise NotFound(f"任务不存在: {task_id}")
    return JSONResponse({"task_id": t["task_id"], "resumed": True}, status_code=202)
