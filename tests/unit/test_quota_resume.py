"""配额中断回归：半路 token 耗尽 = 暂停落盘，下次生成续填并复用已完成块。"""

from __future__ import annotations

import json

from src.core.errors import LLMError, QuotaExhausted
from src.core.llm import DummyProvider, LLMClient
from src.core.model_registry import ModelRegistry
from src.core.paths import Layout
from src.core.store import DocStore
from src.db.runtime import RuntimeDB
from src.generate.engine import generate_chapter

CFG = {"generate": {"models": {"default": "dummy/fake", "outline": "dummy/fake",
                               "fill": "dummy/fake"},
                    "concurrency": 1, "temperature": 0.2, "max_cost_per_chapter": 0.0,
                    "max_concurrent_requests": 2, "timeout": 30},
       "cache": {"dir": "cache/gen"}}


def _provider_for(layout: Layout, pricing: dict | None = None) -> ModelRegistry:
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


def test_quota_exhaustion_pauses_then_next_run_resumes(sample_doc):
    """半路配额耗尽：报告 paused + 缓存落盘；下次生成只补失败块、复用已完成块。"""
    layout, doc_id = sample_doc
    registry = _provider_for(layout)
    store = DocStore(layout, doc_id)
    anchors = store.anchor_rows("ch01")
    aid = anchors[1]["anchor_id"]
    quote = anchors[1]["content"][:20]
    outline = {"blocks": [
        {"temp_id": "t1", "type": "definition", "title": "定义一", "basis": "anchored",
         "sources": [{"anchor_id": aid, "quote": quote, "relation": "defines"}],
         "depends_on": [], "order": 1},
        {"temp_id": "t2", "type": "motivation", "title": "动机二", "basis": "synthesis",
         "sources": [], "depends_on": [], "order": 2}]}
    fill1 = {"content": "第一块内容。", "content_format": "md_latex", "tags": [],
             "sources_patch": [{"anchor_id": aid, "quote": quote, "relation": "defines"}],
             "basis": "anchored"}
    fill2 = {"content": "第二块内容。", "content_format": "md_latex", "tags": [],
             "sources_patch": [], "basis": "synthesis"}

    # 第一次：t1 成功，t2 配额耗尽
    client1 = LLMClient(registry, RuntimeDB(layout.runtime_db), CFG,
                        overrides={"dummy": DummyProvider([outline, fill1,
                                                           QuotaExhausted("配额已用尽")])})
    r1 = generate_chapter(doc_id, "ch01", layout=layout, client=client1, config=CFG,
                          registry=registry, force=True, use_cache=True)
    assert r1["status"] == "paused", r1
    assert r1.get("resume_hint"), r1
    assert client1.halted_reason, "配额熔断应已置位"
    blocks1 = {b["title"] for b in store.block_rows("ch01")}
    assert "定义一" in blocks1, f"已完成块应已落库: {blocks1}"

    # 第二次：缓存命中，只补 t2（新 Key/充值后的恢复调用）
    client2 = LLMClient(registry, RuntimeDB(layout.runtime_db), CFG,
                        overrides={"dummy": DummyProvider([fill2])})
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=CFG,
                          registry=registry)
    assert r2["cache_hit"] is True, r2
    assert r2["status"] == "complete", r2
    blocks2 = {b["title"]: b for b in store.block_rows("ch01")}
    assert {"定义一", "动机二"} <= set(blocks2), f"续填后两块都应存在: {list(blocks2)}"
    # 续跑只补失败块 t2 一次调用；t1 已完成 → 必须原样复用，不得重烧。
    # （原断言 `len(client2.calls if hasattr(...) else []) >= 0` 恒真，等于没测。）
    provider2 = client2.overrides["dummy"]
    assert len(provider2.calls) == 1, f"只应补填 t2 一块，实际 {len(provider2.calls)} 次调用"
    assert client2.stats["calls"] == 1
    retry_prompt = json.dumps(provider2.calls[0]["messages"],
                              ensure_ascii=False).replace('\\"', '"')
    # 提纲 temp_id t2 进生成后带批前缀 b0-；只允许补填 b0-t2
    assert '"temp_id": "b0-t2"' in retry_prompt, retry_prompt[-600:]
    assert '"temp_id": "b0-t1"' not in retry_prompt, "已完成块 t1 不得重新填充"
    assert blocks2["定义一"]["content"] == "第一块内容。", "t1 正文必须保持首次生成的内容"
    assert blocks2["动机二"]["content"] == "第二块内容。"


def test_quota_circuit_breaker_stops_further_calls(tmp_path):
    """熔断：配额耗尽后本次任务内不再发请求（剩余块快速失败，不打爆 API）。"""
    layout = Layout(tmp_path).ensure_base()
    registry = _provider_for(layout)
    provider = DummyProvider([QuotaExhausted("配额已用尽"), {"output": {"x": 1}}])
    client = LLMClient(registry, RuntimeDB(layout.runtime_db), CFG,
                       overrides={"dummy": provider})
    try:
        client.complete("fill", [{"role": "user", "content": "hi"}], schema=None, max_tokens=8)
        raise AssertionError("应抛 QuotaExhausted")
    except QuotaExhausted:
        pass
    n_calls = len(provider.calls)
    try:
        client.complete("fill", [{"role": "user", "content": "again"}], schema=None, max_tokens=8)
        raise AssertionError("应抛 QuotaExhausted")
    except QuotaExhausted:
        pass
    assert len(provider.calls) == n_calls, "熔断后不得再打上游 API"


def test_resume_reuses_ok_blocks_across_cache_key_change(sample_doc):
    """续跑语义：换模型/换参数（缓存键失配）也必须复用已完成块、只补缺口。
    曾经 115/117 已完成却整章重烧。"""
    layout, doc_id = sample_doc
    registry = _provider_for(layout)
    store = DocStore(layout, doc_id)
    anchors = store.anchor_rows("ch01")
    aid = anchors[1]["anchor_id"]
    quote = anchors[1]["content"][:20]
    outline = {"blocks": [
        {"temp_id": "t1", "type": "definition", "title": "定义一", "basis": "anchored",
         "sources": [{"anchor_id": aid, "quote": quote, "relation": "defines"}],
         "depends_on": [], "order": 1},
        {"temp_id": "t2", "type": "motivation", "title": "动机二", "basis": "synthesis",
         "sources": [], "depends_on": [], "order": 2}]}
    fill1 = {"content": "第一块内容。", "content_format": "md_latex", "tags": [],
             "sources_patch": [{"anchor_id": aid, "quote": quote, "relation": "defines"}],
             "basis": "anchored"}
    fill2 = {"content": "第二块内容。", "content_format": "md_latex", "tags": [],
             "sources_patch": [], "basis": "synthesis"}

    # 第一次：t1 成功、t2 配额中断（断点快照落缓存）
    client1 = LLMClient(registry, RuntimeDB(layout.runtime_db), CFG,
                        overrides={"dummy": DummyProvider([outline, fill1,
                                                           QuotaExhausted("配额已用尽")])})
    r1 = generate_chapter(doc_id, "ch01", layout=layout, client=client1, config=CFG,
                          registry=registry, force=True, use_cache=True)
    assert r1["status"] == "paused", r1

    # 第二次：温度不同 → 缓存键不同（模拟换模型/升提示词），resume=True 仍须复用
    cfg2 = {**CFG, "generate": {**CFG["generate"], "temperature": 0.9}}
    client2 = LLMClient(registry, RuntimeDB(layout.runtime_db), cfg2,
                        overrides={"dummy": DummyProvider([fill2])})
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg2,
                          registry=registry, resume=True)
    assert r2.get("resumed") is True, r2
    blocks = {b["title"]: b for b in store.block_rows("ch01")}
    assert "定义一" in blocks and "动机二" in blocks, list(blocks)
    assert blocks["定义一"]["content"] == "第一块内容。", "已完成块必须原样复用（不得重填）"


def test_cached_failed_refill_obeys_chapter_budget(sample_doc):
    """Review #5：命中缓存后的失败块补填必须受 max_cost_per_chapter 约束。

    复现：限额 0.001、定价每百万 token 输入/输出各 1；补填的最坏预留（≈0.002+）
    已经超限，必须在调用供应商之前被预算守卫拦下——旧实现补填显式 budget=None，
    会真实调用供应商 1 次并记录约 0.004 成本，然后谎报 complete。
    """
    layout, doc_id = sample_doc
    registry = _provider_for(layout, pricing={"input_per_million": 1.0,
                                              "output_per_million": 1.0,
                                              "currency": "USD"})
    store = DocStore(layout, doc_id)
    anchors = store.anchor_rows("ch01")
    aid = anchors[1]["anchor_id"]
    quote = anchors[1]["content"][:20]
    outline = {"blocks": [
        {"temp_id": "t1", "type": "definition", "title": "定义一", "basis": "anchored",
         "sources": [{"anchor_id": aid, "quote": quote, "relation": "defines"}],
         "depends_on": [], "order": 1},
        {"temp_id": "t2", "type": "motivation", "title": "动机二", "basis": "synthesis",
         "sources": [], "depends_on": [], "order": 2}]}
    fill1 = {"content": "第一块内容。", "content_format": "md_latex", "tags": [],
             "sources_patch": [{"anchor_id": aid, "quote": quote, "relation": "defines"}],
             "basis": "anchored"}
    fill2 = {"content": "第二块内容。", "content_format": "md_latex", "tags": [],
             "sources_patch": [], "basis": "synthesis"}

    # 第一次：不限预算；t1 成功、t2 LLM 失败 → 缓存里留下 failed 候选
    cfg_unlimited = {**CFG, "generate": {**CFG["generate"], "max_cost_per_chapter": 0.0}}
    client1 = LLMClient(registry, RuntimeDB(layout.runtime_db), cfg_unlimited,
                        overrides={"dummy": DummyProvider(
                            [outline, fill1, LLMError("LLM 调用被拒绝 402")])})
    r1 = generate_chapter(doc_id, "ch01", layout=layout, client=client1,
                          config=cfg_unlimited, registry=registry, force=True)
    assert r1["status"] == "partial", r1
    cache = json.loads(layout.cache_file(doc_id, "ch01").read_text(encoding="utf-8"))
    assert any(c.get("_status") == "failed" for c in cache["candidates"]), cache["candidates"]

    # 第二次：极小预算续跑 → 补填在供应商调用之前被守卫拦下（调用 0 次）
    cfg_tiny = {**CFG, "generate": {**CFG["generate"], "max_cost_per_chapter": 0.001}}
    dummy2 = DummyProvider([fill2])
    client2 = LLMClient(registry, RuntimeDB(layout.runtime_db), cfg_tiny,
                        overrides={"dummy": dummy2})
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2,
                          config=cfg_tiny, registry=registry)
    assert r2["cache_hit"] is True, r2
    assert dummy2.calls == [], "预算守卫必须先于供应商调用拦截补填（不得真实调用）"
    assert r2["timing"]["llm"]["calls"] == 0, r2["timing"]
    assert r2["status"] != "complete", f"预算拦下的补填不得报 complete: {r2}"
    assert any("成本上限" in str(f.get("reason", "")) for f in r2["failures"]), r2["failures"]


