"""Rejected or unwritable settings must not change the running application."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.api.routes.system import put_settings
from src.api.state import AppState
from src.core.block_types import install_types, type_enum
from src.core.config import save_config
from src.core.errors import InputError


@pytest.fixture
def settings_state(workspace):
    state = AppState(workspace.root)
    save_config(state.root, state.config)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(app_state=state)))
    yield state, request
    install_types([])


@pytest.mark.parametrize("patch", [
    {"parser": {"default": "invalid"}, "generate": {"temperature": 0.9}},
    {"generate": {"timeout": "bad"}},
    {"generate": {"max_concurrent_requests": -1}},
    {"generate": {"concurrency": 1.5}},
    {"generate": {"max_cost_per_chapter": float("nan")}},
    {"generate": {"output_language": "invalid"}},
    {"generate": {"thinking": 123}},
    {"export": {"encoding": "invalid"}},
    {"parser": []},
])
def test_rejected_settings_leave_memory_and_disk_unchanged(settings_state, patch):
    state, request = settings_state
    before = deepcopy(state.config)
    path = state.root / "config.yaml"
    raw_before = path.read_bytes()
    with pytest.raises(InputError):
        put_settings(request, patch)
    assert state.config == before
    assert state.client.config == before
    assert path.read_bytes() == raw_before


def test_failed_save_does_not_install_new_types(settings_state, monkeypatch):
    state, request = settings_state
    before = deepcopy(state.config)
    raw_before = (state.root / "config.yaml").read_bytes()
    types_before = type_enum()

    def fail_save(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr("src.core.config.save_config", fail_save)
    with pytest.raises(OSError):
        put_settings(request, {"generate": {
            "temperature": 0.9, "block_types": [{"id": "not_saved", "name": "new"}],
        }})
    assert state.config == before
    assert type_enum() == types_before
    assert (state.root / "config.yaml").read_bytes() == raw_before


def test_valid_settings_apply_to_same_client(settings_state):
    state, request = settings_state
    client = state.client
    put_settings(request, {"generate": {
        "timeout": 77, "max_concurrent_requests": 3, "output_language": "en",
    }})
    assert state.client is client
    assert client.timeout == 77
    assert client._max_slots == 3
    assert state.config["generate"]["output_language"] == "en"
