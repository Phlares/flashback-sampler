# Keybinding Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Qt-independent action + binding engine for flashback-sampler with an in-app Settings → Keybindings dialog, then migrate existing UI buttons to use it.

**Architecture:** Three-layer split under `flashback_sampler/input/`: a pure-Python `core/` (InputEvent, Action registry, BindingTable with JSON persistence), a Qt adapter under `sources/qt_keyboard.py`, and a `ui/settings_dialog.py`. JSON deltas persisted via `platformdirs`. Every existing `clicked.connect(self._handler)` becomes `register(Action(...))` + `clicked.connect(lambda: invoke(action_id))`.

**Tech Stack:** Python 3.10+, PySide6, `platformdirs`, pytest. Existing test pattern: `QApplication.instance() or QApplication([])` — no pytest-qt.

**Reference:** Spec at `docs/superpowers/specs/2026-04-21-keybinding-engine-design.md`.

---

## File Structure

**New files:**

| Path | Responsibility |
|---|---|
| `flashback_sampler/input/__init__.py` | Package marker |
| `flashback_sampler/input/core/__init__.py` | Re-exports for `Action`, `InputEvent`, `register`, `invoke`, `BindingTable` |
| `flashback_sampler/input/core/events.py` | `InputEvent` frozen dataclass |
| `flashback_sampler/input/core/actions.py` | `Action` dataclass + module-level registry (`register`, `invoke`, `get`, `all_actions`, `clear_registry`) |
| `flashback_sampler/input/core/bindings.py` | `BindingTable` class (bind/unbind/resolve/save/load/reset/conflicts) |
| `flashback_sampler/input/sources/__init__.py` | Package marker |
| `flashback_sampler/input/sources/qt_keyboard.py` | `KeyboardSource` — QKeyEvent filter → InputEvent → BindingTable.resolve → invoke |
| `flashback_sampler/input/ui/__init__.py` | Package marker |
| `flashback_sampler/input/ui/settings_dialog.py` | `KeybindingsDialog(QDialog)` with table, rebind modal, reset, filter, persist |
| `tests/unit/input/__init__.py` | Test package marker |
| `tests/unit/input/test_events.py` | `InputEvent` tests |
| `tests/unit/input/test_actions.py` | Action + registry + invoke tests |
| `tests/unit/input/test_bindings.py` | BindingTable tests |
| `tests/unit/input/test_qt_keyboard.py` | Qt adapter tests |
| `tests/unit/input/test_settings_dialog.py` | Settings dialog tests |

**Modified files:**

- `pyproject.toml` — add `platformdirs>=4.0` to `dependencies`
- `flashback_sampler/app/turntable_window.py` — menu item, action registration, replace `.clicked.connect(self._handler)` with `.clicked.connect(lambda: invoke(id))`
- `flashback_sampler/app/main_window.py` — same treatment for its own buttons/menu
- `flashback_sampler/app/widgets/center_bridge.py` — no change (buttons are owned by turntable_window; registration happens where handlers live)

---

## Task 1: Scaffold packages and add platformdirs dependency

**Files:**
- Modify: `pyproject.toml`
- Create: `flashback_sampler/input/__init__.py`
- Create: `flashback_sampler/input/core/__init__.py`
- Create: `flashback_sampler/input/sources/__init__.py`
- Create: `flashback_sampler/input/ui/__init__.py`
- Create: `tests/unit/input/__init__.py`

- [ ] **Step 1: Add dependency to pyproject.toml**

Edit `pyproject.toml`, in the `dependencies` list, add:

```toml
    "platformdirs>=4.0",
```

(Insert alphabetically, between `numpy` and `PySide6`.)

- [ ] **Step 2: Create empty package files**

Create each file with content:

```python
# flashback_sampler/input/__init__.py
```
(empty file with just the line above as a comment, or fully empty)

Do the same for:
- `flashback_sampler/input/core/__init__.py`
- `flashback_sampler/input/sources/__init__.py`
- `flashback_sampler/input/ui/__init__.py`
- `tests/unit/input/__init__.py`

- [ ] **Step 3: Install the new dependency**

Run: `pip install -e .`
Expected: Resolves and installs `platformdirs`.

- [ ] **Step 4: Verify import works**

Run: `python -c "import platformdirs; print(platformdirs.user_config_dir('flashback-sampler'))"`
Expected: Prints a path like `C:\Users\user\AppData\Local\flashback-sampler\flashback-sampler` (Windows) or similar.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml flashback_sampler/input tests/unit/input
git commit -m "chore(input): scaffold input package and add platformdirs dep"
```

---

## Task 2: InputEvent dataclass

**Files:**
- Create: `flashback_sampler/input/core/events.py`
- Create: `tests/unit/input/test_events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/input/test_events.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/input/test_events.py -v`
Expected: Fails with `ModuleNotFoundError: No module named 'flashback_sampler.input.core.events'` (or similar).

- [ ] **Step 3: Implement**

Create `flashback_sampler/input/core/events.py`:

```python
from dataclasses import dataclass
from typing import Literal

EventKind = Literal["press", "release", "value"]


@dataclass(frozen=True)
class InputEvent:
    source: str
    kind: EventKind
    code: str
    value: float | None = None
    is_repeat: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/input/test_events.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/core/events.py tests/unit/input/test_events.py
git commit -m "feat(input): add InputEvent dataclass"
```

---

## Task 3: Action dataclass

**Files:**
- Create: `flashback_sampler/input/core/actions.py`
- Create: `tests/unit/input/test_actions.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/input/test_actions.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/input/test_actions.py -v`
Expected: Fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `flashback_sampler/input/core/actions.py`:

```python
from dataclasses import dataclass
from typing import Callable, Literal

RepeatPolicy = Literal["fire", "ignore_repeat", "edge_only"]


@dataclass
class Action:
    id: str
    name: str
    category: str
    callable: Callable[[], None]
    default_binding: str | None = None
    repeat_policy: RepeatPolicy = "fire"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/input/test_actions.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/core/actions.py tests/unit/input/test_actions.py
git commit -m "feat(input): add Action dataclass"
```

---

## Task 4: Action registry (register, get, all_actions, clear_registry)

**Files:**
- Modify: `flashback_sampler/input/core/actions.py`
- Modify: `tests/unit/input/test_actions.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/input/test_actions.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_actions.py -v`
Expected: New tests fail with `ImportError: cannot import name 'register' ...`.

- [ ] **Step 3: Implement registry**

Append to `flashback_sampler/input/core/actions.py`:

```python
_registry: dict[str, Action] = {}


def register(action: Action) -> None:
    if action.id in _registry:
        raise ValueError(f"Action {action.id!r} already registered")
    _registry[action.id] = action


def get(action_id: str) -> Action | None:
    return _registry.get(action_id)


def all_actions() -> list[Action]:
    return list(_registry.values())


def clear_registry() -> None:
    _registry.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_actions.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/core/actions.py tests/unit/input/test_actions.py
git commit -m "feat(input): add action registry (register/get/all/clear)"
```

---

## Task 5: invoke() with repeat_policy

**Files:**
- Modify: `flashback_sampler/input/core/actions.py`
- Modify: `tests/unit/input/test_actions.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/input/test_actions.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_actions.py -v`
Expected: New tests fail with `ImportError: cannot import name 'invoke'`.

- [ ] **Step 3: Implement invoke**

Append to `flashback_sampler/input/core/actions.py`:

```python
def invoke(action_id: str, *, is_repeat: bool = False) -> None:
    action = _registry.get(action_id)
    if action is None:
        return
    if is_repeat and action.repeat_policy in ("ignore_repeat", "edge_only"):
        return
    action.callable()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_actions.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/core/actions.py tests/unit/input/test_actions.py
git commit -m "feat(input): add invoke() with repeat_policy gating"
```

---

## Task 6: BindingTable — in-memory bind/unbind/resolve with defaults layering

**Files:**
- Create: `flashback_sampler/input/core/bindings.py`
- Create: `tests/unit/input/test_bindings.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/input/test_bindings.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_bindings.py -v`
Expected: Fail with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `flashback_sampler/input/core/bindings.py`:

```python
from flashback_sampler.input.core import actions
from flashback_sampler.input.core.events import InputEvent


class BindingTable:
    """Maps event codes to action IDs.

    Resolution order: user overrides win over registered defaults. A user
    override may be ``None`` (explicitly cleared), which suppresses the
    default for that code. Missing entries fall back to defaults.
    """

    def __init__(self) -> None:
        # Overrides: code -> action_id or None (None = explicitly unbound)
        self._overrides: dict[str, str | None] = {}
        # Reverse set of codes that are overridden (for fast default suppression)
        self._overridden_action_ids: set[str] = set()

    # --- core mutation ---

    def bind(self, event_code: str, action_id: str) -> None:
        if actions.get(action_id) is None:
            raise ValueError(f"unknown action {action_id!r}")
        self._overrides[event_code] = action_id
        self._overridden_action_ids.add(action_id)

    def unbind(self, event_code: str) -> None:
        # Record explicit null so the default doesn't re-apply
        self._overrides[event_code] = None
        # Also suppress the default binding of whichever action owned this code
        for a in actions.all_actions():
            if a.default_binding == event_code:
                self._overridden_action_ids.add(a.id)

    # --- lookup ---

    def resolve(self, event: InputEvent) -> str | None:
        code = event.code
        if code in self._overrides:
            return self._overrides[code]  # may be None
        for a in actions.all_actions():
            if a.default_binding == code and a.id not in self._overridden_action_ids:
                return a.id
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_bindings.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/core/bindings.py tests/unit/input/test_bindings.py
git commit -m "feat(input): add BindingTable with defaults layering"
```

---

## Task 7: BindingTable — reset methods

**Files:**
- Modify: `flashback_sampler/input/core/bindings.py`
- Modify: `tests/unit/input/test_bindings.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/input/test_bindings.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_bindings.py -v`
Expected: New tests fail with `AttributeError: ... has no attribute 'reset_one'`.

- [ ] **Step 3: Implement**

Append to `BindingTable` class in `flashback_sampler/input/core/bindings.py`:

```python
    def reset_one(self, action_id: str) -> None:
        # Drop any overrides pointing to this action
        self._overrides = {
            code: aid for code, aid in self._overrides.items() if aid != action_id
        }
        # Also drop null entries targeting the default code of this action
        a = actions.get(action_id)
        if a is not None and a.default_binding in self._overrides:
            if self._overrides[a.default_binding] is None:
                del self._overrides[a.default_binding]
        self._overridden_action_ids.discard(action_id)

    def reset_to_defaults(self) -> None:
        self._overrides.clear()
        self._overridden_action_ids.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_bindings.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/core/bindings.py tests/unit/input/test_bindings.py
git commit -m "feat(input): add BindingTable reset_one and reset_to_defaults"
```

---

## Task 8: BindingTable — JSON persistence with platformdirs

**Files:**
- Modify: `flashback_sampler/input/core/bindings.py`
- Modify: `tests/unit/input/test_bindings.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/input/test_bindings.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_bindings.py -v`
Expected: New tests fail — `BindingTable.__init__` doesn't take `storage_path`.

- [ ] **Step 3: Implement**

Replace `BindingTable.__init__` and append `save`/`load` in `flashback_sampler/input/core/bindings.py`:

```python
import json
from pathlib import Path

import platformdirs

SCHEMA_VERSION = 1


def default_storage_path() -> Path:
    return Path(platformdirs.user_config_dir("flashback-sampler")) / "bindings.json"


class BindingTable:
    def __init__(self, storage_path: Path | None = None) -> None:
        self._overrides: dict[str, str | None] = {}
        self._overridden_action_ids: set[str] = set()
        self._storage_path: Path = storage_path or default_storage_path()

    # ... bind/unbind/resolve/reset_one/reset_to_defaults unchanged ...

    # --- persistence ---

    def save(self) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": SCHEMA_VERSION, "bindings": self._overrides}
        self._storage_path.write_text(json.dumps(payload, indent=2))

    def load(self) -> None:
        if not self._storage_path.exists():
            return
        data = json.loads(self._storage_path.read_text())
        overrides: dict[str, str | None] = data.get("bindings", {})
        self._overrides = dict(overrides)
        self._overridden_action_ids = {
            aid for aid in self._overrides.values() if aid is not None
        }
        # Re-suppress defaults for codes with explicit null
        for code, aid in self._overrides.items():
            if aid is None:
                for a in actions.all_actions():
                    if a.default_binding == code:
                        self._overridden_action_ids.add(a.id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_bindings.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/core/bindings.py tests/unit/input/test_bindings.py
git commit -m "feat(input): add BindingTable JSON persistence via platformdirs"
```

---

## Task 9: BindingTable — conflict detection

**Files:**
- Modify: `flashback_sampler/input/core/bindings.py`
- Modify: `tests/unit/input/test_bindings.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/input/test_bindings.py`:

```python
def test_conflicts_returns_existing_action_for_override():
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None))
    table = BindingTable()
    table.bind("Ctrl+R", "t.a")
    assert table.conflicts("Ctrl+R") == "t.a"


def test_conflicts_returns_default_action_for_unbound_code():
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable()
    assert table.conflicts("F13") == "t.a"


def test_conflicts_returns_none_for_free_code():
    table = BindingTable()
    assert table.conflicts("F20") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_bindings.py -v`
Expected: Fails with `AttributeError: ... no attribute 'conflicts'`.

- [ ] **Step 3: Implement**

Append to `BindingTable` class:

```python
    def conflicts(self, event_code: str) -> str | None:
        return self.resolve(InputEvent(source="keyboard", kind="press", code=event_code))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_bindings.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/core/bindings.py tests/unit/input/test_bindings.py
git commit -m "feat(input): add BindingTable.conflicts()"
```

---

## Task 10: Core package re-exports

**Files:**
- Modify: `flashback_sampler/input/core/__init__.py`

- [ ] **Step 1: Write re-exports**

Replace contents of `flashback_sampler/input/core/__init__.py`:

```python
from flashback_sampler.input.core.actions import (
    Action,
    RepeatPolicy,
    register,
    invoke,
    get,
    all_actions,
    clear_registry,
)
from flashback_sampler.input.core.bindings import BindingTable, default_storage_path
from flashback_sampler.input.core.events import EventKind, InputEvent

__all__ = [
    "Action",
    "BindingTable",
    "EventKind",
    "InputEvent",
    "RepeatPolicy",
    "all_actions",
    "clear_registry",
    "default_storage_path",
    "get",
    "invoke",
    "register",
]
```

- [ ] **Step 2: Verify imports resolve**

Run: `python -c "from flashback_sampler.input.core import Action, BindingTable, InputEvent, register, invoke; print('ok')"`
Expected: Prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add flashback_sampler/input/core/__init__.py
git commit -m "feat(input): re-export core types from input.core package"
```

---

## Task 11: Qt keyboard adapter

**Files:**
- Create: `flashback_sampler/input/sources/qt_keyboard.py`
- Create: `tests/unit/input/test_qt_keyboard.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/input/test_qt_keyboard.py`:

```python
import pytest
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QWidget

from flashback_sampler.input.core import (
    Action, BindingTable, clear_registry, register,
)
from flashback_sampler.input.sources.qt_keyboard import KeyboardSource


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _press(key: int, modifiers: Qt.KeyboardModifier = Qt.NoModifier, auto_repeat: bool = False) -> QKeyEvent:
    return QKeyEvent(QEvent.KeyPress, key, modifiers, 0, 0, 0, "", auto_repeat, 1)


def test_press_event_invokes_bound_action(qapp):
    fired = []
    register(Action(id="t.play", name="Play", category="T",
                    callable=lambda: fired.append(1),
                    default_binding="Space"))
    table = BindingTable()
    widget = QWidget()
    source = KeyboardSource(table, widget)

    source.eventFilter(widget, _press(Qt.Key_Space))
    assert fired == [1]


def test_release_event_ignored(qapp):
    fired = []
    register(Action(id="t.play", name="Play", category="T",
                    callable=lambda: fired.append(1),
                    default_binding="Space"))
    table = BindingTable()
    widget = QWidget()
    source = KeyboardSource(table, widget)
    release = QKeyEvent(QEvent.KeyRelease, Qt.Key_Space, Qt.NoModifier, 0, 0, 0, "", False, 1)
    source.eventFilter(widget, release)
    assert fired == []


def test_modifier_chord_resolves(qapp):
    fired = []
    register(Action(id="t.record", name="Record", category="T",
                    callable=lambda: fired.append(1),
                    default_binding="Ctrl+Shift+R"))
    table = BindingTable()
    widget = QWidget()
    source = KeyboardSource(table, widget)
    event = _press(Qt.Key_R, Qt.ControlModifier | Qt.ShiftModifier)
    source.eventFilter(widget, event)
    assert fired == [1]


def test_auto_repeat_flag_respects_ignore_repeat_policy(qapp):
    fired = []
    register(Action(id="t.record", name="Record", category="T",
                    callable=lambda: fired.append(1),
                    default_binding="R",
                    repeat_policy="ignore_repeat"))
    table = BindingTable()
    widget = QWidget()
    source = KeyboardSource(table, widget)
    source.eventFilter(widget, _press(Qt.Key_R, auto_repeat=False))
    source.eventFilter(widget, _press(Qt.Key_R, auto_repeat=True))
    source.eventFilter(widget, _press(Qt.Key_R, auto_repeat=True))
    assert fired == [1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_qt_keyboard.py -v`
Expected: Fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `flashback_sampler/input/sources/qt_keyboard.py`:

```python
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QWidget

from flashback_sampler.input.core import BindingTable, invoke
from flashback_sampler.input.core.events import InputEvent


class KeyboardSource(QObject):
    """Qt event filter that converts QKeyEvent presses into InputEvents,
    resolves them through a BindingTable, and dispatches via ``invoke``.

    Installs itself as an event filter on ``target_widget`` on construction.
    """

    def __init__(self, table: BindingTable, target_widget: QWidget) -> None:
        super().__init__(target_widget)
        self._table = table
        target_widget.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.KeyPress:
            return False
        assert isinstance(event, QKeyEvent)
        code = self._event_to_code(event)
        if code is None:
            return False
        ie = InputEvent(
            source="keyboard",
            kind="press",
            code=code,
            is_repeat=event.isAutoRepeat(),
        )
        action_id = self._table.resolve(ie)
        if action_id is None:
            return False
        invoke(action_id, is_repeat=ie.is_repeat)
        return True  # consumed

    @staticmethod
    def _event_to_code(event: QKeyEvent) -> str | None:
        # Ignore modifier-only presses
        if event.key() in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return None
        seq = QKeySequence(event.keyCombination()).toString(QKeySequence.PortableText)
        return seq or None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_qt_keyboard.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/sources/qt_keyboard.py tests/unit/input/test_qt_keyboard.py
git commit -m "feat(input): add Qt keyboard event adapter"
```

---

## Task 12: Settings dialog — skeleton and population

**Files:**
- Create: `flashback_sampler/input/ui/settings_dialog.py`
- Create: `tests/unit/input/test_settings_dialog.py`

- [ ] **Step 1: Write failing test**

Create `tests/unit/input/test_settings_dialog.py`:

```python
import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.input.core import (
    Action, BindingTable, clear_registry, register,
)
from flashback_sampler.input.ui.settings_dialog import KeybindingsDialog


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def test_dialog_shows_all_registered_actions(qapp, tmp_path):
    register(Action(id="t.a", name="Alpha", category="Test",
                    callable=lambda: None, default_binding="F13"))
    register(Action(id="t.b", name="Beta", category="Test",
                    callable=lambda: None))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    shown_ids = {row.action.id for row in dialog._rows}
    assert shown_ids == {"t.a", "t.b"}


def test_dialog_shows_category_headers(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="Transport",
                    callable=lambda: None))
    register(Action(id="t.b", name="B", category="Deck",
                    callable=lambda: None))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    categories = set(dialog._category_labels())
    assert "Transport" in categories
    assert "Deck" in categories
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_settings_dialog.py -v`
Expected: Fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `flashback_sampler/input/ui/settings_dialog.py`:

```python
from dataclasses import dataclass

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from flashback_sampler.input.core import (
    Action,
    BindingTable,
    InputEvent,
    all_actions,
)


@dataclass
class _Row:
    action: Action
    binding_label: QLabel
    rebind_btn: QPushButton
    clear_btn: QPushButton


class KeybindingsDialog(QDialog):
    """Dialog for viewing and editing key bindings for every registered action.

    Construction is idempotent: snapshots the registry and current bindings
    into an in-memory edit buffer. Accept (OK) writes through to the passed-in
    ``BindingTable`` and saves it to disk; reject (Cancel) discards the buffer.
    """

    def __init__(self, table: BindingTable) -> None:
        super().__init__()
        self.setWindowTitle("Keybindings")
        self.resize(640, 480)
        self._table = table
        self._edit_buffer: dict[str, str | None] = dict(table._overrides)  # will be refined in Task 15
        self._rows: list[_Row] = []
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Filter row
        filter_row = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter actions…")
        filter_row.addWidget(self._filter)
        self._reset_all_btn = QPushButton("Reset all to defaults")
        filter_row.addWidget(self._reset_all_btn)
        root.addLayout(filter_row)

        # Scrollable action list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        self._list_layout = QVBoxLayout(inner)
        scroll.setWidget(inner)
        root.addWidget(scroll, stretch=1)

        # Populate grouped by category
        by_category: dict[str, list[Action]] = {}
        for a in sorted(all_actions(), key=lambda a: (a.category, a.name)):
            by_category.setdefault(a.category, []).append(a)

        for category, acts in by_category.items():
            header = QLabel(f"<b>{category}</b>")
            self._list_layout.addWidget(header)
            for a in acts:
                self._add_row(a)

        self._list_layout.addStretch(1)

        # Dialog buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _add_row(self, action: Action) -> None:
        row = QHBoxLayout()
        name = QLabel(action.name)
        name.setMinimumWidth(200)
        row.addWidget(name)

        current = self._current_binding(action)
        binding_label = QLabel(current if current else "(unbound)")
        binding_label.setMinimumWidth(140)
        row.addWidget(binding_label)

        rebind_btn = QPushButton("Rebind")
        clear_btn = QPushButton("×")
        clear_btn.setFixedWidth(28)
        row.addWidget(rebind_btn)
        row.addWidget(clear_btn)
        row.addStretch(1)

        container = QWidget()
        container.setLayout(row)
        self._list_layout.addWidget(container)

        self._rows.append(_Row(action=action,
                               binding_label=binding_label,
                               rebind_btn=rebind_btn,
                               clear_btn=clear_btn))

    def _current_binding(self, action: Action) -> str | None:
        # Check edit buffer first: explicit override or explicit null
        for code, aid in self._edit_buffer.items():
            if aid == action.id:
                return code
        # null for action's default code means unbound
        if action.default_binding in self._edit_buffer and self._edit_buffer[action.default_binding] is None:
            return None
        return action.default_binding

    def _category_labels(self) -> list[str]:
        return sorted({a.category for a in all_actions()})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_settings_dialog.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/ui/settings_dialog.py tests/unit/input/test_settings_dialog.py
git commit -m "feat(input): add KeybindingsDialog skeleton with registry population"
```

---

## Task 13: Settings dialog — rebind flow

**Files:**
- Modify: `flashback_sampler/input/ui/settings_dialog.py`
- Modify: `tests/unit/input/test_settings_dialog.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/input/test_settings_dialog.py`:

```python
def test_rebind_updates_edit_buffer(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "Ctrl+Shift+F20")
    assert dialog._edit_buffer == {"Ctrl+Shift+F20": "t.a"}
    # Label updated
    row = next(r for r in dialog._rows if r.action.id == "t.a")
    assert row.binding_label.text() == "Ctrl+Shift+F20"


def test_rebind_replaces_previous_binding_for_same_action(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "F20")
    dialog._apply_rebind("t.a", "F21")
    assert dialog._edit_buffer == {"F21": "t.a"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_settings_dialog.py -v`
Expected: Fails with `AttributeError: ... no attribute '_apply_rebind'`.

- [ ] **Step 3: Implement**

In `KeybindingsDialog`, wire rebind buttons in `_add_row` to open a `_RebindModal`, and add `_apply_rebind`:

Add at top of `settings_dialog.py`:

```python
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QKeySequenceEdit


class _RebindModal(QDialog):
    def __init__(self, action_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Rebind: {action_name}")
        self.setModal(True)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Press a key or chord for '{action_name}' (Esc to cancel):"))
        self._edit = QKeySequenceEdit()
        lay.addWidget(self._edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def selected_code(self) -> str | None:
        seq = self._edit.keySequence()
        text = seq.toString(QKeySequence.PortableText)
        return text or None
```

Modify `_add_row` — append after `row.addWidget(clear_btn)`:

```python
        rebind_btn.clicked.connect(lambda _=False, aid=action.id, name=action.name:
                                   self._open_rebind(aid, name))
        clear_btn.clicked.connect(lambda _=False, aid=action.id: self._apply_clear(aid))
```

Add methods to `KeybindingsDialog`:

```python
    def _open_rebind(self, action_id: str, action_name: str) -> None:
        modal = _RebindModal(action_name, parent=self)
        if modal.exec() == QDialog.Accepted:
            code = modal.selected_code()
            if code:
                self._apply_rebind(action_id, code)

    def _apply_rebind(self, action_id: str, code: str) -> None:
        # Remove any previous override mapping to this action
        self._edit_buffer = {c: aid for c, aid in self._edit_buffer.items() if aid != action_id}
        self._edit_buffer[code] = action_id
        self._refresh_labels()

    def _apply_clear(self, action_id: str) -> None:
        a = next((x for x in all_actions() if x.id == action_id), None)
        # Drop any overrides mapping to this action
        self._edit_buffer = {c: aid for c, aid in self._edit_buffer.items() if aid != action_id}
        # If the action has a default binding, record null to suppress it
        if a and a.default_binding:
            self._edit_buffer[a.default_binding] = None
        self._refresh_labels()

    def _refresh_labels(self) -> None:
        for row in self._rows:
            current = self._current_binding(row.action)
            row.binding_label.setText(current if current else "(unbound)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_settings_dialog.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/ui/settings_dialog.py tests/unit/input/test_settings_dialog.py
git commit -m "feat(input): add rebind modal and edit-buffer handling"
```

---

## Task 14: Settings dialog — conflict handling, reset, filter

**Files:**
- Modify: `flashback_sampler/input/ui/settings_dialog.py`
- Modify: `tests/unit/input/test_settings_dialog.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/input/test_settings_dialog.py`:

```python
def test_rebind_to_conflicting_code_replaces_previous_owner(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    register(Action(id="t.b", name="B", category="T",
                    callable=lambda: None))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    # Bind F13 to t.b — conflicts with t.a's default
    dialog._apply_rebind("t.b", "F13")
    assert dialog._edit_buffer["F13"] == "t.b"
    # t.a must now show (unbound)
    row_a = next(r for r in dialog._rows if r.action.id == "t.a")
    assert row_a.binding_label.text() == "(unbound)"


def test_attempt_rebind_with_conflict_asks_for_confirmation(qapp, tmp_path):
    register(Action(id="t.a", name="Alpha", category="T",
                    callable=lambda: None, default_binding="F13"))
    register(Action(id="t.b", name="Beta", category="T",
                    callable=lambda: None))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)

    # Simulate user declining the confirmation — rebind should NOT apply.
    dialog._confirm_conflict_replace = lambda code, owner_id: False
    dialog._attempt_rebind("t.b", "F13")
    assert "F13" not in dialog._edit_buffer

    # Simulate user accepting — rebind should apply.
    dialog._confirm_conflict_replace = lambda code, owner_id: True
    dialog._attempt_rebind("t.b", "F13")
    assert dialog._edit_buffer.get("F13") == "t.b"


def test_reset_all_clears_edit_buffer(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "Ctrl+R")
    dialog._apply_reset_all()
    assert dialog._edit_buffer == {}
    row = next(r for r in dialog._rows if r.action.id == "t.a")
    assert row.binding_label.text() == "F13"  # default


def test_filter_hides_non_matching_rows(qapp, tmp_path):
    register(Action(id="t.a", name="Alpha", category="T",
                    callable=lambda: None))
    register(Action(id="t.b", name="Beta", category="T",
                    callable=lambda: None))
    table = BindingTable(storage_path=tmp_path / "b.json")
    dialog = KeybindingsDialog(table)
    dialog._apply_filter("alp")
    visible = {r.action.id for r in dialog._rows if r.binding_label.parentWidget().isVisible()}
    assert visible == {"t.a"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_settings_dialog.py -v`
Expected: New tests fail with `AttributeError: ... no attribute '_apply_reset_all'`.

- [ ] **Step 3: Implement**

Update `_apply_rebind` in `settings_dialog.py` to also evict any existing override at the new code, and add an `_attempt_rebind` wrapper that checks for conflicts and prompts the user first:

```python
    def _apply_rebind(self, action_id: str, code: str) -> None:
        # Evict any existing owner of this code and any previous override for this action
        self._edit_buffer = {c: aid for c, aid in self._edit_buffer.items()
                             if c != code and aid != action_id}
        self._edit_buffer[code] = action_id
        self._refresh_labels()

    def _attempt_rebind(self, action_id: str, code: str) -> None:
        """Check for a conflict before applying the rebind.

        If ``code`` is already bound to a different action (via override or
        default), prompts the user via ``_confirm_conflict_replace``. Applies
        the rebind only if no conflict or the user confirms.
        """
        # Compute the current owner of ``code`` using the edit buffer + defaults
        current_owner: str | None = None
        if code in self._edit_buffer:
            current_owner = self._edit_buffer[code]
        else:
            for a in all_actions():
                if a.default_binding == code:
                    current_owner = a.id
                    break
        if current_owner is not None and current_owner != action_id:
            if not self._confirm_conflict_replace(code, current_owner):
                return
        self._apply_rebind(action_id, code)

    def _confirm_conflict_replace(self, code: str, current_owner_id: str) -> bool:
        """Shown to the user when a rebind would override an existing binding.

        Separate method so tests can override without invoking QMessageBox.
        Returns True to proceed with the replacement, False to cancel.
        """
        from PySide6.QtWidgets import QMessageBox
        owner = next((a for a in all_actions() if a.id == current_owner_id), None)
        owner_name = owner.name if owner else current_owner_id
        reply = QMessageBox.question(
            self,
            "Binding conflict",
            f"{code} is already bound to '{owner_name}'. Replace?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        return reply == QMessageBox.StandardButton.Yes
```

Then update `_open_rebind` to use `_attempt_rebind` instead of `_apply_rebind` directly:

```python
    def _open_rebind(self, action_id: str, action_name: str) -> None:
        modal = _RebindModal(action_name, parent=self)
        if modal.exec() == QDialog.Accepted:
            code = modal.selected_code()
            if code:
                self._attempt_rebind(action_id, code)
```

Add `_apply_reset_all`, `_apply_filter`, and wire signals. In `_build_ui`, after the `filter_row` block, connect the filter and reset-all:

```python
        self._filter.textChanged.connect(self._apply_filter)
        self._reset_all_btn.clicked.connect(self._apply_reset_all)
```

Add methods:

```python
    def _apply_reset_all(self) -> None:
        self._edit_buffer = {}
        self._refresh_labels()

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row in self._rows:
            container = row.binding_label.parentWidget()
            if not needle:
                container.setVisible(True)
                continue
            hay = f"{row.action.name} {row.action.category} {row.action.id}".lower()
            container.setVisible(needle in hay)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_settings_dialog.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/ui/settings_dialog.py tests/unit/input/test_settings_dialog.py
git commit -m "feat(input): add conflict confirmation, reset-all, and filter to dialog"
```

---

## Task 15: Settings dialog — OK/Cancel persistence

**Files:**
- Modify: `flashback_sampler/input/ui/settings_dialog.py`
- Modify: `tests/unit/input/test_settings_dialog.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/input/test_settings_dialog.py`:

```python
def test_ok_writes_edit_buffer_to_table_and_disk(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    path = tmp_path / "b.json"
    table = BindingTable(storage_path=path)
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "Ctrl+R")
    dialog.accept()
    assert table._overrides == {"Ctrl+R": "t.a"}
    assert path.exists()


def test_cancel_does_not_touch_table_or_disk(qapp, tmp_path):
    register(Action(id="t.a", name="A", category="T",
                    callable=lambda: None, default_binding="F13"))
    path = tmp_path / "b.json"
    table = BindingTable(storage_path=path)
    dialog = KeybindingsDialog(table)
    dialog._apply_rebind("t.a", "Ctrl+R")
    dialog.reject()
    assert table._overrides == {}
    assert not path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/input/test_settings_dialog.py -v`
Expected: `test_ok_writes_edit_buffer_to_table_and_disk` fails — accept() doesn't flush buffer to table yet.

- [ ] **Step 3: Implement**

Override `accept` in `KeybindingsDialog`:

```python
    def accept(self) -> None:
        # Replace table state with edit buffer
        self._table._overrides = dict(self._edit_buffer)
        self._table._overridden_action_ids = {
            aid for aid in self._edit_buffer.values() if aid is not None
        }
        # Re-suppress defaults for explicit nulls
        for code, aid in self._edit_buffer.items():
            if aid is None:
                for a in all_actions():
                    if a.default_binding == code:
                        self._table._overridden_action_ids.add(a.id)
        self._table.save()
        super().accept()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/input/test_settings_dialog.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/input/ui/settings_dialog.py tests/unit/input/test_settings_dialog.py
git commit -m "feat(input): persist edit buffer on OK, discard on Cancel"
```

---

## Task 16: Wire Settings → Keybindings into the app

**Files:**
- Modify: `flashback_sampler/app/turntable_window.py`

- [ ] **Step 1: Identify menu-bar setup location**

Open `flashback_sampler/app/turntable_window.py`. Search for existing menu bar creation (`menuBar()` or `QMenuBar`). If no settings menu exists yet, we'll add one.

Run: `grep -n "menuBar\|QMenuBar\|Settings" flashback_sampler/app/turntable_window.py`

- [ ] **Step 2: Add menu action and handler**

Near the top of the file, add imports:

```python
from flashback_sampler.input.core import BindingTable
from flashback_sampler.input.sources.qt_keyboard import KeyboardSource
from flashback_sampler.input.ui.settings_dialog import KeybindingsDialog
```

In `TurntableWindow.__init__`, after `super().__init__()` and window setup, instantiate the binding table and keyboard source:

```python
        self._binding_table = BindingTable()
        self._binding_table.load()
        self._keyboard_source = KeyboardSource(self._binding_table, self)
```

Add a Settings menu with a Keybindings action. Somewhere after widget construction and before `show()`:

```python
        settings_menu = self.menuBar().addMenu("Settings")
        keybindings_act = settings_menu.addAction("Keybindings…")
        keybindings_act.triggered.connect(self._open_keybindings_dialog)
```

Add the handler method:

```python
    def _open_keybindings_dialog(self) -> None:
        dialog = KeybindingsDialog(self._binding_table)
        dialog.exec()
```

- [ ] **Step 3: Smoke test the menu**

Run the app manually:

```bash
python -m flashback_sampler
```

Expected:
- Window opens
- "Settings" menu is present in menu bar
- Settings → Keybindings opens the dialog (will be empty of actions since none are registered yet)
- Cancel closes the dialog without error

- [ ] **Step 4: Commit**

```bash
git add flashback_sampler/app/turntable_window.py
git commit -m "feat(app): add Settings → Keybindings menu entry"
```

---

## Task 17: Migrate transport actions (start, stop, play)

**Files:**
- Modify: `flashback_sampler/app/turntable_window.py`

Convert the existing signal connections at lines ~285, ~286, ~324, and the ~339 Space shortcut to actions. Do not change the handler methods themselves.

- [ ] **Step 1: Add registration block in TurntableWindow.__init__**

Near the top of `turntable_window.py` add import:

```python
from flashback_sampler.input.core import Action, invoke, register
```

In `TurntableWindow.__init__`, **after** all relevant `self._on_...` handler methods are bound (they're defined on the class so this is already true by `__init__` time) and **before** the existing `.clicked.connect` lines you're replacing, add:

```python
        register(Action(id="transport.start_recording", name="Start Recording",
                        category="Transport",
                        callable=self._on_start_clicked,
                        repeat_policy="ignore_repeat"))
        register(Action(id="transport.stop_recording", name="Stop Recording",
                        category="Transport",
                        callable=self._on_stop_clicked,
                        repeat_policy="ignore_repeat"))
        register(Action(id="transport.play_clip", name="Play Clip",
                        category="Transport",
                        callable=self._on_play_clip_clicked,
                        default_binding="Space",
                        repeat_policy="ignore_repeat"))
```

- [ ] **Step 2: Replace signal connections**

In `turntable_window.py`, find:

```python
self.center_bridge.start_btn.clicked.connect(self._on_start_clicked)
self.center_bridge.stop_btn.clicked.connect(self._on_stop_clicked)
```

Replace with:

```python
self.center_bridge.start_btn.clicked.connect(lambda: invoke("transport.start_recording"))
self.center_bridge.stop_btn.clicked.connect(lambda: invoke("transport.stop_recording"))
```

And find:

```python
self.clip_controls[0].clicked.connect(self._on_play_clip_clicked)
```

Replace with:

```python
self.clip_controls[0].clicked.connect(lambda: invoke("transport.play_clip"))
```

- [ ] **Step 3: Remove the redundant Space QShortcut**

Find the Space shortcut block near line 339 and delete it:

```python
play_sc = QShortcut(QKeySequence(Qt.Key_Space), self)
play_sc.activated.connect(self._on_play_clip_clicked)
```

(The keybinding engine now handles Space through the default_binding of `transport.play_clip`.)

- [ ] **Step 4: Reload the binding table after registration**

In `__init__`, move `self._binding_table.load()` to **after** all `register(...)` calls so that user overrides correctly layer on top of the newly-registered defaults. The final ordering should be:

```python
self._binding_table = BindingTable()
self._keyboard_source = KeyboardSource(self._binding_table, self)
# ... register(...) calls ...
self._binding_table.load()
```

- [ ] **Step 5: Smoke test**

Run: `python -m flashback_sampler`
Expected:
- Start/Stop buttons still work
- Space key still plays the current clip
- Settings → Keybindings dialog now shows "Transport" category with three actions

- [ ] **Step 6: Commit**

```bash
git add flashback_sampler/app/turntable_window.py
git commit -m "refactor(app): migrate transport controls to action registry"
```

---

## Task 18: Migrate checkout, save, nav bar, and remaining indexed controls

**Files:**
- Modify: `flashback_sampler/app/turntable_window.py`

- [ ] **Step 1: Audit remaining signal connections**

Run: `grep -n "\.clicked\.connect" flashback_sampler/app/turntable_window.py`

This produces the list of buttons still bypassing the registry. Before editing, identify each handler method and pick a stable action ID (`clip.checkout`, `clip.save`, `buffer.arm_all`, etc.).

- [ ] **Step 2: Register and rewire each button**

For **each** remaining `.clicked.connect(self._handler)` line, follow this mechanical pattern — one block per button, alongside the existing signal wiring:

```python
        register(Action(id="<category>.<verb>", name="<Display Name>",
                        category="<Category>",
                        callable=self._handler))
        some_button.clicked.connect(lambda: invoke("<category>.<verb>"))
```

Target action IDs for the known buttons (based on the grep audit of turntable_window.py):

| Line (approx) | Button | Action ID | Category | Display Name | Repeat policy |
|---|---|---|---|---|---|
| 297 | `self.out_btn` | `clip.checkout` | Clip | Checkout | ignore_repeat |
| 301 | `save_btn` | `clip.save` | Clip | Save Clip | ignore_repeat |
| 307 | `self.buffer_controls[0]` | `buffer.flush_active` | Buffer | Flush Active Buffer | ignore_repeat |
| 308 | `self.buffer_controls[1]` | `buffer.prev` | Buffer | Previous Buffer | fire |
| 311 | `self.buffer_controls[2]` | `buffer.step_back` | Buffer | Step Back | fire |
| 314 | `self.buffer_controls[3]` | `buffer.step_forward` | Buffer | Step Forward | fire |
| 317 | `self.buffer_controls[4]` | `buffer.pause` | Buffer | Pause Buffer | ignore_repeat |
| 325 | `self.clip_controls[1]` | `clip.loop` | Clip | Loop Clip | ignore_repeat |
| 328 | `self.clip_controls[2]` | `clip.step_back` | Clip | Step Back | fire |
| 331 | `self.clip_controls[3]` | `clip.step_forward` | Clip | Step Forward | fire |
| 334 | `self.clip_controls[4]` | `clip.save_as` | Clip | Save As… | ignore_repeat |
| 349 | `self.nav_bar.arm_all_btn` | `buffer.arm_all` | Buffer | Arm All Sources | ignore_repeat |
| 350 | `self.nav_bar.add_source_btn` | `buffer.add_source` | Buffer | Add Source | ignore_repeat |

**Note:** the names for `buffer_controls[1..4]` and `clip_controls[1..4]` in the table above are reasonable guesses from the spec's layout diagram (`FLUSH - ◀ ▶ + PAUSE` and `LOOP PLAY ◀ - + ▶ SAVE`). Before committing, open `flashback_sampler/app/widgets/center_bridge.py` (or wherever `buffer_controls`/`clip_controls` are built) and **confirm the actual button order**. If it differs, update the action IDs/names to match what the buttons actually do.

- [ ] **Step 3: Smoke test**

Run: `python -m flashback_sampler`
Expected:
- All previously-working buttons still work
- Settings → Keybindings dialog shows Transport, Clip, and Buffer categories with all registered actions
- Bind a key to some action via the dialog, hit OK, then exercise it — action fires

- [ ] **Step 4: Run full test suite**

Run: `pytest`
Expected: All existing tests + new `tests/unit/input/*` tests pass.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/turntable_window.py
git commit -m "refactor(app): migrate remaining buttons to action registry"
```

---

## Task 19: End-to-end verification and cleanup

**Files:**
- (Possibly) Modify: `flashback_sampler/app/turntable_window.py`

- [ ] **Step 1: Full-suite green**

Run: `pytest -q`
Expected: All tests pass. No Qt warnings, no import errors. If a test references a handler that was renamed or removed, fix it.

- [ ] **Step 2: Manual keybinding round-trip**

Start the app, open Settings → Keybindings. For a chosen action (e.g. `buffer.flush_active`):

1. Click Rebind, press `F13`, click OK on the modal.
2. Click OK on the dialog.
3. Plug in the knob (or press F13 on an external keyboard).
4. Verify the action fires.

Then re-open the dialog and click the × next to `buffer.flush_active`, OK. Press F13. Expected: nothing happens.

Then use Reset all to defaults, OK. Expected: any default bindings re-apply.

- [ ] **Step 3: Verify persistence across restart**

1. Bind some action to a distinctive key (e.g. `F24`), OK.
2. Close the app.
3. Verify `%APPDATA%\flashback-sampler\bindings.json` exists and contains your binding.
4. Restart the app, open Settings → Keybindings — your binding is preserved.

- [ ] **Step 4: Commit anything that shook out**

If Steps 1–3 surfaced follow-up fixes:

```bash
git add -u
git commit -m "fix(input): <what was fixed>"
```

If everything worked without changes, no commit is needed.

- [ ] **Step 5: Update project memory**

The keybinding engine is now load-bearing for the app. Add a concise memory entry so future sessions know it exists.

Create `C:\Users\user\.claude\projects\C--Users-user-Documents-dev\memory\project_flashback_keybinding_engine.md` and a pointer in `MEMORY.md`. Content:

```markdown
---
name: Flashback keybinding engine
description: Action + binding engine at flashback_sampler/input/ — Qt-independent core, Settings → Keybindings dialog, platformdirs JSON persistence
type: project
---

Central action registry and binding engine for flashback-sampler. Every bindable UI operation is a registered `Action` with a stable string ID (e.g. `transport.play_clip`). Bindings are JSON deltas at `%APPDATA%/flashback-sampler/bindings.json` (platformdirs on other OSes).

Key modules:
- `flashback_sampler/input/core/` — pure-Python: InputEvent, Action + registry, BindingTable
- `flashback_sampler/input/sources/qt_keyboard.py` — Qt adapter
- `flashback_sampler/input/ui/settings_dialog.py` — Settings → Keybindings UI

**Why:** Architected with a pure-Python core so VST hosting and non-Qt contexts can swap the adapter layer. Extending with MIDI/HID/gamepad sources is a new file in `sources/` with no core changes.

**How to apply:** When adding new UI buttons/menu items, register them as actions (`register(Action(...))`) and invoke via `invoke("<id>")` rather than wiring `.clicked.connect(self._handler)` directly. This keeps the settings dialog accurate and the action rebindable.
```

Then append to `MEMORY.md`:

```
- [Flashback keybinding engine](project_flashback_keybinding_engine.md) — action registry + Settings → Keybindings dialog; register new UI actions, don't hardwire
```

---

## Summary checklist

Quick progress map:

- [ ] Task 1: Scaffold packages + platformdirs dep
- [ ] Task 2: InputEvent
- [ ] Task 3: Action dataclass
- [ ] Task 4: Action registry
- [ ] Task 5: invoke() with repeat policy
- [ ] Task 6: BindingTable bind/unbind/resolve
- [ ] Task 7: BindingTable reset methods
- [ ] Task 8: BindingTable persistence
- [ ] Task 9: BindingTable conflict detection
- [ ] Task 10: Core package re-exports
- [ ] Task 11: Qt keyboard adapter
- [ ] Task 12: Settings dialog skeleton
- [ ] Task 13: Settings dialog rebind flow
- [ ] Task 14: Settings dialog conflict/reset/filter
- [ ] Task 15: Settings dialog OK/Cancel persistence
- [ ] Task 16: Settings → Keybindings menu wiring
- [ ] Task 17: Migrate transport actions
- [ ] Task 18: Migrate remaining UI actions
- [ ] Task 19: End-to-end verification and memory update
