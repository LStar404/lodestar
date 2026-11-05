"""预算守卫回归：预留必须能结算成实际花费（原实现「预留即已用、永不结算」）。

现场：每次调用按 输入估算 + **整个 max_tokens** 的成本永久计入预算，实际输出常只有
几分之一 → 打开 max_cost_per_chapter 后账被放大数倍，远没花到上限就 BUDGET_EXCEEDED
按块隔离中断（用户会以为"预算太小"，其实是记账放大）。
"""

from __future__ import annotations

import pytest

from src.core.errors import PriceUnknown
from src.core.llm import BudgetGuard, LLMClient
from src.core.model_registry import ModelRegistry
from src.db.runtime import RuntimeDB


class _FakeAdapter:
    def __init__(self):
        self.calls = 0

    def complete(self, model, messages, schema, max_tokens, **kwargs):
        self.calls += 1
        return {"output": {"ok": True}, "raw_text": "", "request_id": "r" + str(self.calls),
                "usage": {"in": 10, "out": 10}}


def test_settle_replaces_reservation_with_actual_cost():
    g = BudgetGuard(1.0)
    r1 = g.reserve(0.6)
    assert r1 == pytest.approx(0.6)
    with pytest.raises(PriceUnknown):          # 在途预留已占满
        g.reserve(0.6)
    g.settle(0.05, r1)                          # 实际只花了 0.05
    assert g._reserved == pytest.approx(0.05)
    r2 = g.reserve(0.6)                         # 结算后又能预留
    g.settle(None, r2)                          # 失败退回
    assert g._reserved == pytest.approx(0.05)


def test_budget_disabled_is_noop():
    g = BudgetGuard(0.0)
    assert g.reserve(999.0) is None
    g.settle(999.0, 999.0)                      # 不抛错、不记账
    assert g._reserved == 0.0


def _client(tmp_path):
    from src.core.paths import Layout

    layout = Layout(tmp_path).ensure_base()
    reg = ModelRegistry(layout)
    data = reg.load()
    prov = data["providers"][0]
    prov.update({"id": "prov", "base_url": "http://x", "api_key": "k",
                 "capabilities": {"json_schema": True, "tools": False, "vision": False},
                 "models": [{"id": "m", "context_window": 8000, "max_output_tokens": 4000,
                             "capabilities": {"json_schema": True},
                             "pricing": {"input_per_million": 1.0, "output_per_million": 1.0}}]})
    data["providers"] = [prov]
    reg.save(data)
    cfg = {"generate": {"models": {"default": "prov/m"}}}
    return LLMClient(reg, RuntimeDB(layout.runtime_db), cfg, overrides={"prov": _FakeAdapter()})


def test_budget_allows_many_calls_when_actual_cost_is_small(tmp_path):
    client = _client(tmp_path)
    budget = BudgetGuard(0.01)          # 每次最坏预留约 0.006，实际只花 0.00002
    for i in range(5):                  # 旧行为：第 2 次就 BUDGET_EXCEEDED
        out = client.complete("fill", [{"role": "user", "content": "hi"}],
                              max_tokens=4000, budget=budget)
        assert out["usage"]["in"] == 10
    assert budget._reserved < 0.001, budget._reserved


def test_zero_usage_is_not_replaced_with_maximum_budget(tmp_path):
    from src.core.llm import DummyProvider

    client = _client(tmp_path)
    client.overrides["prov"] = DummyProvider([
        {"output": "", "usage": {"in": 0, "out": 0, "estimated": False}},
        {"output": "", "usage": {"in": 0, "out": 0, "estimated": False}},
    ])
    budget = BudgetGuard(0.005)
    for _ in range(2):
        result = client.complete("fill", [{"role": "user", "content": "hi"}],
                                 max_tokens=4000, budget=budget)
        assert result["usage"] == {"in": 0, "out": 0, "estimated": False}
    assert budget._reserved == 0
    assert client.runtime.usage_total() == 0


def test_missing_output_usage_is_labelled_estimated(tmp_path):
    from src.core.llm import DummyProvider

    client = _client(tmp_path)
    client.overrides["prov"] = DummyProvider([
        {"output": "text", "usage": {"in": 10, "out": None, "estimated": False}},
    ])
    result = client.complete("fill", [{"role": "user", "content": "hi"}], max_tokens=20)
    assert result["usage"]["estimated"] is True
    assert client.runtime.usage_summary()[0]["estimated_rows"] == 1
