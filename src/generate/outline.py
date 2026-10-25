"""第一次生成：章节提纲（5.3.1）。临时 ID + DAG + token 预算分批 + 截断缩批重试。"""

from __future__ import annotations

import json
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from src.core.errors import (LLMError, PriceUnknown, QuotaExhausted, RetryableError,
                             SchemaViolation, TaskCancelled)
from src.core.block_types import type_guide
from src.core.llm import BudgetGuard, LLMClient
from src.core.text import estimate_tokens

from .prompts import (OUTLINE_PROMPT, SYSTEM_PROMPT, anchor_summary_line, compact_schema,
                      build_outline_schema, language_instruction)
from .validate_retry import parse_json_output, validate_outline_payload


_PREFACE_RE = re.compile(r"前言|序言|引言|绪论|目录|记号|记法|致谢|附录|contents|preface|foreword|notation", re.I)
# 体量截断的牺牲顺序（数值小的先剪）：定义/定理等必保类型不进此表
_TRIM_ORDER = {"link": 0, "motivation": 1, "figure": 2}


def _trim_to_cap(items: list[dict], cap: int) -> list[dict]:
    """按体量上限截断：先剪 link/motivation/figure，定义/定理等必保全覆盖。

    「覆盖全部定义与定理」优先于体量上限（提纲规则硬约束）；
    必保类型即使超限也全部保留。
    """
    keep = [it for it in items if it.get("type") not in _TRIM_ORDER]
    flex = sorted((it for it in items if it.get("type") in _TRIM_ORDER),
                  key=lambda it: (_TRIM_ORDER.get(it.get("type"), 9), it.get("order", 0)))
    room = max(0, cap - len(keep))
    merged = keep + flex[:room]
    merged.sort(key=lambda it: it.get("order", 0))
    return merged


def chapter_size_plan(n_anchors: int, chapter_title: str) -> tuple[int, int, str]:
    """章体量自适应：返回（目标块数, 硬上限, 提示词说明）。

    前言/目录/记号类与小章从严控量（曾出现 7 条锚点出 6 块、前言出 38 块的
    注水）；目标靠提示词施压，上限只是反注水兜底（定义/定理不受限，必保全覆盖）。
    """
    if _PREFACE_RE.search(chapter_title or ""):
        target = max(2, n_anchors // 25)
        return target, target + 2, (
            f"本章属于前言/目录/记号类章节，内容不多，只生成 {target} 个块左右"
            f"（最多 {target + 2} 个）；宁缺毋滥，不做展开性笔记。")
    if n_anchors <= 15:
        target = max(2, n_anchors // 4)
        return target, max(4, n_anchors // 2), (
            f"本章篇幅很小，只生成 {target} 个块左右（不超过 {max(4, n_anchors // 2)} 个），只记最核心的点。")
    if n_anchors <= 60:
        target = max(4, n_anchors // 5)
        return target, n_anchors // 2, f"生成约 {target} 个块（总量不超过 {n_anchors // 2} 个）。"
    target = n_anchors // 8 + 4
    return target, n_anchors // 2, (
        f"生成约 {target} 个块；以内容需要为准，可适当增减，但不得为凑数注水。")

# 实测（DeepSeek 系模型）：提纲条目输出 ≈ 锚点摘要输入的 6 倍（1129 in → 7012 out）。
# 只按输入预算分批时大批必被 max_tokens 截断 → 缩批重试（每轮都是真实调用，代价高），
# 故分批同时受「估算输出预算」约束，让一批的预期输出落在 max_tokens 之内。
_OUT_RATIO = 6.0
_OUT_RESERVE = 400  # schema/收尾安全余量


@dataclass
class OutlineResult:
    items: list[dict] = field(default_factory=list)
    complete: bool = True
    batches: int = 0
    errors: list[str] = field(default_factory=list)
    # 失败批次记录（Review #3）：引擎据此持久化进缓存，续跑只重建这些批、复用已成功批。
    # 每项 {"anchor_ids": [...], "prefix": "b3", "batch_index": 3, "reason": "..."}；
    # 截断缩批递归失败时由子调用记录（prefix 已是正确的子前缀）。
    failed_batches: list[dict] = field(default_factory=list)
    # 真正发起过请求的批记录（含截断缩批子批）：中断快照据此区分「从未开始 /
    # 已开始未完成 / 已出条目 / 已失败」——父批截断后只完成 b0x0 就取消时，
    # 若只看前缀会把父批当已触及、漏记 b0x1，续跑永远补不上（partial 死端）。
    started_batches: list[dict] = field(default_factory=list)
    # 成功返回（校验通过，含空 blocks）的批记录：中断快照按「已覆盖锚点」补缺失批。
    # 不能拿「有条目」当完成依据——空输出批没有条目，但它确实已被处理过（不是缺失）。
    done_batches: list[dict] = field(default_factory=list)


def _anchor_cost(anchor: dict) -> tuple[float, float]:
    """返回（估算输入 token，估算输出 token）。"""
    cost_in = estimate_tokens(anchor_summary_line(anchor)) + 8
    return cost_in, cost_in * _OUT_RATIO


def _batch_anchors(anchors: list[dict], max_in: float, max_out: float = float("inf")) -> list[list[dict]]:
    """按 heading/阅读序分批；同时受输入预算与估算输出预算约束（5.3.2）。"""
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_in = 0.0
    cur_out = 0.0
    reserve = 1500  # 系统提示/schema 安全余量
    for anchor in sorted(anchors, key=lambda a: (a["page"], a["reading_order"])):
        cost_in, cost_out = _anchor_cost(anchor)
        if cur and (cur_in + cost_in + reserve > max_in
                    or cur_out + cost_out + _OUT_RESERVE > max_out):
            batches.append(cur)
            cur, cur_in, cur_out = [], 0.0, 0.0
        cur.append(anchor)
        cur_in += cost_in
        cur_out += cost_out
    if cur:
        batches.append(cur)
    return batches or [[]]


def plan_batches(anchors: list[dict], config: dict) -> list[list[dict]]:
    """按当前生成配置计算顶层提纲分批（引擎中断快照据此识别「尚未尝试」的批次）。"""
    gen_cfg = config.get("generate", {}) or {}
    max_in = int(gen_cfg.get("outline_max_in", 32000))
    max_out = int(gen_cfg.get("outline_max_out", 16000))
    batch_out = int(gen_cfg.get("outline_batch_out", 8000) or 8000)
    return _batch_anchors(anchors, max_in, min(batch_out, max_out))


def generate_outline(client: LLMClient, *, doc_id: str, chapter_id: str, manifest: dict,
                     anchors: list[dict], model: str | None, config: dict,
                     budget: BudgetGuard | None = None, task_id: str | None = None,
                     progress: Callable[[dict], None] | None = None,
                     is_cancelled: Callable[[], bool] | None = None,
                     on_items: Callable[[list[dict]], None] | None = None,
                     on_batch_failed: Callable[[dict], None] | None = None,
                     on_batch_started: Callable[[str, list[str]], None] | None = None,
                     on_batch_done: Callable[[str, list[str]], None] | None = None,
                     _prefix: str = "b") -> OutlineResult:
    """逐批生成提纲。

    _prefix 是 temp_id 前缀：顶层 "b"，截断缩批递归带路径后缀（如 b0x1x0），
    保证跨调用全局唯一——temp_id 撞名会让 fill_all 把同名项折叠成一个、
    依赖错乱、块丢失（只在大章节截断缩批时触发，隐蔽且破坏性大）。

    on_items：每批校验通过后立即回调该批条目（temp_id 已加前缀），
    供引擎做「提纲/填充流水线」——首批一出就开始填充，不等整章提纲。
    on_batch_failed：每批失败时回调其记录（含该批锚点与 temp_id 前缀），
    供引擎在中断快照里也保住「哪些批缺失」——续跑据此只补批。
    on_batch_started(prefix, anchor_ids)：每批真正发起请求前回调（截断缩批的
    递归子批同样回调）。中断快照据此知道哪些批「从未开始」、哪些「已开始但
    未完成」——父批 b0 截断后只出 b0x0 就取消时，b0x1 必须补记为缺失，
    否则续跑会把父批当已触及、永远 0 调用补不上（partial 死端）。
    on_batch_done(prefix, anchor_ids)：每批成功返回（校验通过，含空 blocks）后回调，
    供中断快照按「已覆盖锚点」补缺失批，而不是靠前缀猜测父批是否完成。
    """
    profile = manifest.get("profile", "textbook")
    chapter = next((c for c in manifest.get("chapters", []) if c["chapter_id"] == chapter_id), {})
    schema = build_outline_schema(profile)
    gen_cfg = config.get("generate", {}) or {}
    max_out = int(gen_cfg.get("outline_max_out", 16000))
    # 分批目标（提速「任务启动」）：实测 MiMo 单批输出 7k-16k tokens 要 3-8 分钟，
    # 若全部提纲完成才开始填充，任务开头几分钟毫无动静。分批目标单独可调
    # （outline_batch_out，默认 8000 ≈ 每批 60-70s），outline_max_out 仍是
    # 防截断的 API max_tokens 上限——两者解耦，缩小分批不会带来截断风险。
    batches = plan_batches(anchors, config)
    result = OutlineResult(batches=len(batches))
    if progress:
        # 启动即报批次总数：任务一开始进度条就有明确去向，而不是干等首批完成
        progress({"stage": "outline_start", "batches": len(batches)})
    anchor_ids = {a["anchor_id"] for a in anchors}
    size_target, size_cap, size_note = chapter_size_plan(len(anchors), chapter.get("title", ""))
    # 动机块总量随目标块数缩放：小章/前言不再每批都喊「最多 6 个」
    mot_total = max(1, min(6, size_target // 3 or 1))

    def _run_batch(bi: int, batch: list[dict]) -> OutlineResult:
        prefix = f"{_prefix}{bi}"
        sub = OutlineResult()
        batch_ids = [a["anchor_id"] for a in batch]
        # 「本批真正开始请求」记录：在首次调用模型之前登记（含递归子批），
        # 中断快照据此补出未完成/未开始的批，避免父批前缀掩盖未完成子批。
        sub.started_batches.append({"prefix": prefix, "anchor_ids": list(batch_ids)})
        if on_batch_started:
            on_batch_started(prefix, list(batch_ids))

        def _fail(reason: str) -> OutlineResult:
            """本批失败：记错误 + 失败批次记录（锚点/前缀/原因），供缓存续跑只补这批。"""
            sub.complete = False
            sub.errors.append(f"batch {bi}: {reason}")
            record = {"anchor_ids": [a["anchor_id"] for a in batch],
                      "prefix": prefix, "batch_index": bi, "reason": reason}
            sub.failed_batches.append(record)
            if on_batch_failed:
                on_batch_failed(record)
            return sub
        # motivation 配额按批内锚点占比折算：分批变小后每批都喊「最多 6 个」
        # 会让一章冒出几十个 motivation 块（意图是每章 ≤6）
        n_motivation = max(1, round(mot_total * len(batch) / max(1, len(anchors))))
        # 分批硬上限进提示词（1.5.0）：先让模型按上限出块，而不是「生成后剪枝」白烧
        # 输出 tokens——实测 6/6 批产出即超限被剪，剪掉的全是已生成的 token
        batch_cap = max(1, math.ceil(size_cap * len(batch) / max(1, len(anchors))))
        prompt = OUTLINE_PROMPT.format(
            doc_kind="论文" if profile == "paper" else "教材",
            book_title=manifest.get("title", ""),
            chapter_part="整篇" if profile == "paper" else f"第 {chapter.get('title', chapter_id)} 章",
            n_motivation=n_motivation,
            n_anchors=len(anchors),
            n_batch=len(batch),
            batch_cap=batch_cap,
            size_note=size_note,
            anchor_summaries="\n".join(anchor_summary_line(a) for a in batch),
            type_guide=type_guide(profile),
            outline_schema=compact_schema(schema),
        ) + "\n" + language_instruction((config.get("generate") or {}).get("output_language"))
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}]
        attempts = 0
        while True:
            attempts += 1
            if is_cancelled and is_cancelled():
                raise TaskCancelled("任务已取消")
            try:
                resp = client.complete("outline", messages, schema=schema,
                                       max_tokens=max_out,
                                       temperature=config.get("generate", {}).get("temperature"),
                                       model=model, doc_id=doc_id, chapter_id=chapter_id,
                                       task_id=task_id, budget=budget)
            except TaskCancelled:
                raise
            except (LLMError, RetryableError, QuotaExhausted) as exc:
                # LLM 调用层失败按批隔离：本批记失败，其余批次照常产出，
                # outline_complete=False 保住旧块不被 5.7 淘汰。
                # 配置类错误（NO_MODEL/PRICE_UNKNOWN/模型不存在）继续冒泡，任务要清晰失败。
                # QuotaExhausted 同样隔离：已产出批次落缓存，下次生成续填。
                return _fail(f"LLM 调用失败: {exc.message}")
            except PriceUnknown as exc:
                if exc.code != "BUDGET_EXCEEDED":
                    raise              # 配置类价格问题 → 任务清晰失败
                # 预算用尽按批隔离（与 fill 一致）：已产出批次保留，不整章陪葬
                return _fail(exc.message)
            if resp.get("finish_reason") == "length":
                # 输出截断不得视作完整提纲：缩批重试（子批前缀带路径后缀，避免 temp_id 撞名）
                if len(batch) > 1:
                    halves = [(0, batch[: len(batch) // 2]), (1, batch[len(batch) // 2:])]
                    for side, half in halves:
                        deep = generate_outline(
                            client, doc_id=doc_id, chapter_id=chapter_id, manifest=manifest,
                            anchors=half, model=model, config=config, budget=budget,
                            task_id=task_id, progress=progress, is_cancelled=is_cancelled,
                            on_items=on_items, on_batch_failed=on_batch_failed,
                            on_batch_started=on_batch_started, on_batch_done=on_batch_done,
                            _prefix=f"{prefix}x{side}")
                        sub.items.extend(deep.items)
                        sub.complete = sub.complete and deep.complete
                        sub.errors.extend(deep.errors)
                        sub.failed_batches.extend(deep.failed_batches)
                        sub.started_batches.extend(deep.started_batches)
                        sub.done_batches.extend(deep.done_batches)
                    return sub
                return _fail("输出截断且无法继续缩批")
            try:
                payload = parse_json_output(resp.get("output") if resp.get("output") is not None
                                            else resp.get("raw_text", ""))
                items = validate_outline_payload(payload, anchor_ids=anchor_ids, profile=profile)
                # 章体量硬上限（前言/小章）：按批占比折算截断，防小章注水
                if size_cap is not None and len(items) > 1:
                    if len(items) > batch_cap:
                        items = _trim_to_cap(items, batch_cap)
                        sub.errors.append(f"batch {bi}: 超出章体量上限，已剪除多余 link/motivation/figure")
                for item in items:
                    item["temp_id"] = f"{prefix}-{item['temp_id']}"
                    item["depends_on"] = [f"{prefix}-{d}" if not d.startswith(prefix) else d
                                          for d in item["depends_on"]]
                sub.items.extend(items)
                if on_items and items:
                    on_items(list(items))
                # 成功完成（字段校验通过，含空 blocks）：按锚点记为已覆盖，
                # 中断快照据此区分「已完成」与「已开始未完成/从未开始」。
                sub.done_batches.append({"prefix": prefix, "anchor_ids": list(batch_ids)})
                if on_batch_done:
                    on_batch_done(prefix, list(batch_ids))
                return sub
            except SchemaViolation as exc:
                if attempts >= 3:
                    return _fail(exc.message)
                messages = messages[:1] + [{"role": "user", "content": prompt}] + [
                    {"role": "assistant", "content": json.dumps(resp.get("output"), ensure_ascii=False)[:4000]},
                    {"role": "user", "content": f"上一次输出校验失败：{exc.message}。请修正后重新输出 JSON。"},
                ]

    total = len(batches)
    workers = max(1, int(gen_cfg.get("concurrency", 2)))
    results: list[OutlineResult | None] = [None] * total
    counter_lock = threading.Lock()
    done = 0

    def _work(bi: int) -> None:
        nonlocal done
        results[bi] = _run_batch(bi, batches[bi])
        with counter_lock:
            done += 1
            n = done
        if progress:
            progress({"stage": "outline", "batch": n, "batches": total})

    if total == 1 or workers <= 1:
        for bi in range(total):
            _work(bi)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, total)) as pool:
            for _ in pool.map(_work, range(total)):
                pass
    for sub in results:  # 按批次序拼接，保证确定性
        if sub is None:
            continue
        result.items.extend(sub.items)
        result.complete = result.complete and sub.complete
        result.errors.extend(sub.errors)
        result.failed_batches.extend(sub.failed_batches)
        result.started_batches.extend(sub.started_batches)
        result.done_batches.extend(sub.done_batches)
    return result
