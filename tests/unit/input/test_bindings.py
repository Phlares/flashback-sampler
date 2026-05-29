import pytest
from flashback_sampler.input.core.actions import (
    Action, register, clear_registry,
)
from flashback_sampler.input.core.bindings import BindingTable
from flashback_sampler.input.core.events import InputEvent


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _key(code: str) -> InputEvent:
    return InputEvent(source="keyboard", kind="press", code=code)


def test_resolve_uses_default_binding():
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable()
    assert table.resolve(_key("F13")) == "t.a"


def test_resolve_unknown_code_returns_none():
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable()
    assert table.resolve(_key("F14")) is None


def test_bind_overrides_default():
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable()
    table.bind("Ctrl+R", "t.a")
    assert table.resolve(_key("Ctrl+R")) == "t.a"
    # default no longer reachable once overridden
    assert table.resolve(_key("F13")) is None


def test_unbind_clears_override_and_defaults():
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable()
    table.unbind("F13")  # cleared — stores null
    assert table.resolve(_key("F13")) is None


def test_bind_to_unknown_action_raises():
    table = BindingTable()
    with pytest.raises(ValueError, match="unknown action"):
        table.bind("F13", "nonexistent.id")


def test_reset_one_restores_default():
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable()
    table.bind("Ctrl+R", "t.a")
    assert table.resolve(_key("Ctrl+R")) == "t.a"
    table.reset_one("t.a")
    assert table.resolve(_key("F13")) == "t.a"
    assert table.resolve(_key("Ctrl+R")) is None


def test_reset_to_defaults_restores_all():
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    register(Action(id="t.b", name="B", category="T",
                    callable=lambda: None, default_binding="F14"))
    table = BindingTable()
    table.bind("Ctrl+R", "t.a")
    table.unbind("F14")
    table.reset_to_defaults()
    assert table.resolve(_key("F13")) == "t.a"
    assert table.resolve(_key("F14")) == "t.b"
    assert table.resolve(_key("Ctrl+R")) is None



import json


def test_save_and_load_round_trip(tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    register(Action(id="t.b", name="B", category="T",
                    callable=lambda: None, default_binding="F14"))
    path = tmp_path / "bindings.json"

    table = BindingTable(storage_path=path)
    table.bind("Ctrl+R", "t.a")
    table.unbind("F14")
    table.save()

    assert path.exists()
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert data["bindings"] == {"Ctrl+R": "t.a", "F14": None}

    fresh = BindingTable(storage_path=path)
    fresh.load()
    assert fresh.resolve(_key("Ctrl+R")) == "t.a"
    assert fresh.resolve(_key("F14")) is None
    assert fresh.resolve(_key("F13")) is None  # t.a's default suppressed by override


def test_load_missing_file_is_noop(tmp_path):
    path = tmp_path / "absent.json"
    table = BindingTable(storage_path=path)
    table.load()  # should not raise
    # no overrides should be present
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    assert table.resolve(_key("F13")) == "t.a"


@pytest.mark.parametrize("content", [
    "{ this is not valid json",   # truncated / corrupt
    "[]",                          # valid JSON but not an object
    '"a string"',                  # valid JSON, wrong type
    '{"version": 1, "bindings": ["not", "a", "dict"]}',  # bindings wrong shape
    '{"version": 1}',              # no bindings key
])
def test_load_degrades_gracefully_on_bad_file(tmp_path, content):
    """A corrupt or hand-edited bindings.json must never break startup —
    load() falls back to defaults instead of raising."""
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    path = tmp_path / "bindings.json"
    path.write_text(content)
    table = BindingTable(storage_path=path)
    table.load()  # must not raise
    # falls back to defaults — t.a's default binding still resolves
    assert table.resolve(_key("F13")) == "t.a"


def test_load_skips_non_string_binding_values(tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    path = tmp_path / "bindings.json"
    # "BadKey" → 123 is a junk value; it must be dropped, while the valid
    # "Ctrl+R" → "t.a" override still loads.
    path.write_text('{"version": 1, "bindings": {"BadKey": 123, "Ctrl+R": "t.a"}}')
    table = BindingTable(storage_path=path)
    table.load()
    assert table.resolve(_key("Ctrl+R")) == "t.a"
    assert table.resolve(_key("BadKey")) is None  # junk dropped, not bound


def test_default_storage_path_has_no_doubled_segment():
    from flashback_sampler.input.core.bindings import default_storage_path
    p = default_storage_path()
    parts = p.parts
    # the app-name segment must not repeat back-to-back
    assert not any(
        parts[i] == parts[i + 1] == "flashback-sampler"
        for i in range(len(parts) - 1)
    ), f"doubled app-name segment in {p}"


def test_resolve_returns_existing_action_for_override():
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None))
    table = BindingTable()
    table.bind("Ctrl+R", "t.a")
    assert table.resolve(_key("Ctrl+R")) == "t.a"


def test_resolve_returns_default_action_for_unbound_code():
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable()
    assert table.resolve(_key("F13")) == "t.a"


def test_resolve_returns_none_for_free_code():
    table = BindingTable()
    assert table.resolve(_key("F20")) is None
