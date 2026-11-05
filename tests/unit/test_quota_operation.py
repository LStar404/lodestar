"""A new user operation may retry after a top-up without restarting the app."""

from types import SimpleNamespace

from src.api.routes.docs import _build_generate_run
from src.core.errors import QuotaExhausted
from src.core.llm import DummyProvider, LLMClient
from src.core.store import DocStore
from src.db.runtime import RuntimeDB
from tests.unit.test_quota_resume import CFG, _provider_for


def test_api_resume_retries_same_client_after_quota_top_up(sample_doc):
    layout, doc_id = sample_doc
    registry = _provider_for(layout)
    store = DocStore(layout, doc_id)
    outline = {"blocks": [
        {"temp_id": "t1", "type": "motivation", "title": "First",
         "basis": "synthesis", "sources": [], "depends_on": [], "order": 1},
        {"temp_id": "t2", "type": "motivation", "title": "Second",
         "basis": "synthesis", "sources": [], "depends_on": [], "order": 2},
    ]}
    def fill(content):
        return {"content": content, "content_format": "md_latex",
                "tags": [], "sources_patch": [], "basis": "synthesis"}
    provider = DummyProvider([outline, fill("saved first"), QuotaExhausted("quota"),
                              fill("saved second")])
    client = LLMClient(registry, RuntimeDB(layout.runtime_db), CFG,
                       overrides={"dummy": provider})
    state = SimpleNamespace(layout=layout, config=CFG, registry=registry, client=client)
    callbacks = ({}, lambda evt: None, lambda: False, lambda *args: None)
    first = _build_generate_run(state, doc_id, "ch01", None)(*callbacks, task_id="same-task")
    assert first["reports"][0]["status"] == "paused"
    calls_before = len(provider.calls)
    # The resume API intentionally retains its task ID and AppState client.
    second = _build_generate_run(state, doc_id, "ch01", None, resume=True)(
        *callbacks, task_id="same-task")
    assert second["reports"][0]["status"] == "complete"
    assert len(provider.calls) == calls_before + 1
    assert {b["content"] for b in store.block_rows("ch01")} == {"saved first", "saved second"}


def test_quota_circuits_are_isolated_between_operations(workspace):
    import pytest

    from src.core.llm import operation_client

    provider = DummyProvider([QuotaExhausted("quota"), {"output": "ok"}])
    client = LLMClient(_provider_for(workspace), RuntimeDB(workspace.runtime_db), CFG,
                       overrides={"dummy": provider})
    first, second = operation_client(client), operation_client(client)
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(QuotaExhausted):
        first.complete("fill", messages)
    assert second.complete("fill", messages)["output"] == "ok"
    with pytest.raises(QuotaExhausted):
        first.complete("fill", messages)
    assert len(provider.calls) == 2
    assert client.halted_reason is None


def test_queued_calls_stop_after_same_operation_hits_quota(workspace, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import pytest

    from src.core.llm import operation_client

    started, queued, release = threading.Event(), threading.Event(), threading.Event()

    class Provider:
        calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            started.set()
            assert release.wait(5)
            raise QuotaExhausted("quota")

    provider = Provider()
    cfg = {**CFG, "generate": {**CFG["generate"], "max_concurrent_requests": 1}}
    client = LLMClient(_provider_for(workspace), RuntimeDB(workspace.runtime_db), cfg,
                       overrides={"dummy": provider})
    acquire = client._acquire_slot

    def observe_queue():
        if started.is_set():
            queued.set()
        acquire()

    monkeypatch.setattr(client, "_acquire_slot", observe_queue)
    operation = operation_client(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = pool.submit(operation.complete, "fill", [{"role": "user", "content": "first"}])
            assert started.wait(5)
            second = pool.submit(operation.complete, "fill", [{"role": "user", "content": "second"}])
            assert queued.wait(5)
        finally:
            release.set()
        for future in (first, second):
            with pytest.raises(QuotaExhausted):
                future.result(timeout=5)
    assert provider.calls == 1
