# Keybinding Engine — Design Spec

## Overview

A central action + binding system for flashback-sampler. Every user-invokable operation in the app is a registered `Action` identified by a stable string ID. A `BindingTable` maps input events (key sequences today; MIDI / HID / VST automation later) to those actions. A Qt-independent core makes the engine portable to future VST and non-Qt contexts.

Users can rebind every action through a **Settings → Keybindings** dialog, with reset-to-default and per-row clear. Defaults are declared at action registration; user overrides are persisted as JSON deltas in the platform user-config directory.

This replaces ad-hoc signal wiring (`button.clicked.connect(self._on_record)`) with dispatch through the action registry (`lambda: invoke("transport.record")`), which makes the app scriptable, remappable, and externally controllable.

## Goals

- Every UI action in the app is registered as a discoverable, rebindable `Action`.
- Keybindings are editable through an in-app settings dialog with the classic **click-to-rebind, listen-for-input, reset-default** flow.
- Repeat behavior (OS key autorepeat, rotary-encoder key-spam) works correctly by default and is configurable per-action.
- Engine core is pure Python — no Qt imports — so it ports cleanly to future VST and non-Qt contexts.
- Adding a new input source (MIDI, raw HID, gamepad, network) is an additive change with no core edits.
- Windows-first, but no Windows-specific primitives in the engine or storage layer.

## Non-goals (v1)

- **MIDI / raw HID / gamepad / network input sources.** Architecture accommodates them; implementation is deferred.
- **Continuous values.** `InputEvent.value` is reserved in the schema but unused for v1. Knob input arrives as discrete keyboard events via the vendor tool.
- **Command palette / fuzzy action search.** Action registry supports it trivially when wanted; UI is out of scope for v1.
- **Macros / action sequences.** One binding → one action for v1.
- **Per-widget focus contexts** beyond Qt's default `WindowShortcut` semantics. A deck-specific scrub binding firing regardless of which deck has focus is acceptable for v1.
- **VST plugin packaging.** Architecture anticipates it; actual VST3/AU build pipeline is a separate future project.

## Architecture

Three layers, lowest-to-highest:

```
flashback_sampler/input/
├── core/                 ← Pure Python. No Qt imports. Fully unit-testable.
│   ├── __init__.py
│   ├── events.py         ← InputEvent dataclass
│   ├── actions.py        ← Action dataclass, registry, invoke()
│   └── bindings.py       ← BindingTable: bind/unbind/resolve/save/load
├── sources/
│   ├── __init__.py
│   └── qt_keyboard.py    ← Qt adapter: QKeyEvent → InputEvent → dispatch
└── ui/
    ├── __init__.py
    └── settings_dialog.py ← The Settings → Keybindings dialog
```

**Why this shape:**

- **v1:** `qt_keyboard.py` is the only input source. Standalone app works end-to-end.
- **Future VST:** Add `sources/vst_automation.py` that produces `InputEvent`s from VST parameter changes. Core and actions unchanged. If the VST host eats keyboard events, `qt_keyboard.py` simply isn't wired; the engine still functions.
- **Multiplatform:** Core is pure Python; `qt_keyboard.py` is Qt-portable across Windows/Linux/Mac. Storage path resolution uses `platformdirs`.
- **Testing:** Core runs under plain `pytest` without `QApplication` or a display.

## Core abstractions

### `core/events.py`

```python
from dataclasses import dataclass
from typing import Literal

EventKind = Literal["press", "release", "value"]

@dataclass(frozen=True)
class InputEvent:
    source: str                # "keyboard" today; "midi" / "hid" / "vst" later
    kind: EventKind
    code: str                  # "F13", "Ctrl+Shift+F20"; later "cc:7", "note:36"
    value: float | None = None # None for discrete; reserved for continuous
    is_repeat: bool = False    # OS autorepeat or encoder-spam marker
```

### `core/actions.py`

```python
from dataclasses import dataclass
from typing import Callable, Literal

RepeatPolicy = Literal["fire", "ignore_repeat", "edge_only"]

@dataclass
class Action:
    id: str                               # "transport.record"
    name: str                             # "Record"
    category: str                         # "Transport"
    callable: Callable[[], None]
    default_binding: str | None = None    # e.g. "Ctrl+R"; None = unbound by default
    repeat_policy: RepeatPolicy = "fire"

# Module-level registry
def register(action: Action) -> None: ...
def invoke(action_id: str) -> None: ...
def get(action_id: str) -> Action | None: ...
def all_actions() -> list[Action]: ...
def clear_registry() -> None: ...         # test support
```

- `register` is called at app startup by each UI component that owns an action.
- Registering a duplicate ID raises — IDs must be globally unique.
- `invoke` looks up the action, respects its `repeat_policy`, then calls it.

### `core/bindings.py`

```python
class BindingTable:
    def __init__(self, storage_path: Path): ...
    def bind(self, event_code: str, action_id: str) -> None: ...
    def unbind(self, event_code: str) -> None: ...
    def resolve(self, event: InputEvent) -> str | None: ...  # returns action_id
    def reset_to_defaults(self) -> None: ...
    def reset_one(self, action_id: str) -> None: ...         # snap back to default
    def save(self) -> None: ...
    def load(self) -> None: ...
    def conflicts(self, event_code: str) -> str | None: ...  # returns existing action_id
```

- `resolve` converts an `InputEvent` to an action ID (or `None`) by matching `event.code` against the composed defaults-and-overrides map.
- `bind` emits the dialog's conflict-detection signal if the code is already bound.

## Repeat semantics

Both OS key autorepeat (holding Left arrow) and encoder-as-key-spam (knob turning rapidly) arrive as a stream of discrete `press` events. Qt's `QKeyEvent.isAutoRepeat()` populates `InputEvent.is_repeat` for the former; encoder events are real presses so `is_repeat=False`.

`Action.repeat_policy` controls behavior at `invoke` time:

| Policy            | Behavior                                                   | Example actions                 |
|-------------------|------------------------------------------------------------|---------------------------------|
| `"fire"` (default)| Every event invokes. Autorepeat and encoder spam both work.| scrub, step, shift-selection, ±  |
| `"ignore_repeat"` | Autorepeats suppressed; only non-repeat presses fire.      | play/pause, record toggle       |
| `"edge_only"`     | Fires only on transition to pressed; ignores all repeats.  | rare modal toggles              |

Knob rapid-fire is indistinguishable from a very fast human keypress and behaves identically — by design.

## Storage

### Location

Resolved via `platformdirs.user_config_dir("flashback-sampler")`:

- **Windows:** `%APPDATA%\flashback-sampler\bindings.json`
- **Linux:** `~/.config/flashback-sampler/bindings.json`
- **macOS:** `~/Library/Application Support/flashback-sampler/bindings.json`

`platformdirs` becomes a new dependency (lightweight, well-maintained, already transitively used by many of the app's deps).

### Format

Stores only **deltas from defaults**:

```json
{
  "version": 1,
  "bindings": {
    "transport.record": "Ctrl+Shift+R",
    "deck.left.scrub_forward": "F13",
    "transport.play": null
  }
}
```

- Missing entry → action uses its registered `default_binding`.
- `null` → user explicitly cleared the binding (distinct from "never customized"; prevents defaults from re-applying at load).
- `version` allows clean migration if the schema ever changes.

**Behavior:**

- Reset-to-default for a single action → remove the entry (falls back to default at next resolve).
- Reset-all-to-defaults → truncate the file to `{"version": 1, "bindings": {}}`.
- New actions added in a future release appear automatically with their declared defaults; existing user customizations are untouched.

## Settings dialog

**Menu path:** Settings → Keybindings.

### Layout

Three-column table grouped by `Action.category`, with a filter box and global reset:

```
┌───────────────────────────────────────────────────────────────┐
│ Filter: [____________]            [Reset all to defaults]     │
├───────────────────────────────────────────────────────────────┤
│ Transport                                                     │
│   Record            │ Ctrl+Shift+R        │ [Rebind] [×]      │
│   Play / Pause      │ Space               │ [Rebind] [×]      │
│   Capture Now       │ (unbound)           │ [Rebind] [×]      │
│ Deck · Left                                                   │
│   Scrub Forward     │ F13                 │ [Rebind] [×]      │
│   Scrub Back        │ F14                 │ [Rebind] [×]      │
│   Next Clip         │ (unbound)           │ [Rebind] [×]      │
│ Deck · Right                                                  │
│   Scrub Forward     │ F15                 │ [Rebind] [×]      │
│ ...                                                           │
├───────────────────────────────────────────────────────────────┤
│                                        [Cancel]   [OK]        │
└───────────────────────────────────────────────────────────────┘
```

### Interactions

- **[Rebind]** → opens a modal with "Press a key…". Uses Qt's native `QKeySequenceEdit` (supports chords, Esc cancels).
- **[×]** → clears the binding in the edit buffer (stores `null` on OK).
- **Reset all to defaults** → confirmation dialog, then resets edit buffer for every action.
- **Filter** → live filter by action name or category, case-insensitive.
- **Conflict handling:** if a newly-entered binding is already assigned, show an inline banner:
  > *"Ctrl+Shift+R is already bound to Record. [Replace] [Cancel]"*
  Replace clears the previous binding and assigns the new one.
- **Apply semantics:** OK writes changes to disk and updates the live `BindingTable`; Cancel discards the edit buffer. No auto-apply during the session.

## Context / focus

Use Qt's default `WindowShortcut` semantics: actions fire window-wide except when an editable widget (`QLineEdit`, `QTextEdit`) has focus. Qt handles this automatically for `QKeySequence`-registered shortcuts; our Qt adapter must match this behavior when forwarding raw key events.

Per-widget contexts (e.g., "scrub only when left deck has focus") are **deferred to v2**. If needed, add an optional `context: str | None` field to `Action` and filter at resolve time.

## Migration strategy

All existing UI actions in `main_window.py`, `turntable_window.py`, and `widgets/*.py` are converted to registered actions in a single pass — mechanical changes, small surface area per edit.

### Pattern

```python
# Before
self.record_button.clicked.connect(self._on_record)

# After
from flashback_sampler.input.core.actions import register, invoke, Action

register(Action(
    id="transport.record",
    name="Record",
    category="Transport",
    callable=self._on_record,
    default_binding=None,          # unbound by default for v1
    repeat_policy="ignore_repeat", # don't re-fire on held key
))
self.record_button.clicked.connect(lambda: invoke("transport.record"))
```

### Scope for v1

- Every existing button, menu item, and shortcut is registered.
- **`default_binding` is `None`** for all actions at first pass; user assigns keys through the settings dialog.
- A handful of obvious defaults may be declared inline (e.g. `Space` for play/pause) if the team wants them — no research needed, just common sense.
- Each UI class registers its own actions in `__init__` (necessary for bound-method callables like `self._on_record`). The plan will define a deterministic registration order by having `MainWindow` / `TurntableWindow` construct owning widgets in a fixed sequence before the `BindingTable` is loaded, so all actions exist by the time user overrides are resolved.

### Action ID namespace

Dotted hierarchy, enforced by convention (no runtime check beyond uniqueness):

- `transport.*` — play, stop, record, pause, capture
- `deck.left.*`, `deck.right.*` — deck-specific actions (scrub, next/prev clip, volume)
- `buffer.*` — source management (add source, arm all, flush)
- `clip.*` — clip management (save, loop, checkout)
- `app.*` — app-level (open settings, quit)

## Testing

### Core (pure Python, no Qt)

- `test_events.py` — `InputEvent` equality, hashing, frozen semantics.
- `test_actions.py` — register, duplicate-ID rejection, invoke dispatch, `repeat_policy` gating via mock calls.
- `test_bindings.py` — bind/unbind/resolve round-trips, defaults-vs-overrides layering, `null` entry semantics, conflict detection, save/load through a temp `Path`.

### Qt adapter

- `test_qt_keyboard.py` (under `pytest-qt` if adopted; otherwise a minimal adapter with no `QApplication`) — synthesize `QKeyEvent`s, verify correct `InputEvent` emitted, verify `isAutoRepeat()` flag propagates.

### Integration

- `test_settings_dialog.py` — open dialog, rebind an action, click OK, verify persisted JSON matches expected deltas.
- `test_migration.py` — smoke test confirming key app buttons still invoke via the registry after migration.

All core tests run with no audio hardware, no display, no Qt event loop.

## Dependencies added

- `platformdirs>=4.0` — cross-platform user-config directory resolution. Added to `dependencies` in `pyproject.toml`.

No other new deps. `QKeySequenceEdit` is built into PySide6.

## Open questions

None blocking implementation. Two deferrable decisions:

1. **Should a handful of common default bindings be declared at registration time** (Space = play/pause, Ctrl+R = record), or does v1 ship fully unbound with the user assigning everything via the dialog? Low-stakes either way; I'll propose 3–5 obvious defaults during plan review and the user can accept or blank them.
2. **Should the dialog support "Apply" (live update without closing)** in addition to OK/Cancel? Nice polish, but adds state management around dirty/undo. Default plan: OK/Cancel only for v1; revisit if friction emerges.

## Future work (explicitly out of scope)

- MIDI input source (`mido` + a `sources/midi.py` module).
- Raw HID input source (`hidapi` + `sources/hid.py`) — enables continuous knob values without going through keyboard.
- VST3 / AU source adapter.
- Command palette (Ctrl+Shift+P fuzzy-search dispatch UI).
- Macro / sequence bindings (one key → multiple actions).
- Per-widget focus contexts beyond Qt's window default.
- Export / import binding profiles (share configs between users).
