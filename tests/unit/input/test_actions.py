from flashback_sampler.input.core.actions import Action


def test_action_construction_with_defaults():
    called = []
    a = Action(id="transport.record", name="Record", category="Transport",
               callable=lambda: called.append(1))
    assert a.id == "transport.record"
    assert a.name == "Record"
    assert a.category == "Transport"
    assert a.default_binding is None
    assert a.repeat_policy == "fire"
    a.callable()
    assert called == [1]


def test_action_with_default_binding_and_repeat_policy():
    a = Action(id="transport.play", name="Play", category="Transport",
               callable=lambda: None, default_binding="Space",
               repeat_policy="ignore_repeat")
    assert a.default_binding == "Space"
    assert a.repeat_policy == "ignore_repeat"


import pytest
from flashback_sampler.input.core.actions import (
    register, get, all_actions, clear_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _mk(aid: str = "test.a", name: str = "A") -> Action:
    return Action(id=aid, name=name, category="Test", callable=lambda: None)


def test_register_and_get():
    a = _mk()
    register(a)
    assert get("test.a") is a


def test_get_unknown_returns_none():
    assert get("nope") is None


def test_register_duplicate_raises():
    register(_mk())
    with pytest.raises(ValueError, match="already registered"):
        register(_mk())


def test_all_actions_returns_registered():
    register(_mk("test.a", "A"))
    register(_mk("test.b", "B"))
    ids = {a.id for a in all_actions()}
    assert ids == {"test.a", "test.b"}


def test_clear_registry_empties():
    register(_mk())
    clear_registry()
    assert all_actions() == []


from flashback_sampler.input.core.actions import invoke


def test_invoke_calls_callable():
    called = []
    register(Action(id="t.x", name="X", category="T",
                    callable=lambda: called.append(1)))
    invoke("t.x")
    assert called == [1]


def test_invoke_unknown_action_is_noop():
    invoke("does.not.exist")  # should not raise


def test_invoke_fire_policy_fires_on_repeat():
    called = []
    register(Action(id="t.x", name="X", category="T",
                    callable=lambda: called.append(1),
                    repeat_policy="fire"))
    invoke("t.x", is_repeat=False)
    invoke("t.x", is_repeat=True)
    invoke("t.x", is_repeat=True)
    assert called == [1, 1, 1]


def test_invoke_ignore_repeat_policy_suppresses_repeats():
    called = []
    register(Action(id="t.x", name="X", category="T",
                    callable=lambda: called.append(1),
                    repeat_policy="ignore_repeat"))
    invoke("t.x", is_repeat=False)
    invoke("t.x", is_repeat=True)
    invoke("t.x", is_repeat=True)
    assert called == [1]


def test_invoke_edge_only_policy_suppresses_repeats():
    called = []
    register(Action(id="t.x", name="X", category="T",
                    callable=lambda: called.append(1),
                    repeat_policy="edge_only"))
    invoke("t.x", is_repeat=False)
    invoke("t.x", is_repeat=True)
    assert called == [1]
