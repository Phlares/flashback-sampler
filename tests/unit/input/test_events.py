from flashback_sampler.input.core.events import InputEvent


def test_input_event_construction():
    e = InputEvent(source="keyboard", kind="press", code="F13")
    assert e.source == "keyboard"
    assert e.kind == "press"
    assert e.code == "F13"
    assert e.value is None
    assert e.is_repeat is False


def test_input_event_with_value_and_repeat():
    e = InputEvent(source="midi", kind="value", code="cc:7", value=0.5, is_repeat=True)
    assert e.value == 0.5
    assert e.is_repeat is True


def test_input_event_is_frozen():
    e = InputEvent(source="keyboard", kind="press", code="F13")
    import dataclasses
    try:
        e.code = "F14"
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("InputEvent must be frozen")


def test_input_event_equality_and_hash():
    a = InputEvent(source="keyboard", kind="press", code="F13")
    b = InputEvent(source="keyboard", kind="press", code="F13")
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1
