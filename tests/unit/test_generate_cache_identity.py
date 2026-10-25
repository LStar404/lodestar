"""缓存身份回归（Review #9/#7）：改输出语言/提示词因子/解码模式后必须失效重算，不能命中旧缓存。"""

from __future__ import annotations

import json

import pytest

from src.core.llm import DummyProvider, LLMClient
from src.core.model_registry import ModelRegistry
from src.core.store import DocStore
from src.db.runtime import RuntimeDB
from src.generate.engine import _prompt_identity, generate_chapter

BASE_GEN = {"models": {"default": "dummy/fake", "outline": "dummy/fake", "fill": "dummy/fake"},
            "concurrency": 1, "temperature": 0.2, "max_cost_per_chapter": 0.0,
            "max_concurrent_requests": 2, "timeout": 30}


def _cfg(**overrides) -> dict:
    return {"generate": {**BASE_GEN, **overrides}, "cache": {"dir": "cache/gen"}}


def _registry(layout) -> ModelRegistry:
    registry = ModelRegistry(layout)
    registry.upsert_provider({"id": "dummy", "label": "Dummy", "protocol": "openai_compat",
                              "base_url": "http://127.0.0.1:1", "api_key": None,
                              "capabilities": {"json_schema": True, "tools": True,
                                               "vision": False},
                              "models": [{"id": "fake", "context_window": 8192,
                                          "max_output_tokens": 2048,
                                          "capabilities": {"json_schema": True, "tools": True,
                                                           "vision": False},
                                          "pricing": {"input_per_million": 0,
                                                      "output_per_million": 0,
                                                      "currency": "USD"}}]})
    return registry


def _outline(anchor: dict) -> dict:
    return {"blocks": [
        {"temp_id": "t1", "type": "definition", "title": "行列式的定义",
         "basis": "anchored",
         "sources": [{"anchor_id": anchor["anchor_id"],
                      "quote": anchor["content"][:20], "relation": "defines"}],
         "depends_on": [], "order": 1}]}


def _fill(anchor: dict, content: str) -> dict:
    return {"content": content, "content_format": "md_latex", "tags": [],
            "sources_patch": [{"anchor_id": anchor["anchor_id"],
                               "quote": anchor["content"][:20], "relation": "defines"}],
            "basis": "anchored"}


def _client(layout, registry, cfg, responses):
    return LLMClient(registry, RuntimeDB(layout.runtime_db), cfg,
                     overrides={"dummy": DummyProvider(responses)})


def test_output_language_changes_cache_identity(sample_doc):
    """中文生成后再改英文：必须 cache_hit=False、重新调用模型、正文为英文。"""
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchor = store.anchor_rows("ch01")[1]

    cfg_zh = _cfg(output_language="zh")
    client_zh = _client(layout, registry, cfg_zh,
                        [_outline(anchor), _fill(anchor, "行列式的定义正文。")])
    r1 = generate_chapter(doc_id, "ch01", layout=layout, client=client_zh, config=cfg_zh,
                          registry=registry)
    assert r1["status"] == "complete", r1
    assert r1["cache_hit"] is False
    assert store.block_rows("ch01")[0]["content"] == "行列式的定义正文。"
    key_zh = json.loads(layout.cache_file(doc_id, "ch01").read_text(encoding="utf-8"))["cache_key"]

    cfg_en = _cfg(output_language="en")
    client_en = _client(layout, registry, cfg_en,
                        [_outline(anchor), _fill(anchor, "The determinant is a scalar value.")])
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client_en, config=cfg_en,
                          registry=registry)
    assert r2["cache_hit"] is False, "输出语言变了必须让缓存失效"
    assert client_en.stats["calls"] >= 2, "改语言后必须重新调用模型"
    assert store.block_rows("ch01")[0]["content"] == "The determinant is a scalar value."

    # 同语言再次生成：键稳定 → 命中缓存、不再调用模型（防止指纹里混入非确定值）
    client_again = _client(layout, registry, cfg_en, [])
    r3 = generate_chapter(doc_id, "ch01", layout=layout, client=client_again, config=cfg_en,
                          registry=registry)
    key_en = json.loads(layout.cache_file(doc_id, "ch01").read_text(encoding="utf-8"))["cache_key"]
    assert r3["cache_hit"] is True, r3
    assert client_again.stats["calls"] == 0
    assert key_zh != key_en


@pytest.mark.parametrize("field,value", [
    ("output_language", "en"),
    ("fill_max_in", 4000),
    ("outline_max_in", 16000),
    ("outline_batch_out", 4000),
    ("fill_batch", 2),
    ("thinking", "disabled"),
    ("reasoning_effort", "low"),
])
def test_prompt_identity_covers_prompt_and_batching_config(field, value):
    """审计清单：会改变提示词/分批/解码模式的 generate 配置都参与缓存身份。"""
    assert _prompt_identity("textbook", {field: value}) != _prompt_identity("textbook", {})
    assert _prompt_identity("textbook", {}) == _prompt_identity("textbook", {}), "指纹必须稳定"


def test_decode_config_identity_normalization():
    """Review #7：空值/未配置/"default"/非法值不得被当成两种身份；大小写不敏感。"""
    base = _prompt_identity("textbook", {})
    for raw in (None, "", "  ", "default", "DEFAULT", "bogus"):
        assert _prompt_identity("textbook", {"thinking": raw}) == base, raw
        assert _prompt_identity("textbook", {"reasoning_effort": raw}) == base, raw
    assert _prompt_identity("textbook", {"thinking": "default"})["thinking"] == "default"
    assert _prompt_identity(
        "textbook", {"reasoning_effort": "default"})["reasoning_effort"] == "default"
    assert _prompt_identity("textbook", {"thinking": "DISABLED"}) == _prompt_identity(
        "textbook", {"thinking": " disabled "})
    assert _prompt_identity("textbook", {"reasoning_effort": "HIGH"}) == _prompt_identity(
        "textbook", {"reasoning_effort": "high"})
    assert _prompt_identity("textbook", {"thinking": "disabled"}) != base
    assert _prompt_identity("textbook", {"reasoning_effort": "low"}) != base


def test_fill_batch_identity_normalization():
    """P3-G1：fill_batch 归一（默认 1）——未配置/0/1 同一种身份，2 必须不同。"""
    base = _prompt_identity("textbook", {})
    for raw in (None, "", 0, 1, "1"):
        assert _prompt_identity("textbook", {"fill_batch": raw}) == base, raw
    assert _prompt_identity("textbook", {"fill_batch": 2}) != base
    assert _prompt_identity("textbook", {"fill_batch": "2"}) == _prompt_identity(
        "textbook", {"fill_batch": 2})
    assert _prompt_identity("textbook", {"fill_batch": 0})["fill_batch"] == 1


def test_fill_batch_change_invalidates_cache_and_hits_when_stable(sample_doc):
    """P3-G1：fill_batch>1 会换用 FILL_MANY_PROMPT（合批提示词/输出不同），
    不是纯吞吐参数——1→2 必须 cache miss 且重新调用；配置不变仍命中。"""
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchor = store.anchor_rows("ch01")[1]

    cfg1 = _cfg(fill_batch=1)
    client1 = _client(layout, registry, cfg1, [_outline(anchor), _fill(anchor, "逐块版。")])
    r1 = generate_chapter(doc_id, "ch01", layout=layout, client=client1, config=cfg1,
                          registry=registry)
    assert r1["status"] == "complete", r1
    assert r1["cache_hit"] is False

    cfg2 = _cfg(fill_batch=2)
    client2 = _client(layout, registry, cfg2, [_outline(anchor), _fill(anchor, "合批版。")])
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg2,
                          registry=registry)
    assert r2["cache_hit"] is False, "fill_batch 改变会换合批提示词，必须让缓存失效"
    assert client2.stats["calls"] >= 2, "改 fill_batch 后必须重新调用模型"
    assert store.block_rows("ch01")[0]["content"] == "合批版。"

    # 配置不变第三次：命中缓存、0 调用（指纹稳定）
    client3 = _client(layout, registry, cfg2, [])
    r3 = generate_chapter(doc_id, "ch01", layout=layout, client=client3, config=cfg2,
                          registry=registry)
    assert r3["cache_hit"] is True, r3
    assert client3.stats["calls"] == 0


@pytest.mark.parametrize("field,first,second", [
    ("thinking", None, "disabled"),
    ("thinking", "disabled", "enabled"),
    ("reasoning_effort", None, "low"),
    ("reasoning_effort", "low", "high"),
])
def test_decode_config_change_invalidates_cache_and_reruns(sample_doc, field, first, second):
    """Review #7：只改 thinking / reasoning_effort（提示词文本不变）也必须重算。

    用户改完解码/思考模式点「重新生成」，期望拿到新内容；旧实现只把提示词影响因子
    放进缓存身份，这两项被漏掉 → 仍命中旧缓存拿旧内容。配置不变再次生成必须仍命中
    （指纹稳定，不能每次重烧）。
    """
    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchor = store.anchor_rows("ch01")[1]

    cfg1 = _cfg(**{field: first})
    client1 = _client(layout, registry, cfg1, [_outline(anchor), _fill(anchor, "旧解码版。")])
    r1 = generate_chapter(doc_id, "ch01", layout=layout, client=client1, config=cfg1,
                          registry=registry)
    assert r1["status"] == "complete", r1
    assert r1["cache_hit"] is False
    assert store.block_rows("ch01")[0]["content"] == "旧解码版。"

    cfg2 = _cfg(**{field: second})
    client2 = _client(layout, registry, cfg2, [_outline(anchor), _fill(anchor, "新解码版。")])
    r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg2,
                          registry=registry)
    assert r2["cache_hit"] is False, f"改 {field} 必须让缓存失效"
    assert client2.stats["calls"] >= 2, "改解码模式后必须重新调用模型"
    assert store.block_rows("ch01")[0]["content"] == "新解码版。"

    # 配置不变第三次生成：键稳定 → 命中缓存、不调用模型（防止指纹混入非确定值）
    client3 = _client(layout, registry, cfg2, [])
    r3 = generate_chapter(doc_id, "ch01", layout=layout, client=client3, config=cfg2,
                          registry=registry)
    assert r3["cache_hit"] is True, r3
    assert client3.stats["calls"] == 0


def test_custom_block_type_hint_changes_cache_identity(sample_doc):
    """自定义块类型（hint 进提示词、type 枚举进 schema）变化同样必须让缓存失效。"""
    from src.core.block_types import install_types

    layout, doc_id = sample_doc
    registry = _registry(layout)
    store = DocStore(layout, doc_id)
    anchor = store.anchor_rows("ch01")[1]
    cfg = _cfg()
    try:
        install_types([{"id": "insight", "name": "洞见", "hint": "洞见：写出深层理解",
                        "source_policy": "optional"}])
        client1 = _client(layout, registry, cfg, [_outline(anchor), _fill(anchor, "第一版。")])
        r1 = generate_chapter(doc_id, "ch01", layout=layout, client=client1, config=cfg,
                              registry=registry)
        assert r1["cache_hit"] is False

        install_types([{"id": "insight", "name": "洞见", "hint": "洞见：写出可迁移的方法论",
                        "source_policy": "optional"}])
        client2 = _client(layout, registry, cfg, [_outline(anchor), _fill(anchor, "第二版。")])
        r2 = generate_chapter(doc_id, "ch01", layout=layout, client=client2, config=cfg,
                              registry=registry)
        assert r2["cache_hit"] is False, "块类型提示词变了必须重算"
        assert client2.stats["calls"] >= 2
        assert store.block_rows("ch01")[0]["content"] == "第二版。"
    finally:
        install_types(None)
