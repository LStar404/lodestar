"""生成编排（3.3 契约 generate_chapter）：outline → fill → 校验 → 5.7 合并 → 提交 → link。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from src.core.block_types import active_types
from src.core.config import REASONING_EFFORTS, THINKING_MODES
from src.core.errors import PipelineError, RevisionConflict, TaskCancelled
from src.core.llm import BudgetGuard, LLMClient
from src.core.model_registry import ModelRegistry
from src.core.paths import Layout
from src.core.store import DocStore
from src.core.text import utc_now_iso

from .cache import cache_key, model_fingerprint, read_cache, read_cache_any, write_cache
from .fill import fill_all
from .merge import merge_candidates
from .outline import generate_outline, plan_batches
from .prompts import PROMPT_VERSION


# 进程内按 (workspace, doc) 串行「读基线→merge→commit→link」（4.2.2 单写者）。
# 整本生成时各章 outline/fill 并行跑以提速，但写入必须排队：两章同时提交会让
# 后到者拿旧 data_revision 撞 RevisionConflict，白烧一章的 LLM 调用。
# 用户并发改动仍由 CAS 兜底：基线在锁内读取，锁外的用户提交要么先落（被 merge
# 读到）、要么后落（撞 CAS 由用户重试），不会互相吞掉。
_COMMIT_LOCKS: dict[str, threading.Lock] = {}
_COMMIT_LOCKS_GUARD = threading.Lock()


def _commit_lock(layout: Layout, doc_id: str) -> threading.Lock:
    key = f"{layout.root}|{doc_id}"
    with _COMMIT_LOCKS_GUARD:
        lock = _COMMIT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _COMMIT_LOCKS[key] = lock
        return lock


def _derive_heading_and_order(items: list[dict], anchors: list[dict], chapter_id: str) -> list[dict]:
    """生成后置处理（5.3.1）：heading_anchor_id 与 order 的确定性规则，不交给模型。"""
    ordered = sorted(anchors, key=lambda a: (a["page"], a["reading_order"]))
    index = {a["anchor_id"]: i for i, a in enumerate(ordered)}
    headings = [(i, a) for i, a in enumerate(ordered) if a.get("content_type") == "heading"]
    out = []
    for item in items:
        sources = item.get("sources", [])
        local = [s for s in sources if s["anchor_id"] in index]
        heading_id = None
        base_index = None
        if local:
            first = min(local, key=lambda s: index[s["anchor_id"]])
            base_index = index[first["anchor_id"]]
            anchor = ordered[base_index]
            if anchor.get("content_type") == "heading":
                heading_id = anchor["anchor_id"]
            else:
                prior = [h for h in headings if h[0] < base_index]
                heading_id = prior[-1][1]["anchor_id"] if prior else None
        elif item.get("depends_on"):
            deps = [d for d in item["depends_on"]]
            for dep in deps:
                src_item = next((x for x in items if x.get("temp_id") == dep), None)
                if src_item and src_item.get("_heading_anchor_id"):
                    heading_id = src_item["_heading_anchor_id"]
                    break
        item = dict(item)
        item["_heading_anchor_id"] = heading_id
        item["heading_anchor_id"] = heading_id
        if base_index is not None:
            order = (base_index + 1) * 10.0
        else:
            order = float(len(ordered) + 1) * 10.0
        if item.get("type") == "motivation":
            order = max(1.0, order - 5.0)
        if item.get("type") == "link":
            order = float(len(ordered) + 1) * 10.0
        item["order"] = float(order)
        out.append(item)
    return out


def _normalize_sources(sources: list | None) -> list:
    """4.6.1：一块内 anchor_id 唯一、≤5 条——模型输出归一，避免 commit 阶段才失败。"""
    out: list = []
    seen: set = set()
    for src in sources or []:
        aid = (src or {}).get("anchor_id")
        if aid is None or aid in seen:
            continue
        seen.add(aid)
        out.append(src)
    return out[:5]


def _build_candidates(items: list[dict], filled: dict[str, dict]) -> list[dict]:
    """提纲条目 + 填充结果 → 候选块（正常路径与断点快照共用同一构建）。"""
    out: list[dict] = []
    for item in items:
        f = filled.get(item["temp_id"], {})
        cand = dict(item)
        cand["sources"] = _normalize_sources(f.get("sources_patch", item.get("sources", [])))
        cand["basis"] = f.get("basis", item.get("basis", "anchored"))
        cand["content"] = f.get("content", "")
        cand["content_format"] = f.get("content_format", "md_latex")
        cand["tags"] = f.get("tags", [])
        cand["_status"] = f.get("_status", "ok") if f else "failed"
        cand["_errors"] = f.get("_errors", [])
        cand["status"] = "needs_review" if cand["_status"] == "needs_review" else "ai_generated"
        out.append(cand)
    return out


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _norm_choice(value: Any, allowed: tuple[str, ...]) -> str:
    """解码类配置归一：大小写/空白无关；未配置、显式 "default" 与非法值同一种身份。"""
    text = str(value if value is not None else "").strip().lower()
    return text if text in allowed else "default"


def _prompt_identity(profile: str, gen_cfg: dict) -> dict:
    """缓存身份里的「提示词/schema/解码模式影响因子」（Review #9 + #7）。

    收会改变实际发给模型内容（或改变分批而改变内容）的配置：
    - output_language：outline/fill 提示词末尾的语言约束（prompts.language_instruction）
    - fill_max_in：超限时裁剪来源原文的阈值（fill.py）
    - outline_max_in / outline_batch_out：提纲分批方式（批内锚点集合不同 → 产出不同）
    - fill_batch：>1 时 N 块塞进同一条 FILL_MANY_PROMPT 且 max_tokens 按块数缩放，
      合批提示词与单块提示词不同、输出也不同——不是纯吞吐参数（Review P3-G1）。
    - types：生效块类型集（type_guide 进提示词、type 枚举进 outline schema；
      profile 与用户自定义 block_types 都会改变它）——取排序后的类型规格哈希。
    - reasoning_effort / thinking（Review #7）：不改提示词，但改解码/思考模式，
      用户改完点「重新生成」语义上期望重算，不能命中旧缓存拿到旧内容；
      归一后空/未配置/显式 "default"/非法值都记为 "default"（不制造两种身份）。
    temperature / outline_max_out / fill_max_out 仍由 cache_key 的 params 原样并入。
    不收 concurrency/timeout 等纯吞吐参数。
    有意不收 manifest.title / chapter.title：它们确实进 outline/fill 提示词（书名、
    章名），但属于显示名——用户重命名文档/章节后复用旧缓存（内容仍对得上）是刻意
    取舍，避免一次改名触发整本重烧；若改名同时重锚定，outline_hash 变化仍会让缓存失效。
    """
    specs = sorted(
        ({"id": s.type_id, "name": s.cn_name, "hint": s.hint,
          "policy": s.source_policy, "synthesis": bool(s.synthesis_allowed),
          "relations": list(s.allowed_relations)} for s in active_types(profile)),
        key=lambda s: s["id"])
    types_hash = hashlib.sha256(
        json.dumps(specs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {
        "output_language": str(gen_cfg.get("output_language") or "zh").lower(),
        "fill_max_in": _as_int(gen_cfg.get("fill_max_in"), 8000),
        "outline_max_in": _as_int(gen_cfg.get("outline_max_in"), 32000),
        "outline_batch_out": _as_int(gen_cfg.get("outline_batch_out") or 8000, 8000),
        "fill_batch": max(1, _as_int(gen_cfg.get("fill_batch") or 1, 1)),
        "types": types_hash,
        "reasoning_effort": _norm_choice(gen_cfg.get("reasoning_effort"), REASONING_EFFORTS),
        "thinking": _norm_choice(gen_cfg.get("thinking"), THINKING_MODES),
    }


def _outline_batch_records(raw: Any) -> list[dict]:
    """缓存 meta.outline_failed_batches → 规范化记录（旧缓存/坏数据一律忽略）。"""
    out: list[dict] = []
    seen: set[tuple] = set()
    for rec in raw if isinstance(raw, list) else []:
        if not isinstance(rec, dict):
            continue
        raw_ids = rec.get("anchor_ids")
        if not isinstance(raw_ids, list):
            continue        # 坏数据（字符串/数字）不得按字符拆成假锚点
        ids = [str(a) for a in raw_ids if a]
        if not ids:
            continue
        prefix = str(rec.get("prefix") or "")
        key = (prefix, tuple(sorted(ids)))
        if key in seen:
            continue
        seen.add(key)
        out.append({"anchor_ids": ids, "prefix": prefix,
                    "batch_index": rec.get("batch_index"),
                    "reason": str(rec.get("reason") or "")})
    return out


def _cache_meta(outline_complete: bool, failed_batches: list[dict],
                errors: list[str], started: int = 0, unresumable: bool = False) -> dict:
    """缓存元数据：提纲缺失批次必须持久化，续跑才能只补这些批（Review #3）。

    unresumable：旧版本缓存没记录缺失批次、或候选为空等「无法自动补齐」的坏缓存，
    标记后再次生成仍按「请重新生成」提示（第一轮已锁定的旧缓存语义）；没有该标记
    的「不完整且缺批记录为空」缓存才会被当作死端、走全量重新生成。
    """
    meta = {
        "prompt_version": PROMPT_VERSION,
        "outline_failed_batches": [] if outline_complete else list(failed_batches),
        "outline_errors": [] if outline_complete else [str(e) for e in errors],
        # 诊断字段：本次提纲实际发起过多少次请求（含截断缩批子批），便于排查死端
        "outline_started_batches": int(started or 0),
    }
    if unresumable and not outline_complete:
        meta["outline_unresumable"] = True
    return meta


def _missing_batches_after_interrupt(anchors: list[dict], config: dict,
                                     items: list[dict], failures: list[dict],
                                     started: list[dict] | None = None,
                                     done: list[dict] | None = None) -> list[dict]:
    """提纲阶段被中断：按「已完成 + 已失败」覆盖的锚点补出缺失批（含截断缩批）。

    关键：截断缩批的批前缀形如 b0 → b0x00/b0x10 → b0x00x00…，不能靠「见过 b0x
    前缀就把父批 b0 当已触及」——父批 b0 截断后只完成了一个子批就取消时，其余锚点
    从未被覆盖；旧实现据此漏记缺失批（failed_batches=[]），续跑永远 0 调用、partial
    死端（Review P2-G1）。

    done 是 on_batch_done 记录的成功批（前缀 + 该批锚点），覆盖判定以「锚点」为准：
    缺失 = 顶层分批中既未被成功批覆盖、也没有失败记录的锚点。没有 done 时退回用
    「已出条目的批次前缀」近似（兼容调用方）；两者都没有时退回旧前缀粗判。
    """
    def _records(raw: Any) -> list[dict]:
        out: list[dict] = []
        for rec in raw if isinstance(raw, list) else []:
            if not isinstance(rec, dict):
                continue
            ids = [str(a) for a in (rec.get("anchor_ids") or []) if a]
            if ids:
                out.append({"prefix": str(rec.get("prefix") or ""), "anchor_ids": ids})
        return out

    def _covered_ids(records: list[dict]) -> set[str]:
        out: set[str] = set()
        for rec in records:
            out.update(rec["anchor_ids"])
        return out

    if done is not None:
        covered = _covered_ids(_records(done)) | _covered_ids(_records(failures))
    elif started:
        # 兼容：没有「已完成」回调时，用已出条目/已失败批前缀近似已完成批
        heads = {str(it.get("temp_id") or "").split("-", 1)[0] for it in items}
        heads |= {str(rec.get("prefix") or "") for rec in failures}
        covered = _covered_ids([rec for rec in _records(started)
                                if rec["prefix"] in heads])
    else:
        # 兼容路径：没有任何开始信息时只能按 temp_id/失败前缀粗判（旧行为）
        heads = {str(it.get("temp_id") or "").split("-", 1)[0] for it in items}
        heads |= {str(rec.get("prefix") or "") for rec in failures}
        heads.discard("")

        def _touched(root: str) -> bool:
            return any(h == root or h.startswith(root + "x") for h in heads)

        missing: list[dict] = []
        for bi, batch in enumerate(plan_batches(anchors, config)):
            root = f"b{bi}"
            if _touched(root):
                continue
            missing.append({"anchor_ids": [a["anchor_id"] for a in batch], "prefix": root,
                            "batch_index": bi, "reason": "提纲中断：该批尚未完成"})
        return missing

    missing = []
    for bi, batch in enumerate(plan_batches(anchors, config)):
        pending = [a["anchor_id"] for a in batch if a["anchor_id"] not in covered]
        if pending:
            missing.append({"anchor_ids": pending, "prefix": f"b{bi}", "batch_index": bi,
                            "reason": "提纲中断：该批未完成"})
    return missing


def _remap_temp_ids(items: list[dict], existing: list[dict]) -> None:
    """补批条目换到本次运行独占的 temp_id 命名空间（不得与缓存里已成功条目撞名）。"""
    if not items:
        return
    used = {str(c.get("temp_id") or "") for c in existing}
    root = "r"
    while any(tid == root or tid.startswith(root + "-") for tid in used):
        root += "r"
    mapping = {it["temp_id"]: f"{root}-{i}" for i, it in enumerate(items)}
    for it in items:
        it["temp_id"] = mapping[it["temp_id"]]
        it["depends_on"] = [mapping.get(dep, dep) for dep in it.get("depends_on", [])]


def generate_chapter(doc_id: str, chapter_id: str, *, layout: Layout | None = None,
                     client: LLMClient | None = None, config: dict | None = None,
                     registry: ModelRegistry | None = None, model: str | None = None,
                     force: bool = False, use_cache: bool = True, merge_mode: str = "merge",
                     resume: bool = False,
                     task_id: str | None = None,
                     progress: Callable[[dict], None] | None = None,
                     is_cancelled: Callable[[], bool] | None = None) -> dict:
    """分章生成笔记块；返回 ChapterReport（部分失败不误删旧块）。

    预算口径（5.3.5）：每次调用（= 每章每次生成/续跑）建一个 BudgetGuard，
    覆盖本次运行的提纲、填充、提纲补批与缓存失败补填；不跨运行累计。
    """
    layout = layout or Layout(Path.cwd())
    config = config or {}
    store = DocStore(layout, doc_id)
    manifest = store.manifest()
    from src.core.note_templates import (StyledClient, book_template,
                                          generation_identity, generation_style)

    template = book_template(layout, doc_id)
    if template:
        # A book owns a fixed template snapshot; later library edits do not change it.
        manifest = {**manifest, "profile": template.get("profile", manifest.get("profile", "textbook"))}
        config = {**config, "generate": {**(config.get("generate") or {}),
                                          "note_template_identity": generation_identity(template)}}
        if client is not None:
            client = StyledClient(client, generation_style(template))
    profile = manifest.get("profile", "textbook")
    chapter = store.chapter(chapter_id)
    anchors = store.anchor_rows(chapter_id)
    anchors_by_id = {a["anchor_id"]: a for a in anchors}
    if not anchors:
        raise PipelineError(f"章节 {chapter_id} 没有锚点，请先运行 anchor")

    def _check_cancel() -> None:
        if is_cancelled and is_cancelled():
            raise TaskCancelled("任务已取消")

    outline_hash = chapter.get("outline_hash") or ""
    registry = registry or ModelRegistry(layout)
    gen_cfg = config.get("generate", {}) or {}
    models_cfg = gen_cfg.get("models", {}) or {}
    outline_model = model or models_cfg.get("outline") or models_cfg.get("default")
    fill_model = model or models_cfg.get("fill") or models_cfg.get("default")
    # 缓存身份 = 旧参数（温度/输出上限/提示词版本）+ 提示词/schema 影响因子（Review #9）：
    # 只把 output_language 加进去仍会漏「改 fill_max_in/分批/块类型后命中旧缓存」，
    # 故统一由 _prompt_identity 生成一份紧凑指纹并入 params。
    key = cache_key(
        doc_id=doc_id, chapter_id=chapter_id, outline_hash=outline_hash, profile=profile,
        registry_version="1",
        outline_model=model_fingerprint(registry, outline_model),
        fill_model=model_fingerprint(registry, fill_model),
        params={"temperature": gen_cfg.get("temperature"),
                "outline_max_out": gen_cfg.get("outline_max_out", 16000),
                "fill_max_out": gen_cfg.get("fill_max_out", 6000),
                "prompt_version": PROMPT_VERSION,
                **({"note_template": gen_cfg["note_template_identity"]} if template else {}),
                **_prompt_identity(profile, gen_cfg)})

    report: dict[str, Any] = {
        "doc_id": doc_id, "chapter_id": chapter_id, "status": "complete",
        "cache_hit": False, "outline_complete": True, "created_at": utc_now_iso(),
        "model": {"outline": outline_model, "fill": fill_model},
        "failures": [],
        "outline_errors": [],
        "outline_missing_batches": 0,
        "outline_batches_started": 0,
        # 记录本次生成对应的锚点指纹：全书生成据此判断「已生成完且锚点没变」才跳过，
        # 重锚定/换提示词后的章节不会被旧报告误跳
        "outline_hash": outline_hash,
    }
    t_start = time.monotonic()

    def _finish(rep: dict) -> dict:
        """收尾报告：总耗时 + LLM 调用遥测（排队长=全局闸紧，调用长=模型慢）。"""
        timing = rep.setdefault("timing", {})
        timing["total_s"] = round(time.monotonic() - t_start, 2)
        stats = getattr(client, "stats", None)
        if isinstance(stats, dict):
            timing["llm"] = {k: (round(v, 2) if isinstance(v, float) else v)
                             for k, v in stats.items()}
        _write_report(layout, doc_id, chapter_id, rep)
        return rep
    if progress:
        progress({"stage": "start", "chapter_id": chapter_id})

    # 预算口径（5.3.5，Review #5）：每次 generate_chapter 运行建一个 BudgetGuard，
    # 覆盖本次运行的提纲、填充、缓存失败补填与提纲补批。同一章的多次续跑各自重新计量
    # （上一轮的花费不滚进本轮，否则充值后的「继续生成」会被历史用量挡死）。
    # 原实现只在「无缓存」分支建 guard，命中缓存后的失败补填显式 budget=None，
    # 完全绕过 max_cost_per_chapter。
    budget = BudgetGuard(float(gen_cfg.get("max_cost_per_chapter", 0.0) or 0.0))

    cached = None
    if use_cache and not force:
        cached = read_cache(layout, doc_id, chapter_id, key)
    if cached is None and resume:
        # 续跑语义：无视缓存键差异（换模型/升级提示词不该让 115/117 重烧），
        # 吃掉该章已有缓存里的完成块，只补失败/缺失部分
        cached = read_cache_any(layout, doc_id, chapter_id)
        if cached is not None:
            report["resumed"] = True

    if cached is not None:
        _cached_meta = cached.get("meta") or {}
        # 死端兜底：新格式缓存声称提纲不完整、却没有任何可重建的缺失批记录，且不是
        # 旧缓存兜底标记过的「不可续跑」缓存——这是历史「截断缩批递归中断」bug 的产物
        # （父批被误判已触及），续跑/重试只会 0 提纲调用、永远 partial。视为缓存不可用，
        # 走真正的全量重新生成（无 client 时仍按旧方式提示，不丢已有块）。
        _dead_end = (not bool(cached.get("outline_complete", True))
                     and "outline_failed_batches" in _cached_meta
                     and not _cached_meta.get("outline_unresumable")
                     and not _outline_batch_records(_cached_meta.get("outline_failed_batches")))
        if _dead_end and client is not None:
            report["cache_discarded"] = "incomplete_cache_without_failed_batches"
            report.pop("resumed", None)
            cached = None

    if cached is not None:
        report["cache_hit"] = True
        candidates = cached.get("candidates", [])
        cached_meta = cached.get("meta") or {}
        report["outline_complete"] = bool(cached.get("outline_complete", True))
        report["outline_errors"] = [str(e) for e in (cached_meta.get("outline_errors") or [])]
        report["outline_failed_batches"] = _outline_batch_records(
            cached_meta.get("outline_failed_batches"))
        report["outline_missing_batches"] = len(report["outline_failed_batches"])
        report["outline_batches_started"] = _as_int(
            cached_meta.get("outline_started_batches"), 0)
        cache_dirty = False
        cache_unresumable = bool(cached_meta.get("outline_unresumable"))
        if not candidates and report["outline_complete"]:
            # 空候选却声称提纲完整 = 不可信缓存（损坏/旧版本异常落盘）。照常提交会让
            # merge 按「完整提纲明确排除」把本章未保护旧块当淘汰对象软删，报告还谎报
            # complete——降级为不完整，走「请重新生成」提示，旧块一块不动。
            report["outline_complete"] = False

        # 缓存里的失败候选必须重试：否则重跑同一章只是复读旧失败（一个块失败即永久失败）。
        # 部分失败的章节尤其需要：缓存带着 22 个失败项时，用户点「生成本章」期望的是补齐而不是原样重报。
        # 补填与正常填充共用本次运行的 budget（Review #5：不得绕过 max_cost_per_chapter）。
        failed = [c for c in candidates if c.get("_status") == "failed" and c.get("temp_id")]
        if failed and client is not None:
            if progress:
                progress({"stage": "fill_retry", "count": len(failed)})
            retry_specs = [{"temp_id": c["temp_id"], "type": c.get("type"),
                            "title": c.get("title", ""), "basis": c.get("basis", "anchored"),
                            "sources": c.get("sources", []),
                            "depends_on": c.get("depends_on", [])} for c in failed]
            refilled = fill_all(client, doc_id=doc_id, chapter_id=chapter_id,
                                outline_items=retry_specs, anchors_by_id=anchors_by_id,
                                anchors=anchors, model=fill_model,
                                config={**config, "_profile": profile},
                                concurrency=int(gen_cfg.get("concurrency", 2)),
                                budget=budget, task_id=task_id, progress=progress,
                                is_cancelled=is_cancelled)
            for c in candidates:
                f = refilled.get(c["temp_id"])
                if not f:
                    continue
                if f.get("_status") == "failed":
                    # 补填仍失败：保留原候选，但刷新失败原因（最新诊断优先，如预算拦截）
                    c["_errors"] = f.get("_errors", c.get("_errors", []))
                    continue
                c["sources"] = _normalize_sources(f.get("sources_patch", c.get("sources", [])))
                c["basis"] = f.get("basis", c.get("basis", "anchored"))
                c["content"] = f.get("content", "")
                c["content_format"] = f.get("content_format", "md_latex")
                c["tags"] = f.get("tags", [])
                c["_status"] = f.get("_status", "ok")
                c["_errors"] = f.get("_errors", [])
                c["status"] = "needs_review" if f.get("_status") == "needs_review" else "ai_generated"
            cache_dirty = True

        if not report["outline_complete"]:
            # 提纲缺批（Review #3）：续跑只重建缓存记录的缺失批次，复用已成功批次的
            # 候选与已填充内容（不重烧），新条目只填充新块（同样受 budget 约束）。
            missing = report["outline_failed_batches"]
            if not missing:
                # 旧版本缓存 meta 没有 outline_failed_batches（或锚点已变）：无法知道
                # 缺哪几批，不能假装补齐，更不能因此报 complete——明确提示重新生成。
                hint = ("缓存没有可提交的候选块（可能已损坏），无法自动补齐；请重新生成本章"
                        if not candidates else
                        "缓存未记录可重建的缺失提纲批次（旧版本缓存或锚点已变），"
                        "无法自动补齐；请重新生成本章")
                # 多次点「继续生成」不堆叠同一条提示（否则缓存里的错误列表无限变长）
                if hint not in report["outline_errors"]:
                    report["outline_errors"].append(hint)
                # 标记「不可续跑」：第一轮锁定的旧缓存语义（提示重生成、保持 partial），
                # 下次生成不得被死端兜底误判成可丢弃缓存（避免同一提示来回翻转行为）
                cache_unresumable = True
                cache_dirty = True
            elif client is None:
                # 缺失批次记录齐全，但本次没有 LLM 客户端可用：如实说明原因，别误导成
                # 「缓存没记录」——已成功的块与缺失记录都还在，配好客户端再续跑即可
                hint = "未配置 LLM 客户端，无法自动补齐缺失提纲批次；请重新生成本章"
                if hint not in report["outline_errors"]:
                    report["outline_errors"].append(hint)
                cache_dirty = True
            else:
                if progress:
                    progress({"stage": "outline_retry", "batches": len(missing)})
                wanted = {aid for rec in missing for aid in rec["anchor_ids"]}
                sub_anchors = [a for a in sorted(anchors, key=lambda a: (a["page"], a["reading_order"]))
                               if a["anchor_id"] in wanted]
                unknown = sorted(wanted - set(anchors_by_id))
                report["outline_errors"] = []
                if not sub_anchors:
                    report["outline_errors"].append(
                        "缺失提纲批次的锚点已不存在（可能已重锚定），无法自动补齐；请重新生成本章")
                    report["outline_missing_batches"] = len(missing)
                else:
                    retry = generate_outline(
                        client, doc_id=doc_id, chapter_id=chapter_id, manifest=manifest,
                        anchors=sub_anchors, model=outline_model,
                        config={**config, "_profile": profile}, budget=budget,
                        task_id=task_id, progress=progress, is_cancelled=is_cancelled,
                        _prefix="r")
                    _remap_temp_ids(retry.items, candidates)
                    report["outline_batches_started"] = (
                        _as_int(report.get("outline_batches_started"), 0)
                        + len(retry.started_batches))
                    if retry.items:
                        new_filled = fill_all(
                            client, doc_id=doc_id, chapter_id=chapter_id,
                            outline_items=retry.items, anchors_by_id=anchors_by_id,
                            anchors=anchors, model=fill_model,
                            config={**config, "_profile": profile},
                            concurrency=int(gen_cfg.get("concurrency", 2)),
                            budget=budget, task_id=task_id, progress=progress,
                            is_cancelled=is_cancelled)
                        candidates.extend(_build_candidates(retry.items, new_filled))
                    remaining = list(retry.failed_batches)
                    report["outline_errors"].extend(str(e) for e in retry.errors)
                    if not retry.items and not retry.failed_batches:
                        # 补批返回空提纲（校验层允许 blocks: []）：不能当成「已补齐」——
                        # 否则 outline_complete 会被置真、缺失批的块永远不产出；若缓存
                        # 候选也为空，合并还会按完整提纲淘汰整章旧块。记为缺失、保持
                        # partial，让用户明确看到并重试（每次续跑只重试一次，不自动死循环）。
                        empty_reason = "补批未产出任何块（模型返回空提纲），请重新生成本章"
                        remaining.append({
                            "anchor_ids": [a["anchor_id"] for a in sub_anchors],
                            "prefix": "", "batch_index": None, "reason": empty_reason})
                        report["outline_errors"].append(empty_reason)
                    if unknown:
                        remaining.append({"anchor_ids": unknown, "prefix": "",
                                          "batch_index": None,
                                          "reason": "锚点已不存在，无法重建"})
                        report["outline_errors"].append(
                            f"{len(unknown)} 个缺失锚点已不存在（可能已重锚定），无法重建")
                    report["outline_failed_batches"] = remaining
                    report["outline_missing_batches"] = len(remaining)
                    report["outline_complete"] = not remaining
                    if report["outline_complete"]:
                        report["outline_errors"] = []
                cache_dirty = True

        if cache_dirty and use_cache:
            write_cache(layout, doc_id, chapter_id, key,
                        {"candidates": candidates,
                         "outline_complete": report["outline_complete"],
                         "meta": _cache_meta(report["outline_complete"],
                                             report.get("outline_failed_batches") or [],
                                             report.get("outline_errors") or [],
                                             _as_int(report.get("outline_batches_started"), 0),
                                             cache_unresumable)})
    else:
        if client is None:
            raise PipelineError("未配置 LLM 客户端，且无可用缓存")
        _check_cancel()
        fill_conc = int(gen_cfg.get("concurrency", 2))
        # 提纲/填充流水线（提速「任务启动」）：每批提纲校验通过即开始填充该批，
        # 不等整章提纲完成——首条笔记的等待从「全部提纲 + 首次填充」缩到
        # 「首批提纲 + 首次填充」。concurrency=1 语义是严格串行，不做重叠；
        # depends_on 受校验约束只能指向同批 temp_id，逐批填充不破坏 5.3.1。
        pipelined = fill_conc > 1
        pipe_lock = threading.Lock()
        filled_all: dict[str, dict] = {}
        acc_items: list[dict] = []      # 已出批的提纲条目（断点快照用）
        outline_failures: list[dict] = []   # 提纲失败批次（中断快照也要保住，续跑据此补批）
        started_batches: list[dict] = []    # 真正发过请求的提纲批（含截断缩批子批）
        done_batches: list[dict] = []       # 成功完成的提纲批（锚点已覆盖，供快照补缺）
        fill_state = {"total": 0}
        batch_done: dict[int, int] = {}
        fill_futs: list = []
        batch_seq = [0]
        # 外层分派池放开到 fill_conc：曾钳在 4 使多批填充最多 4 路并行（实测有效并发 4.2，
        # 而配置的并发是 10）；真正的总闸是 LLMClient 的 max_concurrent_requests 信号量，
        # 等闸时间记入 wait_seconds，多开线程无害
        fill_pool = (ThreadPoolExecutor(max_workers=max(1, fill_conc))
                     if pipelined else None)

        def _on_batch_failed(record: dict) -> None:
            with pipe_lock:
                outline_failures.append(record)

        def _on_batch_started(prefix: str, anchor_ids: list[str]) -> None:
            # 每批真正发起请求前记录（含截断缩批子批）：快照据此补出未开始的批
            with pipe_lock:
                started_batches.append({"prefix": str(prefix),
                                        "anchor_ids": [str(a) for a in anchor_ids]})

        def _on_batch_done(prefix: str, anchor_ids: list[str]) -> None:
            # 成功返回（校验通过）的批：快照按「已覆盖锚点」补缺失批，不靠前缀猜测
            with pipe_lock:
                done_batches.append({"prefix": str(prefix),
                                     "anchor_ids": [str(a) for a in anchor_ids]})

        def _fill_progress(idx: int):
            def _p(evt: dict) -> None:
                if (evt or {}).get("stage") != "fill":
                    return
                with pipe_lock:
                    batch_done[idx] = int(evt.get("done") or 0)
                    done = sum(batch_done.values())
                    total = fill_state["total"]
                if progress:
                    progress({"stage": "fill", "done": done, "total": total})
            return _p

        def _run_fill(items: list[dict], idx: int) -> None:
            out = fill_all(client, doc_id=doc_id, chapter_id=chapter_id,
                           outline_items=items, anchors_by_id=anchors_by_id,
                           anchors=anchors, model=fill_model,
                           config={**config, "_profile": profile},
                           concurrency=fill_conc, budget=budget, task_id=task_id,
                           progress=_fill_progress(idx), is_cancelled=is_cancelled)
            with pipe_lock:
                filled_all.update(out)

        def _on_items(items: list[dict]) -> None:
            with pipe_lock:
                idx = batch_seq[0]
                batch_seq[0] += 1
                acc_items.extend(items)
                fill_state["total"] += len(items)
                done = sum(batch_done.values())
                total = fill_state["total"]
            if progress:
                progress({"stage": "fill", "done": done, "total": total})
            fill_futs.append(fill_pool.submit(_run_fill, items, idx))

        def _collect_items(items: list[dict]) -> None:
            # 非流水线模式不提前填充，但必须收集已出批条目：提纲阶段中断时快照要靠它
            # 保住这些已成功批的条目，否则「已覆盖锚点」仍在缺失计算里却无块可续，漏块
            with pipe_lock:
                acc_items.extend(items)

        t_gen = time.monotonic()
        outline = None
        try:
            outline = generate_outline(client, doc_id=doc_id, chapter_id=chapter_id, manifest=manifest,
                                       anchors=anchors, model=outline_model,
                                       config={**config, "_profile": profile},
                                       budget=budget, task_id=task_id, progress=progress,
                                       is_cancelled=is_cancelled,
                                       on_items=_on_items if pipelined else _collect_items,
                                       on_batch_failed=_on_batch_failed,
                                       on_batch_started=_on_batch_started,
                                       on_batch_done=_on_batch_done)
            report["outline_complete"] = outline.complete
            report["outline_errors"] = list(outline.errors)
            report["outline_failed_batches"] = list(outline.failed_batches)
            report["outline_missing_batches"] = len(outline.failed_batches)
            report["outline_batches_started"] = len(outline.started_batches)
            if not outline.items:
                if fill_pool is not None:
                    fill_pool.shutdown(wait=True)
                report["status"] = "failed"
                report["failures"].append({"stage": "outline", "reason": "; ".join(outline.errors) or "空提纲"})
                _finish(report)
                return report
            if progress:
                progress({"stage": "outline_done", "count": len(outline.items)})
            _check_cancel()
            if pipelined:
                for fut in fill_futs:
                    fut.result()      # 填充内意外异常在此冒泡（LLM 失败已按块隔离）
                filled = filled_all
            else:
                filled = fill_all(client, doc_id=doc_id, chapter_id=chapter_id,
                                  outline_items=outline.items, anchors_by_id=anchors_by_id,
                                  anchors=anchors, model=fill_model,
                                  config={**config, "_profile": profile},
                                  concurrency=fill_conc, budget=budget, task_id=task_id,
                                  progress=progress, is_cancelled=is_cancelled)
        except BaseException:
            if fill_pool is not None:
                # 取消/异常：不等在途填充（它们会在下一个检查点自行退出）
                fill_pool.shutdown(wait=False, cancel_futures=True)
            # 断点保护：已产出结果落缓存（取消/配额/意外异常都不丢），
            # 下次生成自动续填失败块、复用已完成块——不再整章作废重烧。
            # 提纲缺失批次同样落缓存（Review #3）：中断在提纲阶段时用分批计划
            # 找出「既没出条目也没记失败」的未尝试批，续跑才可能补齐整章提纲。
            try:
                with pipe_lock:
                    snap_items, snap_filled = list(acc_items), dict(filled_all)
                    snap_failures = list(outline_failures)
                    snap_started = list(started_batches)
                    snap_done = list(done_batches)
                if outline is not None:
                    # 提纲已返回：outline.items 才是权威的已完成批（流水线/非流水线回调
                    # 都可能只覆盖子集）；按 temp_id 去重合并，避免续跑漏块。
                    seen_tids = {str(it.get("temp_id") or "") for it in outline.items}
                    snap_items = list(outline.items) + [
                        it for it in snap_items if str(it.get("temp_id") or "") not in seen_tids]
                if use_cache and (snap_items or snap_filled or snap_failures or snap_started):
                    if outline is not None:
                        # 提纲已返回：它的 failed_batches 才是权威（含未尝试批不会漏）
                        failed_batches = list(outline.failed_batches)
                        outline_errors = list(outline.errors)
                        outline_ok = outline.complete
                        started_count = len(outline.started_batches)
                    else:
                        # 提纲还没返回：按「已完成/已失败覆盖的锚点」补出缺失批——截断缩批
                        # 的父批只完成部分子批时，剩余锚点必须记录，否则续跑 0 调用死端。
                        failed_batches = list(snap_failures) + _missing_batches_after_interrupt(
                            anchors, config, snap_items, snap_failures,
                            snap_started or None, snap_done or None)
                        outline_errors = [str(rec.get("reason") or "") for rec in failed_batches]
                        # 所有批都已成功完成（异常发生在收尾回调等场景）→ 提纲实际完整；
                        # 若仍写 False 会制造「不完整且无缺失记录」的死端缓存，与修复目标相悖
                        outline_ok = not failed_batches and bool(snap_done)
                        started_count = len(snap_started)
                    write_cache(layout, doc_id, chapter_id, key,
                                {"candidates": _build_candidates(snap_items, snap_filled),
                                 "outline_complete": outline_ok,
                                 "meta": _cache_meta(outline_ok, failed_batches, outline_errors,
                                                     started_count)})
            except Exception:
                pass      # 快照失败不得掩盖原始异常
            raise
        else:
            if fill_pool is not None:
                fill_pool.shutdown(wait=True)
        report.setdefault("timing", {})["gen_s"] = round(time.monotonic() - t_gen, 2)  # 提纲+填充墙钟（流水线重叠）
        candidates = _build_candidates(outline.items, filled)
        if use_cache:
            write_cache(layout, doc_id, chapter_id, key,
                        {"candidates": candidates, "outline_complete": outline.complete,
                         "meta": _cache_meta(outline.complete, outline.failed_batches,
                                             outline.errors, len(outline.started_batches))})

    # 4.6.1 归一（缓存路径同样适用：旧缓存可能带着未归一的重复来源）
    for c in candidates:
        c["sources"] = _normalize_sources(c.get("sources"))

    candidates = _derive_heading_and_order(candidates, anchors, chapter_id)
    # 写入段整体按文档串行（_commit_lock）：并行分章时两章的 merge/commit/link
    # 排队执行，避免互撞 RevisionConflict。CAS 基线取在 merge 读取时刻（I7/11.6.3）：
    # outline+fill 可达十几分钟，若基线取在生成开始，期间用户随手编辑一块就让
    # 整章提交 Conflict、几十次 LLM 调用全部作废；merge 本就是按当前状态设计的
    # （保护块跳过），用 merge 时刻的 revision 既不丢用户并发改动、又不误伤在途生成。
    t_commit = time.monotonic()
    if progress:
        # 最后一步要有名字：合并/提交/链接此前无进度事件，界面像卡在「填充 N/N」
        progress({"stage": "link"})
    with _commit_lock(layout, doc_id):
        base_revision = store.data_revision()
        # 失败候选必须交给合并器：保留对应旧块并进报告，不能据此删除（5.7 第 1 条）
        mutation, merge_report = merge_candidates(store, chapter_id, candidates,
                                                  mode=merge_mode,
                                                  outline_complete=report["outline_complete"])
        report.update({k: v for k, v in merge_report.items() if k != "temp_to_id"})
        if merge_report.get("failures"):
            report["status"] = "partial"
        if not report.get("outline_complete", True):
            # 提纲不完整 → 不得报 complete（Review #3）：全书生成按「complete 报告 +
            # 锚点未变」跳过章节，误报 complete 会让缺失批次永远补不回来。
            # 已成功的块照常提交；merge 因 outline_complete=False 不会淘汰旧块。
            report["status"] = "partial"
            detail = "；".join(str(e) for e in (report.get("outline_errors") or [])[:3])
            missing_n = report.get("outline_missing_batches") or 0
            # 旧版本缓存（meta 无缺失批次记录）时数量未知，别写成误导性的「缺失 0 批」
            head = (f"提纲未完整（缺失 {missing_n} 批）：" if missing_n
                    else "提纲未完整（缺失批次未能记录）：")
            report["failures"].insert(0, {
                "stage": "outline",
                "reason": (head
                           + (detail or "部分批次失败，已保留成功块，再次生成只补缺失批次"))[:400],
            })

        _check_cancel()
        try:
            store.commit(mutation, base_revision=base_revision, sync_backlinks=True)
        except RevisionConflict as exc:
            report["status"] = "failed"
            report["failures"].append({"stage": "commit", "reason": exc.message})
            _finish(report)
            raise

        # 链接阶段（5.4）：本章块出链与反向索引全量校验（同锁内：link 自带 CAS 提交）
        try:
            from src.link.backlink import link_doc

            link_report = link_doc(doc_id, chapter_id=chapter_id, layout=layout)
            report["link"] = link_report
        except Exception as exc:  # 链接失败不影响已提交块
            report["link"] = {"error": str(exc)}

    report.setdefault("timing", {})["commit_s"] = round(time.monotonic() - t_commit, 2)  # 含锁排队+合并+提交+链接
    if report.get("failures") or merge_report.get("failures"):
        report["status"] = "partial"
    # 配额耗尽 = 「暂停」而非报错：已完成块已保存并会被下次生成复用
    if report.get("status") == "partial" and getattr(client, "halted_reason", None):
        report["status"] = "paused"
        report["resume_hint"] = (("配额已用尽（%s）：已完成的块已保存并在下次生成时复用；"
                                  "为供应商充值/换 Key/换模型后再次点击生成，将自动续填剩余块。")
                                 % str(client.halted_reason)[:120])
    _finish(report)
    return report


def _write_report(layout: Layout, doc_id: str, chapter_id: str, report: dict) -> None:
    out = layout.reports_doc_dir(doc_id) / f"{chapter_id}.generate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
