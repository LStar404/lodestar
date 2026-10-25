"""部分提纲失败回归（Review #3）：不得谎报 complete，续跑只补缺失批次并最终补齐。

另含第二轮加固（C2）的续跑边界审计：旧缓存、锚点消失、预算共享、缓存回写唯一性、
中断快照与提纲完整性判定。
"""

from __future__ import annotations

import json

import pytest

from src.core.errors import LLMError, TaskCancelled
from src.core.llm import DummyProvider, LLMClient
from src.core.model_registry import ModelRegistry
from src.core.store import DocStore
from src.db.runtime import RuntimeDB
from src.generate.engine import (_missing_batches_after_interrupt,
                                 _outline_batch_records, generate_chapter)

BASE_GEN = {"models": {"default": "dummy/fake", "outline": "dummy/fake", "fill": "dummy/fake"},
            "concurrency": 1, "temperature": 0.2, "max_cost_per_chapter": 0.0,
            "max_concurrent_requests": 2, "timeout": 30, "fill_max_out": 6000}


def _cfg(**overrides) -> dict:
    return {"generate": {**BASE_GEN, **overrides}, "cache": {"dir": "cache/gen"}}


def _registry(layout, pricing: dict | None = None) -> ModelRegistry:
    registry = ModelRegistry(layout)
    registry.upsert_provider({"id": "dummy", "label": "Dummy", "protocol": "openai_compat",
                              "base_url": "http://127.0.0.1:1", "api_key": None,
                              "capabilities": {"json_schema": True, "tools": True,
                                               "vision": False},
                              "models": [{"id": "fake", "context_window": 8192,
                                          "max_output_tokens": 2048,
                                          "capabilities": {"json_schema": True, "tools": True,
                                                           "vision": False},
                                          "pricing": pricing or {"input_per_million": 0,
                                                                 "output_per_million": 0,
                                                                 "currency": "USD"}}]})
    return registry


def _client(layout, registry, cfg, responses) -> LLMClient:
    return LLMClient(registry, RuntimeDB(layout.runtime_db), cfg,
                     overrides={"dummy": DummyProvider(responses)})


def _definition(tid: str, anchor: dict, title: str) -> dict:
    return {"temp_id": tid, "type": "definition", "title": title, "basis": "anchored",
            "sources": [{"anchor_id": anchor["anchor_id"],
                         "quote": anchor["content"][:20], "relation": "defines"}],
            "depends_on": [], "order": 1}


def _fill_for(anchor: dict, content: str) -> dict:
    return {"content": content, "content_format": "md_latex", "tags": [],
            "sources_patch": [{"anchor_id": anchor["anchor_id"],
                               "quote": anchor["content"][:20], "relation": "defines"}],
            "basis": "anchored"}


def _cache_body(layout, doc_id: str) -> dict:
    return json.loads(layout.cache_file(doc_id, "ch01").read_text(encoding="utf-8"))


def test_partial_outline_not_complete_then_resume_rebuilds_missing_batches(sample_doc):
    """10 批提纲只第 1 批成功：首次 partial + 缓存记缺失批；续跑只补这 9 批并完成。"""
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = sorted(store.anchor_rows("ch01"), key=lambda a: (a["page"], a["reading_order"]))
    assert len(anchors) == 10, "样本 ch01 应为 10 个锚点（每锚点一批）"

    # 第一次：outline_max_in=1 → 每锚点一批；第 0 批成功，其余 9 批 LLM 调用失败
    cfg1 = _cfg(outline_max_in=1)
    responses = [{"blocks": [_definition("t1", anchors[0], "第一批定义")]}]
    responses += [LLMError("LLM 调用被拒绝 500（本地替身）")] * 9
    responses += [_fill_for(anchors[0], "第一批内容。")]
    client1 = _client(layout, registry, cfg1, responses)

    r1 = generate_chapter(doc_id, "ch01", layout=layout, client=client1, config=cfg1,
                          registry=registry)
    assert r1["status"] != "complete", f"提纲没补齐不得报 complete: {r1}"
    assert r1["outline_complete"] is False
    assert r1["outline_missing_batches"] == 9, r1
    assert any(f.get("stage") == "outline" for f in r1["failures"]), r1["failures"]
    cache1 = _cache_body(layout, doc_id)
    assert cache1["outline_complete"] is False
    recorded = cache1["meta"]["outline_failed_batches"]
    assert len(recorded) == 9, recorded
    assert sorted(a for rec in recorded for a in rec["anchor_ids"]) == sorted(
        a["anchor_id"] for a in anchors[1:])

    # 第二次续跑：缓存只记了 9 个缺失批 → 只对它们重建提纲；已成功批不重烧
    cfg2 = _cfg(outline_max_in=10 ** 9, outline_batch_out=10 ** 9)
    rebuild = {"blocks": [_definition(str(i + 1), anchors[i + 1], f"补批定义{i + 1}")
                          for i in range(9)]}
    fills = [_fill_for(anchors[i + 1], f"补批内容{i + 1}。") for i in range(9)]
    client2 = _client(layout, registry, cfg2, [rebuild] + fills)
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg2,
                          registry=registry, resume=True)

    assert r2["cache_hit"] is True
    assert r2.get("resumed") is True
    assert r2["outline_complete"] is True, r2
    assert r2["status"] == "complete", r2["failures"]
    assert client2.stats["calls"] == 1 + 9, client2.stats
    assert len(store.block_rows("ch01")) == 10, [b["title"] for b in store.block_rows("ch01")]
    cache2 = _cache_body(layout, doc_id)
    assert cache2["outline_complete"] is True
    assert cache2["meta"]["outline_failed_batches"] == []

    # 只对缺失批调用模型：重建提纲的提示词只含 9 个缺失锚点，不含已成功批的锚点
    prompts = []
    # DummyProvider 记录每次调用的 messages；从中挑出提纲调用（schema 含 blocks）
    provider = client2.overrides["dummy"]
    for call in provider.calls:
        schema = call.get("schema") or {}
        if "blocks" in (schema.get("properties") or {}):
            prompts.append(call["messages"][-1]["content"])
    assert len(prompts) == 1, f"续跑应只重建 1 次提纲（缺失批合并成一批），实际 {len(prompts)}"
    assert anchors[0]["anchor_id"] not in prompts[0], "已成功批的锚点不得重烧"
    for a in anchors[1:]:
        assert a["anchor_id"] in prompts[0], f"缺失锚点 {a['anchor_id']} 未进入补批提示词"


def test_interrupted_outline_snapshot_records_missing_batches(sample_doc, monkeypatch):
    """提纲阶段被取消：断点快照也必须落缺失批次记录，续跑才能补批（Review #3）。"""
    import src.generate.engine as engine_mod

    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = store.anchor_rows("ch01")
    item = _definition("b0-1", anchors[0], "第一批定义")
    item["depends_on"] = []
    failed = {"anchor_ids": [anchors[1]["anchor_id"]], "prefix": "b1", "batch_index": 1,
              "reason": "LLM 调用失败"}

    def fake_outline(client, **kw):
        if kw.get("on_items"):
            kw["on_items"]([dict(item)])
        if kw.get("on_batch_failed"):
            kw["on_batch_failed"](dict(failed))
        raise TaskCancelled("任务已取消")

    monkeypatch.setattr(engine_mod, "generate_outline", fake_outline)
    cfg = _cfg(concurrency=2)
    client = _client(layout, registry, cfg, [])
    with pytest.raises(TaskCancelled):
        generate_chapter(doc_id, "ch01", layout=layout, client=client, config=cfg,
                         registry=registry)
    cache = _cache_body(layout, doc_id)
    assert cache["outline_complete"] is False
    assert any(rec["anchor_ids"] == failed["anchor_ids"]
               for rec in cache["meta"]["outline_failed_batches"]), cache["meta"]


def test_missing_batches_after_interrupt_marks_untried_batches():
    """中断时按分批计划找未尝试批：已出条目/已记失败的批不算缺失。"""
    anchors = [{"anchor_id": f"a{i}", "page": i, "reading_order": 0, "content": "原文"}
               for i in range(4)]
    cfg = {"generate": {"outline_max_in": 1}}
    items = [{"temp_id": "b0-1"}]
    failures = [{"anchor_ids": ["a2"], "prefix": "b2", "batch_index": 2, "reason": "x"}]
    missing = _missing_batches_after_interrupt(anchors, cfg, items, failures)
    assert [rec["prefix"] for rec in missing] == ["b1", "b3"], missing
    assert missing[0]["anchor_ids"] == ["a1"]


# =====================================================================================
# 第二轮加固（C2）：续跑边界审计
# =====================================================================================

def _anchors(store: DocStore) -> list[dict]:
    return sorted(store.anchor_rows("ch01"), key=lambda a: (a["page"], a["reading_order"]))


def _outline_prompts(client: LLMClient) -> list[str]:
    """从 DummyProvider 调用记录里挑出提纲调用（outline schema 含 blocks 属性）。"""
    provider = client.overrides["dummy"]
    out: list[str] = []
    for call in provider.calls:
        schema = call.get("schema") or {}
        if "blocks" in (schema.get("properties") or {}):
            out.append(call["messages"][-1]["content"])
    return out


def _partial_outline_run(layout, doc_id, registry, anchors, fill_response):
    """首次生成：10 批只第 0 批成功，返回 (cfg, report)；fill_response 决定该块填充成败。"""
    cfg = _cfg(outline_max_in=1, concurrency=1)
    responses = [{"blocks": [_definition("t1", anchors[0], "第一批定义")]}]
    responses += [LLMError("LLM 调用被拒绝 500（本地替身）")] * 9
    responses += [fill_response]
    client = _client(layout, registry, cfg, responses)
    report = generate_chapter(doc_id, "ch01", layout=layout, client=client, config=cfg,
                              registry=registry)
    return cfg, report


LEGACY_HINT = ("缓存未记录可重建的缺失提纲批次（旧版本缓存或锚点已变），"
               "无法自动补齐；请重新生成本章")


def test_legacy_cache_without_failed_batches_stays_partial_and_asks_regenerate(sample_doc):
    """C2.1 旧版本缓存（meta 无 outline_failed_batches）+ outline_complete=False：
    普通生成/续跑都不得崩溃、不得谎报 complete，必须给出「请重新生成」提示并保持 partial。
    """
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = _anchors(store)
    cfg, r1 = _partial_outline_run(layout, doc_id, registry, anchors,
                                   _fill_for(anchors[0], "第一批内容。"))
    assert r1["status"] == "partial" and r1["outline_complete"] is False

    # 模拟修复前的缓存结构：meta 只有 prompt_version，没有缺失批次记录
    cache = _cache_body(layout, doc_id)
    cache["meta"] = {"prompt_version": cache.get("prompt_version")}
    layout.cache_file(doc_id, "ch01").write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    # 普通生成（同键命中旧缓存）：0 调用、partial、明确要求重新生成
    client2 = _client(layout, registry, cfg, [])
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg,
                          registry=registry)
    assert r2["cache_hit"] is True
    assert r2["status"] == "partial", r2
    assert r2["outline_complete"] is False
    assert client2.stats["calls"] == 0, "缺失批次记录不可知，不得盲目调用模型"
    assert r2["outline_errors"] == [LEGACY_HINT], r2["outline_errors"]
    assert any(f.get("stage") == "outline" and LEGACY_HINT in str(f.get("reason"))
               for f in r2["failures"]), r2["failures"]
    assert _cache_body(layout, doc_id)["outline_complete"] is False

    # 续跑（键失配走 read_cache_any）：同样 partial + 同一条提示，且不越堆越多
    cfg_resume = {**cfg, "generate": {**cfg["generate"], "temperature": 0.9}}
    client3 = _client(layout, registry, cfg_resume, [])
    r3 = generate_chapter(doc_id, "ch01", layout=layout, client=client3, config=cfg_resume,
                          registry=registry, resume=True)
    assert r3.get("resumed") is True
    assert r3["status"] == "partial" and r3["outline_complete"] is False
    assert r3["outline_errors"] == [LEGACY_HINT], "重复续跑不得堆叠同一提示"
    assert client3.stats["calls"] == 0

    # 无 LLM 客户端时也不得崩溃
    r4 = generate_chapter(doc_id, "ch01", layout=layout, client=None, config=cfg,
                          registry=registry, resume=True)
    assert r4["status"] == "partial" and r4["outline_complete"] is False
    assert r4["outline_errors"] == [LEGACY_HINT]


def test_outline_batch_records_tolerates_malformed_meta():
    """C2.1 坏 meta 一律忽略（字符串 anchor_ids 不得被按字符拆成假锚点）。"""
    assert _outline_batch_records(None) == []
    assert _outline_batch_records("garbage") == []
    assert _outline_batch_records([1, None, "x", {"anchor_ids": []},
                                   {"anchor_ids": "ab"}]) == []
    records = _outline_batch_records([
        {"anchor_ids": ["a", "b"], "prefix": "b1", "batch_index": 1, "reason": "x"},
        {"anchor_ids": ["b", "a"], "prefix": "b1", "batch_index": 1, "reason": "重复"},
        {"anchor_ids": [1, 2], "prefix": "b2"},
    ])
    assert [rec["anchor_ids"] for rec in records] == [["a", "b"], ["1", "2"]], records


def test_resume_rebuilds_only_existing_anchors_and_records_vanished_ones(sample_doc):
    """C2.2 缺失批锚点已不存在（重锚定）：只重建仍存在的锚点；unknown 记入失败原因与
    outline_missing_batches；不崩溃、不无限重试、状态保持 partial。
    """
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = _anchors(store)
    cfg, r1 = _partial_outline_run(layout, doc_id, registry, anchors,
                                   _fill_for(anchors[0], "第一批内容。"))
    assert r1["status"] == "partial"

    # 缺失批次记录齐全但没有客户端：如实提示「未配置 LLM 客户端」（不得谎称缓存没记录）
    r_no_client = generate_chapter(doc_id, "ch01", layout=layout, client=None, config=cfg,
                                   registry=registry, resume=True)
    assert r_no_client["status"] == "partial"
    assert any("未配置 LLM 客户端" in str(e) for e in r_no_client["outline_errors"]), \
        r_no_client["outline_errors"]
    assert r_no_client["outline_missing_batches"] == 9

    ghost = "a_ns_ghost000000000001"
    cache = _cache_body(layout, doc_id)
    cache["meta"]["outline_failed_batches"] = [{
        "anchor_ids": [anchors[1]["anchor_id"], ghost],
        "prefix": "b1", "batch_index": 1, "reason": "LLM 调用失败"}]
    layout.cache_file(doc_id, "ch01").write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    cfg2 = _cfg(outline_max_in=10 ** 9, outline_batch_out=10 ** 9, concurrency=1)
    rebuild = {"blocks": [_definition("t1", anchors[1], "补批定义")]}
    client2 = _client(layout, registry, cfg2,
                      [rebuild, _fill_for(anchors[1], "补批内容。")])
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg2,
                          registry=registry, resume=True)
    assert r2["cache_hit"] is True and r2.get("resumed") is True, r2
    assert r2["status"] == "partial", r2
    assert r2["outline_complete"] is False
    assert r2["outline_missing_batches"] == 1, r2
    assert any("不存在" in str(e) for e in r2["outline_errors"]), r2["outline_errors"]
    prompts = _outline_prompts(client2)
    assert len(prompts) == 1, "只重建仍存在的锚点（一次调用）"
    assert anchors[1]["anchor_id"] in prompts[0]
    assert ghost not in prompts[0], "已消失的锚点不得进入补批提示词"
    cache2 = _cache_body(layout, doc_id)
    ghost_ids = {aid for rec in cache2["meta"]["outline_failed_batches"]
                 for aid in rec["anchor_ids"]}
    assert ghost in ghost_ids, "unknown 锚点必须留在缓存里，不得被静默丢弃"
    assert {b["title"] for b in store.block_rows("ch01")} == {"第一批定义", "补批定义"}

    # 再次续跑：唯一缺失锚点已不存在 → 0 调用、partial、明确提示（不无限重试）
    client3 = _client(layout, registry, cfg2, [])
    r3 = generate_chapter(doc_id, "ch01", layout=layout, client=client3, config=cfg2,
                          registry=registry, resume=True)
    assert client3.stats["calls"] == 0, "锚点已不存在时不得再调模型"
    assert r3["status"] == "partial" and r3["outline_complete"] is False
    assert r3["outline_missing_batches"] == 1
    assert any("不存在" in str(e) and "请重新生成本章" in str(e)
               for e in r3["outline_errors"]), r3["outline_errors"]


def test_missing_outline_retry_and_cached_fill_share_one_budget(sample_doc):
    """C2.3 缺失提纲批 + 缓存失败块同时存在：两者共用本次运行的 BudgetGuard。

    极小预算（非零、定价已知）下，补填与补批都必须在供应商调用之前被拦下（0 次调用），
    不得有任何路径绕过 max_cost_per_chapter。
    """
    layout, doc_id = sample_doc
    registry = _registry(layout, pricing={"input_per_million": 1.0,
                                          "output_per_million": 1.0,
                                          "currency": "USD"})
    store = DocStore(layout, doc_id)
    anchors = _anchors(store)
    cfg1, r1 = _partial_outline_run(layout, doc_id, registry, anchors,
                                    LLMError("LLM 调用被拒绝 402"))
    assert r1["status"] == "partial", r1
    cache = _cache_body(layout, doc_id)
    assert any(c.get("_status") == "failed" for c in cache["candidates"])
    assert len(cache["meta"]["outline_failed_batches"]) == 9

    cfg_tiny = _cfg(outline_max_in=1, concurrency=1, max_cost_per_chapter=1e-6)
    dummy2 = DummyProvider([])
    client2 = LLMClient(registry, RuntimeDB(layout.runtime_db), cfg_tiny,
                        overrides={"dummy": dummy2})
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2,
                          config=cfg_tiny, registry=registry)
    assert r2["cache_hit"] is True
    assert dummy2.calls == [], "极小预算下不得绕过 guard 调供应商（补填/补批均然）"
    assert r2["timing"]["llm"]["calls"] == 0
    assert r2["status"] != "complete", r2
    assert r2["outline_complete"] is False
    assert any("成本上限" in str(f.get("reason", "")) for f in r2["failures"]), r2["failures"]
    assert any("成本上限" in str(e) for e in r2["outline_errors"]), r2["outline_errors"]


def test_resume_rebuild_keeps_candidates_temp_ids_and_blocks_unique(sample_doc):
    """C2.4 续跑补齐提纲后回写的缓存：candidates 数量正确、temp_id 唯一、再合并后
    块数/身份唯一；二次续跑（缓存已有 r- 前缀）的 temp_id 重映射仍唯一。
    """
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = _anchors(store)

    # 第一次：第 0 批成功，其余 9 批失败
    cfg1, r1 = _partial_outline_run(layout, doc_id, registry, anchors,
                                    _fill_for(anchors[0], "第一批内容。"))
    assert r1["status"] == "partial"

    # 第二次续跑：9 个缺失批各一次调用；第 1 批成功、其余 8 批失败
    cfg2 = _cfg(outline_max_in=1, concurrency=1, temperature=0.5)
    responses2 = [{"blocks": [_definition("t1", anchors[1], "第二批定义")]}]
    responses2 += [LLMError("LLM 调用被拒绝 500（本地替身）")] * 8
    responses2 += [_fill_for(anchors[1], "第二批内容。")]
    client2 = _client(layout, registry, cfg2, responses2)
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg2,
                          registry=registry, resume=True)
    assert r2["status"] == "partial" and r2["outline_complete"] is False, r2
    cache2 = _cache_body(layout, doc_id)
    tids2 = [c["temp_id"] for c in cache2["candidates"]]
    assert len(tids2) == 2, tids2
    assert len(set(tids2)) == 2, tids2
    assert any(t.startswith("r-") for t in tids2), "补批条目必须换到本次运行独占命名空间"
    assert len(store.block_rows("ch01")) == 2
    prompts2 = _outline_prompts(client2)
    assert len(prompts2) == 9, "9 个缺失批各一次调用"
    joined2 = "\n".join(prompts2)
    assert anchors[0]["anchor_id"] not in joined2, "已成功批不得重烧"

    # 第三次续跑：缓存里已有 r- 前缀；补齐剩余 8 批后 temp_id 仍须唯一
    cfg3 = _cfg(outline_max_in=10 ** 9, outline_batch_out=10 ** 9, concurrency=1)
    rebuild3 = {"blocks": [_definition(str(i + 1), anchors[i + 2], f"第三批定义{i + 2}")
                           for i in range(8)]}
    responses3 = [rebuild3] + [_fill_for(anchors[i + 2], f"第三批内容{i + 2}。")
                              for i in range(8)]
    client3 = _client(layout, registry, cfg3, responses3)
    r3 = generate_chapter(doc_id, "ch01", layout=layout, client=client3, config=cfg3,
                          registry=registry, resume=True)
    assert r3["outline_complete"] is True, r3["failures"]
    assert r3["status"] == "complete", r3["failures"]
    cache3 = _cache_body(layout, doc_id)
    tids3 = [c["temp_id"] for c in cache3["candidates"]]
    assert len(tids3) == 10, tids3
    assert len(set(tids3)) == 10, f"二次续跑 temp_id 撞名: {tids3}"
    assert sum(t.startswith("rr-") for t in tids3) == 8, tids3
    assert cache3["outline_complete"] is True
    assert cache3["meta"]["outline_failed_batches"] == []

    # 真实 merge 提交后按 DocStore 断言：行数正确、id/身份键唯一、每个锚点恰好一块
    rows = store.block_rows("ch01")
    assert len(rows) == 10, [(b["id"], b["title"]) for b in rows]
    assert len({b["id"] for b in rows}) == 10, "块 id 不得重复"
    keys = [(b.get("type"),
             tuple(sorted(s["anchor_id"] for s in b.get("sources", [])))) for b in rows]
    assert len(set(keys)) == 10, f"身份键重复（同一来源出了多块）: {keys}"
    covered = sorted(s["anchor_id"] for b in rows for s in b.get("sources", []))
    assert covered == sorted(a["anchor_id"] for a in anchors)

    # 已完整的缓存再续跑一次：0 调用、不新增/重复块
    client4 = _client(layout, registry, cfg3, [])
    r4 = generate_chapter(doc_id, "ch01", layout=layout, client=client4, config=cfg3,
                          registry=registry, resume=True)
    assert client4.stats["calls"] == 0, "提纲已完整时续跑不得再调模型"
    assert r4["status"] == "complete"
    assert len(store.block_rows("ch01")) == 10, "重复续跑不得产生重复块"


def test_empty_rebuild_result_keeps_partial_and_preserves_old_blocks(sample_doc):
    """C2.4/C2.6 补批返回空提纲（blocks: []）不得被当成「已补齐」。

    修复前：retry.items 为空且无失败记录 → outline_complete 被置真；若缓存候选也为空，
    下游 merge 会按「完整提纲明确排除」把整章旧块软删，并谎报 complete。
    修复后：记为缺失、保持 partial、旧块一块不动。
    """
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = _anchors(store)

    # 先做一次完整生成，留下一个旧块（后续误判的受害者）
    cfg_ok = _cfg(concurrency=1)
    client0 = _client(layout, registry, cfg_ok,
                      [{"blocks": [_definition("t1", anchors[0], "首批定义")]},
                       _fill_for(anchors[0], "首批内容。")])
    r0 = generate_chapter(doc_id, "ch01", layout=layout, client=client0, config=cfg_ok,
                          registry=registry)
    assert r0["status"] == "complete"
    assert len(store.block_rows("ch01")) == 1

    # 换配置重跑，提纲阶段中断：只有失败批记录、没有已出条目 → 缓存 candidates=[]
    cfg_b = _cfg(outline_max_in=1, concurrency=1)
    client1 = _client(layout, registry, cfg_b,
                      [LLMError("LLM 调用被拒绝 500（本地替身）"), TaskCancelled("任务已取消")])
    with pytest.raises(TaskCancelled):
        generate_chapter(doc_id, "ch01", layout=layout, client=client1, config=cfg_b,
                         registry=registry)
    cache = _cache_body(layout, doc_id)
    assert cache["candidates"] == []
    assert cache["outline_complete"] is False

    # 续跑：模型对缺失的 10 个锚点返回空提纲 → 不得报 complete、不得删旧块
    cfg_c = _cfg(outline_max_in=10 ** 9, outline_batch_out=10 ** 9, concurrency=1)
    client2 = _client(layout, registry, cfg_c, [{"blocks": []}])
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg_c,
                          registry=registry, resume=True)
    assert r2["status"] == "partial", r2
    assert r2["outline_complete"] is False
    assert r2["outline_missing_batches"] == 1, r2
    assert any("空提纲" in str(e) for e in r2["outline_errors"]), r2["outline_errors"]
    rows = store.block_rows("ch01")
    assert len(rows) == 1 and rows[0]["title"] == "首批定义", "旧块不得因空补批被淘汰"
    cache2 = _cache_body(layout, doc_id)
    assert cache2["outline_complete"] is False
    recorded = {aid for rec in cache2["meta"]["outline_failed_batches"]
                for aid in rec["anchor_ids"]}
    assert recorded == {a["anchor_id"] for a in anchors}, "缺失锚点必须全部留档以便重试"


def test_corrupt_complete_cache_without_candidates_does_not_delete_blocks(sample_doc):
    """C2.6 空候选却声称完整的缓存：不得按「完整提纲」淘汰旧块、不得谎报 complete。"""
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = _anchors(store)

    cfg = _cfg(concurrency=1)
    client0 = _client(layout, registry, cfg,
                      [{"blocks": [_definition("t1", anchors[0], "首批定义")]},
                       _fill_for(anchors[0], "首批内容。")])
    r0 = generate_chapter(doc_id, "ch01", layout=layout, client=client0, config=cfg,
                          registry=registry)
    assert r0["status"] == "complete" and len(store.block_rows("ch01")) == 1

    cache = _cache_body(layout, doc_id)
    cache["candidates"] = []
    layout.cache_file(doc_id, "ch01").write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    client1 = _client(layout, registry, cfg, [])
    r1 = generate_chapter(doc_id, "ch01", layout=layout, client=client1, config=cfg,
                          registry=registry)
    assert r1["cache_hit"] is True
    assert client1.stats["calls"] == 0
    assert r1["status"] == "partial", r1
    assert r1["outline_complete"] is False
    assert any("没有可提交的候选块" in str(e) for e in r1["outline_errors"]), r1["outline_errors"]
    assert len(store.block_rows("ch01")) == 1, "不可信缓存不得触发旧块淘汰"


def test_interrupt_after_outline_returns_snapshots_complete_outline(sample_doc, monkeypatch):
    """C2.5 提纲已返回后填充中断（concurrency=1，非流水线）：快照必须按 outline.complete
    写 outline_complete，并保住全部完成批的条目；续跑只补填充、不重跑提纲。
    """
    import src.generate.engine as engine_mod
    from src.generate.outline import OutlineResult

    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = _anchors(store)
    items = [_definition("b0-1", anchors[0], "块一"),
             _definition("b0-2", anchors[1], "块二")]

    def fake_outline(client, **kw):
        return OutlineResult(items=[dict(it) for it in items], complete=True,
                             failed_batches=[])

    monkeypatch.setattr(engine_mod, "generate_outline", fake_outline)
    cfg = _cfg(concurrency=1)
    client1 = _client(layout, registry, cfg, [TaskCancelled("任务已取消")])
    with pytest.raises(TaskCancelled):
        generate_chapter(doc_id, "ch01", layout=layout, client=client1, config=cfg,
                         registry=registry)

    cache = _cache_body(layout, doc_id)
    assert cache["outline_complete"] is True, "提纲已完整返回，快照不得把它记成缺失"
    assert cache["meta"]["outline_failed_batches"] == []
    cands = cache["candidates"]
    assert {c["temp_id"] for c in cands} == {"b0-1", "b0-2"}, \
        "非流水线模式也要保住已完成批的条目（否则续跑会漏块）"
    assert all(c.get("_status") == "failed" for c in cands), "未填充的块应可续填"

    def no_outline(*a, **k):        # 续跑若重跑提纲即失败
        raise AssertionError("提纲已完整，续跑不得重跑提纲")

    monkeypatch.setattr(engine_mod, "generate_outline", no_outline)
    client2 = _client(layout, registry, cfg,
                      [_fill_for(anchors[0], "块一内容。"), _fill_for(anchors[1], "块二内容。")])
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg,
                          registry=registry, resume=True)
    assert r2["cache_hit"] is True
    assert r2["outline_complete"] is True and r2["status"] == "complete", r2["failures"]
    assert client2.stats["calls"] == 2, "只需补两块填充"
    rows = store.block_rows("ch01")
    assert {b["title"]: b["content"] for b in rows} == {
        "块一": "块一内容。", "块二": "块二内容。"}


def test_interrupted_outline_snapshot_resumes_consistently(sample_doc, monkeypatch):
    """C2.5 提纲未返回时中断：按分批计划记录未尝试批；两种快照的续跑行为一致
    （这里续跑后应补齐整章，且已完成批不重烧）。
    """
    import src.generate.engine as engine_mod

    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = _anchors(store)
    real_outline = engine_mod.generate_outline
    item0 = _definition("b0-1", anchors[0], "批零定义")
    item1 = _definition("b1-1", anchors[1], "批一定义")
    failure = {"anchor_ids": [anchors[2]["anchor_id"]], "prefix": "b2", "batch_index": 2,
               "reason": "LLM 调用失败"}

    def fake_outline(client, **kw):
        if kw.get("on_items"):
            kw["on_items"]([dict(item0)])
            kw["on_items"]([dict(item1)])
        if kw.get("on_batch_failed"):
            kw["on_batch_failed"](dict(failure))
        raise TaskCancelled("任务已取消")

    monkeypatch.setattr(engine_mod, "generate_outline", fake_outline)
    cfg1 = _cfg(outline_max_in=1, concurrency=2)      # 流水线模式（提速路径）
    client1 = _client(layout, registry, cfg1, [])
    with pytest.raises(TaskCancelled):
        generate_chapter(doc_id, "ch01", layout=layout, client=client1, config=cfg1,
                         registry=registry)
    cache = _cache_body(layout, doc_id)
    assert cache["outline_complete"] is False
    recorded = cache["meta"]["outline_failed_batches"]
    recorded_ids = {aid for rec in recorded for aid in rec["anchor_ids"]}
    assert recorded_ids == {a["anchor_id"] for a in anchors[2:]}, \
        f"未尝试批必须完整记录: {recorded_ids}"
    assert len(recorded) == 8, "b2 失败 + b3..b9 未尝试 = 8 批"
    assert {c["temp_id"] for c in cache["candidates"]} == {"b0-1", "b1-1"}

    monkeypatch.setattr(engine_mod, "generate_outline", real_outline)
    cfg2 = _cfg(outline_max_in=10 ** 9, outline_batch_out=10 ** 9, concurrency=1)
    rebuild = {"blocks": [_definition(str(i + 1), anchors[i + 2], f"补批定义{i + 2}")
                          for i in range(8)]}
    responses = [_fill_for(anchors[0], "批零内容。"), _fill_for(anchors[1], "批一内容。")]
    responses += [rebuild]
    responses += [_fill_for(anchors[i + 2], f"补批内容{i + 2}。") for i in range(8)]
    client2 = _client(layout, registry, cfg2, responses)
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg2,
                          registry=registry, resume=True)
    assert r2["outline_complete"] is True and r2["status"] == "complete", r2["failures"]
    assert len(store.block_rows("ch01")) == 10
    prompts = _outline_prompts(client2)
    assert len(prompts) == 1, "续跑只补 1 批（锚点 2..9 一次调用）"
    assert anchors[0]["anchor_id"] not in prompts[0]
    assert anchors[1]["anchor_id"] not in prompts[0], "已完成批不得重烧"
    for a in anchors[2:]:
        assert a["anchor_id"] in prompts[0]


# =====================================================================================
# 第四轮（P2-G1）：截断缩批递归中断 → 缺失批记录 / 死端兜底
# =====================================================================================

class _TruncatingOutlineClient:
    """提纲桩：>=3 个锚点的批返回截断；含 cancel 锚点的批抛 TaskCancelled；其余成功。

    填充调用一律取消：本用例只关心「提纲中断时的快照与缺失批记录」，填充由续跑阶段
    的真实 DummyProvider 完成（后台在途填充异常不会影响快照）。
    """

    def __init__(self, anchors: list[dict], cancel_ids: set[str]):
        self.anchor_by_id = {a["anchor_id"]: a for a in anchors}
        self.anchor_ids = [a["anchor_id"] for a in anchors]
        self.cancel_ids = set(cancel_ids)
        self.calls: list[str] = []
        self.stats = {"calls": 0, "retries": 0, "failures": 0, "call_seconds": 0.0,
                      "wait_seconds": 0.0, "tokens_in": 0, "tokens_out": 0}

    def complete(self, task, messages, **kwargs):
        self.calls.append(task)
        if task != "outline":
            raise TaskCancelled("测试：填充阶段不在本用例范围")
        prompt = messages[-1]["content"]
        present = [aid for aid in self.anchor_ids if aid in prompt]
        if len(present) >= 3:
            return {"output": None, "raw_text": '{"blocks": [', "tool_calls": [],
                    "finish_reason": "length", "usage": {"in": 1, "out": 1},
                    "request_id": "truncate"}
        if self.cancel_ids & set(present):
            raise TaskCancelled("任务已取消")
        blocks = []
        for i, aid in enumerate(present):
            anchor = self.anchor_by_id[aid]
            blocks.append({
                "temp_id": str(i + 1), "type": "definition",
                "title": f"块{aid[-4:]}", "basis": "anchored",
                "sources": [{"anchor_id": aid, "quote": anchor["content"][:20],
                             "relation": "defines"}],
                "depends_on": [], "order": i + 1})
        return {"output": {"blocks": blocks}, "raw_text": "{}", "tool_calls": [],
                "finish_reason": "stop", "usage": {"in": 1, "out": 1}, "request_id": "ok"}


def _synthetic_anchors(n: int) -> list[dict]:
    return [{"anchor_id": f"a_ns_{i:016d}", "chapter_id": "ch01", "page": i + 1,
             "reading_order": 0, "content": f"第 {i} 段原文内容，用于分批与引文校验。",
             "content_type": "paragraph"} for i in range(n)]


def test_missing_batches_counts_uncovered_anchors_after_truncation_cancel():
    """P2：父批截断后只完成一部分子批就取消——未覆盖的锚点必须记缺失。

    旧实现只看 temp_id 前缀：见过 b0x… 就把父批 b0 当已触及 → failed_batches 为空 →
    续跑 0 调用、永远 partial 死端。新实现按「成功批(done)+失败批」覆盖的锚点补缺。
    """
    anchors = _synthetic_anchors(3)
    ids = [a["anchor_id"] for a in anchors]
    cfg = {"generate": {"outline_max_in": 10 ** 9, "outline_batch_out": 10 ** 9,
                        "outline_max_out": 10 ** 9, "concurrency": 1}}
    started = [{"prefix": "b0", "anchor_ids": ids},
               {"prefix": "b0x00", "anchor_ids": [ids[0]]}]
    # b0x00 成功（done）→ 未覆盖的 ids[1], ids[2] 必须补记（b0x10 从未开始）
    missing = _missing_batches_after_interrupt(
        anchors, cfg, [{"temp_id": "b0x00-1"}], [], started,
        done=[{"prefix": "b0x00", "anchor_ids": [ids[0]]}])
    assert [rec["anchor_ids"] for rec in missing] == [[ids[1], ids[2]]], missing

    # 部分失败的批同样算已覆盖，只补剩余锚点
    failures = [{"anchor_ids": [ids[1]], "prefix": "b0x10", "batch_index": 0,
                 "reason": "LLM 调用失败"}]
    missing2 = _missing_batches_after_interrupt(
        anchors, cfg, [], failures, started,
        done=[{"prefix": "b0x00", "anchor_ids": [ids[0]]}])
    assert [rec["anchor_ids"] for rec in missing2] == [[ids[2]]], missing2

    # 全部锚点都已被成功批覆盖 → 不误记缺失（截断递归全部成功）
    missing3 = _missing_batches_after_interrupt(
        anchors, cfg, [], [], started,
        done=[{"prefix": "b0x00", "anchor_ids": [ids[0]]},
              {"prefix": "b0x10", "anchor_ids": [ids[1], ids[2]]}])
    assert missing3 == []

    # 兼容路径（无 done 回调）：用已出条目批次前缀近似覆盖
    missing4 = _missing_batches_after_interrupt(
        anchors, cfg, [{"temp_id": "b0x00-1"}], [], started)
    assert [rec["anchor_ids"] for rec in missing4] == [[ids[1], ids[2]]], missing4


def test_outline_batch_started_and_done_fire_for_recursive_sub_batches():
    """P2：on_batch_started/on_batch_done 必须覆盖截断缩批的递归子批。"""
    from src.generate.outline import generate_outline

    anchors = _synthetic_anchors(3)
    ids = [a["anchor_id"] for a in anchors]
    stub = _TruncatingOutlineClient(anchors, {ids[1]})
    started: list[tuple[str, tuple[str, ...]]] = []
    done: list[tuple[str, tuple[str, ...]]] = []
    cfg = {"generate": {"outline_max_in": 10 ** 9, "outline_batch_out": 10 ** 9,
                        "outline_max_out": 10 ** 9, "concurrency": 1}}
    manifest = {"profile": "textbook", "title": "书",
                "chapters": [{"chapter_id": "ch01", "title": "一章"}]}
    with pytest.raises(TaskCancelled):
        generate_outline(stub, doc_id="doc-x", chapter_id="ch01", manifest=manifest,
                         anchors=anchors, model=None, config=cfg,
                         on_batch_started=lambda prefix, batch_ids: started.append(
                             (prefix, tuple(batch_ids))),
                         on_batch_done=lambda prefix, batch_ids: done.append(
                             (prefix, tuple(batch_ids))))
    # 父批 b0 截断 → 两个递归子批；实际前缀带路径后缀（b0x00 / b0x10）
    assert started[0] == ("b0", tuple(ids))
    assert sorted(batch_ids for _, batch_ids in started[1:]) == [
        (ids[0],), (ids[1], ids[2])], started
    assert len(started) == 3
    # 只有成功的那个子批记为 done；取消的批不得记 done
    assert [batch_ids for _, batch_ids in done] == [(ids[0],)], done


@pytest.mark.parametrize("concurrency", [1, 2])
def test_truncated_outline_cancel_records_unfinished_sub_batches_and_resumes(
        sample_doc, concurrency):
    """P2 主场景：b0 截断 → 某子批成功出条目 → 另一子批进行中取消。

    缓存 failed_batches 必须包含未完成的子批锚点（含从未开始的那半批），随后续跑
    真正调用提纲把章补完，而不是 0 调用死端。concurrency=1（非流水线）与 2（流水线）
    都要成立：前者 on_items 不提前填充，但已出批条目也必须进快照。
    """
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = _anchors(store)
    assert len(anchors) == 10

    cfg = _cfg(outline_max_in=10 ** 9, outline_batch_out=10 ** 9,
               outline_max_out=10 ** 9, concurrency=concurrency)
    stub = _TruncatingOutlineClient(anchors, {anchors[3]["anchor_id"]})
    with pytest.raises(TaskCancelled):
        generate_chapter(doc_id, "ch01", layout=layout, client=stub, config=cfg,
                         registry=registry)

    cache = _cache_body(layout, doc_id)
    assert cache["outline_complete"] is False
    produced_ids = {s["anchor_id"] for c in cache["candidates"]
                    for s in c.get("sources", [])}
    assert produced_ids, "截断前已成功的子批条目应已进入快照"
    recorded = {aid for rec in cache["meta"]["outline_failed_batches"]
                for aid in rec["anchor_ids"]}
    assert recorded, "递归缩批中断必须记录缺失批（不得为空 → 死端）"
    assert not (produced_ids & recorded), "已有条目的锚点不得同时记缺失"
    assert recorded == {a["anchor_id"] for a in anchors} - produced_ids
    assert cache["meta"]["outline_started_batches"] >= 3
    assert cache["meta"].get("outline_unresumable") is not True

    # 续跑：先补失败候选的填充，再只对缺失锚点重建提纲并填充 → 补完整章
    aid_by_id = {a["anchor_id"]: a for a in anchors}
    recorded_order = [a for a in anchors if a["anchor_id"] in recorded]
    cfg2 = _cfg(outline_max_in=10 ** 9, outline_batch_out=10 ** 9,
                outline_max_out=10 ** 9, concurrency=1, temperature=0.5)
    responses = []
    for cand in cache["candidates"]:
        src_id = cand["sources"][0]["anchor_id"]
        responses.append(_fill_for(aid_by_id[src_id], f"补填{cand['temp_id']}。"))
    responses.append({"blocks": [_definition(str(i + 1), a, f"重建定义{i}")
                                 for i, a in enumerate(recorded_order)]})
    responses += [_fill_for(a, f"重建内容{i}。") for i, a in enumerate(recorded_order)]
    client2 = _client(layout, registry, cfg2, responses)
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg2,
                          registry=registry, resume=True)
    assert r2["outline_complete"] is True, r2["failures"]
    assert r2["status"] == "complete", r2["failures"]
    assert r2["outline_batches_started"] >= 3, "可观测字段：本应是多批缩批中断后补批"
    assert len(store.block_rows("ch01")) == len(anchors)
    prompts = _outline_prompts(client2)
    assert len(prompts) == 1, "缺失批合并成一次补批调用"
    for aid in produced_ids:
        assert aid not in prompts[0], "已出条目的子批不得重烧"
    for a in recorded_order:
        assert a["anchor_id"] in prompts[0], "缺失子批锚点必须进入补批提示词"
    cache2 = _cache_body(layout, doc_id)
    assert cache2["outline_complete"] is True
    assert cache2["meta"]["outline_failed_batches"] == []


def test_stale_dead_end_cache_triggers_full_regeneration(sample_doc):
    """P2 兜底：新格式缓存「提纲不完整但缺失批记录为空」= 历史死端产物，必须丢弃缓存
    真正全量重生成（而不是只提示「旧版本缓存」+ 0 调用），且无 client 时保持旧提示。
    """
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchors = _anchors(store)
    cfg, r1 = _partial_outline_run(layout, doc_id, registry, anchors,
                                   _fill_for(anchors[0], "第一批内容。"))
    assert r1["status"] == "partial"

    # 构造历史死端缓存：outline_complete=False 且缺失批记录为空（父批被误判已触及）
    cache = _cache_body(layout, doc_id)
    cache["meta"]["outline_failed_batches"] = []
    cache["meta"].pop("outline_unresumable", None)
    layout.cache_file(doc_id, "ch01").write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    # 无 LLM 客户端：无法重生成 → 保持旧提示 + partial，不崩溃
    r_none = generate_chapter(doc_id, "ch01", layout=layout, client=None, config=cfg,
                              registry=registry, resume=True)
    assert r_none["status"] == "partial" and r_none["outline_complete"] is False
    assert any("请重新生成本章" in str(e) for e in r_none["outline_errors"])

    # 重写回「无标记死端缓存」，有 client 时必须真正全量重生成
    cache = _cache_body(layout, doc_id)
    cache["meta"]["outline_failed_batches"] = []
    cache["meta"].pop("outline_unresumable", None)
    cache["outline_complete"] = False
    layout.cache_file(doc_id, "ch01").write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    responses = [{"blocks": [_definition("t1", a, f"重生成定义{i}")]}
                 for i, a in enumerate(anchors)]
    responses += [_fill_for(a, f"重生成内容{i}。") for i, a in enumerate(anchors)]
    client2 = _client(layout, registry, cfg, responses)
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg,
                          registry=registry)
    assert r2["cache_hit"] is False, r2
    assert r2.get("cache_discarded") == "incomplete_cache_without_failed_batches"
    assert r2["outline_complete"] is True
    assert r2["status"] == "complete", r2["failures"]
    assert len(_outline_prompts(client2)) == 10, "必须真正重跑提纲（不是 0 调用死端）"
    assert len(store.block_rows("ch01")) == 10
