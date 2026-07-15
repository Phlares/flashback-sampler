# DAW Drag-Out + Hi-Res Sample Rates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drag a slice off either deck of Flashback straight onto a DAW track (Ableton Live 12 is the acceptance bench), from a persistent export pool, plus hi-res capture rates with an honest device probe.

**Architecture:** Render-at-drag-start: when a drag gesture crosses Qt's threshold on a `SelectableWaveform`, the slice is rendered from RAM to a WAV in the export pool, then offered as a standard OS file drag (`QDrag` + `file://` URL) that every DAW accepts. Core render logic is pure Python (`core/drag_export.py`); the Qt drag is a thin injectable seam (`app/drag_out.py`); `TurntableWindow` wires decks to both. Rates: extend the Add Source dialog's rate choices and probe deliverability via sounddevice (`check_input_settings` for inputs, WASAPI device mix-format rate for loopback), falling back with a notice instead of silently upsampling.

**Tech Stack:** Python 3.11+, PySide6, numpy, soundfile, sounddevice, pytest.

**Spec:** `docs/superpowers/specs/2026-07-15-daw-drag-and-hires-rates-design.md`. Two deliberate deviations found during codebase mapping, both spec-compatible in intent: (1) the Add Source dialog already *is* the spec's "custom row" (the named-preset ladder no longer exists in the dialog), so hi-res rates go into its `SAMPLE_RATE_CHOICES` rather than new `quality_presets.PRESETS` entries — 44.1k is already offered; (2) the loopback probe uses sounddevice's WASAPI host API (`default_samplerate` of the output device *is* its shared-mode mix-format rate under PortAudio/WASAPI) instead of new ctypes COM code.

## Global Constraints

- Windows is the only supported loopback platform; everything must keep running (and tests passing) on any platform via fakes.
- Internal audio dtype stays float32 everywhere.
- Export default is 32-bit float WAV (`subtype="FLOAT"`); user-selectable PCM_24 / PCM_16.
- Export pool default: `Path.home() / "Documents" / "flashback-sampler" / "exports"` (no captures-dir constant exists in code). Pool files are never auto-deleted except cancelled-drag cleanup of the just-rendered file.
- Drag filename: `<sanitized-source>_<YYYYMMDD-HHMMSS>_<len>s.wav`, collision-suffixed `_2`, `_3`, …
- New UI behavior on `SelectableWaveform`: press *inside* an existing selection arms a drag-out (it no longer starts a new selection there); Ctrl+press anywhere arms a full-clip drag-out. Edge-grab and press-outside-selection behavior unchanged.
- TDD: every task writes its failing test first. Test command: `python -m pytest tests/unit -q` (use `./.venv/Scripts/python.exe -m pytest` if bare `python` lacks deps).
- Commit after each task; branch `feat/daw-drag-out` (create via superpowers:using-git-worktrees at execution start).
- Repo conventions: root `conftest.py` stubs `QMessageBox`/`QFileDialog`/`QMenu` statics process-wide; app tests use a module-scoped `qapp` fixture (`QApplication.instance() or QApplication([])`).

---

### Task 1: `CheckoutManager.save()` — explicit subtype + `mark_saved`

Fixes the silent 16-bit downcast (libsndfile's WAV default is PCM_16) and lets the drag renderer write a file *without* flipping checkout state (the caller decides after the drop).

**Files:**
- Modify: `flashback_sampler/core/checkout.py` (save at ~line 274)
- Test: `tests/unit/test_checkout.py`

**Interfaces:**
- Produces: `CheckoutManager.save(checkout_id, target_path, fmt="WAV", trimmed=True, subtype=None, mark_saved=True) -> Path` — `subtype ∈ {"FLOAT","PCM_24","PCM_16"} | None`; `None` resolves to FLOAT for WAV, PCM_24 for FLAC; FLAC+FLOAT coerces to PCM_24 (FLAC has no float subtype). `CheckoutManager.mark_saved(checkout_id) -> None` sets `state="saved"` (raises `KeyError` on unknown id).

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_checkout.py`; it already imports `numpy as np`, `pytest`, `soundfile as sf`, `Path`, `AudioCircularBuffer`, `CheckoutManager`, and `tests.fixtures.sine_source.ramp_block`)

```python
def _mgr_with_checkout(tmp_path=None):
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 800, channels=1))
    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=0.5)
    return mgr, co


def test_save_wav_defaults_to_float32_subtype(tmp_path):
    mgr, co = _mgr_with_checkout()
    target = mgr.save(co.id, tmp_path / "clip.wav")
    assert sf.info(str(target)).subtype == "FLOAT"


def test_save_flac_defaults_to_pcm_24(tmp_path):
    mgr, co = _mgr_with_checkout()
    target = mgr.save(co.id, tmp_path / "clip.flac", fmt="FLAC")
    assert sf.info(str(target)).subtype == "PCM_24"


def test_save_flac_coerces_float_to_pcm_24(tmp_path):
    mgr, co = _mgr_with_checkout()
    target = mgr.save(co.id, tmp_path / "clip.flac", fmt="FLAC", subtype="FLOAT")
    assert sf.info(str(target)).subtype == "PCM_24"


def test_save_explicit_pcm_16(tmp_path):
    mgr, co = _mgr_with_checkout()
    target = mgr.save(co.id, tmp_path / "clip.wav", subtype="PCM_16")
    assert sf.info(str(target)).subtype == "PCM_16"


def test_save_rejects_unknown_subtype(tmp_path):
    mgr, co = _mgr_with_checkout()
    with pytest.raises(ValueError):
        mgr.save(co.id, tmp_path / "clip.wav", subtype="PCM_32_BANANA")


def test_save_mark_saved_false_leaves_state(tmp_path):
    mgr, co = _mgr_with_checkout()
    mgr.save(co.id, tmp_path / "clip.wav", mark_saved=False)
    assert mgr.get(co.id).state == "pending"


def test_mark_saved_sets_state():
    mgr, co = _mgr_with_checkout()
    mgr.mark_saved(co.id)
    assert mgr.get(co.id).state == "saved"


def test_mark_saved_unknown_id_raises():
    mgr, _ = _mgr_with_checkout()
    with pytest.raises(KeyError):
        mgr.mark_saved("nope")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_checkout.py -q`
Expected: the new tests FAIL (`TypeError: save() got an unexpected keyword argument 'subtype'`, `AttributeError: ... no attribute 'mark_saved'`, and the default-subtype test fails with `'PCM_16' != 'FLOAT'`).

- [ ] **Step 3: Implement**

In `core/checkout.py`, add below `CheckoutFormat` (line 26):

```python
CheckoutSubtype = Literal["FLOAT", "PCM_24", "PCM_16"]

# libsndfile's WAV default is PCM_16 — an explicit subtype keeps our
# float32 buffers bit-perfect on disk. FLAC has no float subtype.
_DEFAULT_SUBTYPE: dict[str, str] = {"WAV": "FLOAT", "FLAC": "PCM_24"}
_VALID_SUBTYPES: tuple[str, ...] = ("FLOAT", "PCM_24", "PCM_16")
```

Replace the `save` signature and the `sf.write` call:

```python
    def save(
        self,
        checkout_id: str,
        target_path: Path | str,
        fmt: CheckoutFormat = "WAV",
        trimmed: bool = True,
        subtype: CheckoutSubtype | None = None,
        mark_saved: bool = True,
    ) -> Path:
```

Inside, after the existing `fmt` validation:

```python
        if subtype is None:
            subtype = _DEFAULT_SUBTYPE[fmt]  # type: ignore[assignment]
        if subtype not in _VALID_SUBTYPES:
            raise ValueError(
                f"Unsupported subtype {subtype!r}; must be one of {_VALID_SUBTYPES}"
            )
        if fmt == "FLAC" and subtype == "FLOAT":
            subtype = "PCM_24"
```

Change the write call to `sf.write(str(target), audio, sr, format=fmt, subtype=subtype)` and guard the final state flip with `if mark_saved:`. Then add after `save`:

```python
    def mark_saved(self, checkout_id: str) -> None:
        """Flip a checkout to `saved` without writing anything — used by
        the drag-out flow, which renders first and only commits the state
        once the drop target has accepted the file."""
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            self._checkouts[checkout_id].state = "saved"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_checkout.py -q`
Expected: PASS (all, including pre-existing tests — none assert PCM_16 output; if one does, update it to FLOAT and note it in the commit message).

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/core/checkout.py tests/unit/test_checkout.py
git commit -m "fix(core): explicit save subtype (float32 default) + mark_saved seam"
```

---

### Task 2: `core/drag_export.py` — naming, collision, render

**Files:**
- Create: `flashback_sampler/core/drag_export.py`
- Test: `tests/unit/test_drag_export.py`

**Interfaces:**
- Consumes: `CheckoutManager.save(..., subtype=..., mark_saved=False)` and `CheckoutManager.get()` from Task 1.
- Produces: `sanitize_source_name(name: str) -> str`; `drag_filename(source_name: str, when: datetime, duration_s: float) -> str`; `resolve_collision(target: Path) -> Path`; `render_drag_file(manager, checkout_id, pool_dir, source_name, *, bit_depth="FLOAT", trimmed=True, now=None) -> Path` (does NOT mark saved).

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the drag-out export renderer (pure core, no Qt)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import soundfile as sf

from flashback_sampler.core.buffer import AudioCircularBuffer
from flashback_sampler.core.checkout import CheckoutManager
from flashback_sampler.core.drag_export import (
    drag_filename,
    render_drag_file,
    resolve_collision,
    sanitize_source_name,
)
from tests.fixtures.sine_source import ramp_block

WHEN = datetime(2026, 7, 15, 13, 5, 9)


def _mgr_with_checkout():
    buf = AudioCircularBuffer(duration_seconds=2.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 1500, channels=1))
    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=0.5)  # 500 samples = 0.5 s
    return mgr, co


def test_sanitize_source_name():
    assert sanitize_source_name("Speakers (Realtek)") == "speakers_realtek"
    assert sanitize_source_name("") == "source"
    assert sanitize_source_name("___") == "source"


def test_drag_filename_format():
    assert (
        drag_filename("My Deck", WHEN, 3.52)
        == "my_deck_20260715-130509_3.5s.wav"
    )


def test_resolve_collision_appends_suffix(tmp_path):
    target = tmp_path / "clip.wav"
    assert resolve_collision(target) == target
    target.write_bytes(b"")
    assert resolve_collision(target) == tmp_path / "clip_2.wav"
    (tmp_path / "clip_2.wav").write_bytes(b"")
    assert resolve_collision(target) == tmp_path / "clip_3.wav"


def test_render_drag_file_writes_wav_without_marking_saved(tmp_path):
    mgr, co = _mgr_with_checkout()
    path = render_drag_file(mgr, co.id, tmp_path, "Deck A", now=WHEN)
    assert path == tmp_path / "deck_a_20260715-130509_0.5s.wav"
    info = sf.info(str(path))
    assert info.subtype == "FLOAT"
    assert info.frames == 500
    assert mgr.get(co.id).state == "pending"


def test_render_drag_file_respects_trim_and_bit_depth(tmp_path):
    mgr, co = _mgr_with_checkout()
    co.trim_in_samples = 100
    co.trim_out_samples = 300
    path = render_drag_file(
        mgr, co.id, tmp_path, "Deck A", bit_depth="PCM_24", now=WHEN
    )
    info = sf.info(str(path))
    assert info.frames == 200
    assert info.subtype == "PCM_24"
    assert "_0.2s" in path.name


def test_render_drag_file_creates_pool_dir(tmp_path):
    mgr, co = _mgr_with_checkout()
    pool = tmp_path / "nested" / "exports"
    path = render_drag_file(mgr, co.id, pool, "x", now=WHEN)
    assert path.parent == pool and path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_drag_export.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'flashback_sampler.core.drag_export'`.

- [ ] **Step 3: Implement `flashback_sampler/core/drag_export.py`**

```python
"""
Render a checkout into the export pool for an OS drag-out.

Pure Python + soundfile — no Qt. The app layer decides when to render
(drag-start) and what to do afterward (mark saved on drop, delete on
cancel); this module only owns naming and the write itself.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .checkout import CheckoutManager, CheckoutSubtype

_MAX_COLLISION_SUFFIX = 999


def sanitize_source_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "").strip("_").lower()
    return base or "source"


def drag_filename(source_name: str, when: datetime, duration_s: float) -> str:
    return (
        f"{sanitize_source_name(source_name)}"
        f"_{when:%Y%m%d-%H%M%S}_{duration_s:.1f}s.wav"
    )


def resolve_collision(target: Path) -> Path:
    """Return `target`, or the first free `<stem>_N<suffix>` sibling."""
    if not target.exists():
        return target
    for i in range(2, _MAX_COLLISION_SUFFIX + 1):
        candidate = target.with_name(f"{target.stem}_{i}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"no free collision suffix for {target}")


def render_drag_file(
    manager: CheckoutManager,
    checkout_id: str,
    pool_dir: Path | str,
    source_name: str,
    *,
    bit_depth: CheckoutSubtype = "FLOAT",
    trimmed: bool = True,
    now: datetime | None = None,
) -> Path:
    """
    Write the checkout's (trimmed) audio to the export pool and return
    the path. Does NOT mark the checkout saved — the caller commits that
    only once the drop target has accepted the file.
    """
    co = manager.get(checkout_id)
    audio = co.trimmed_audio() if trimmed else co.audio
    duration_s = audio.shape[0] / co.sample_rate
    when = now or datetime.now()
    pool = Path(pool_dir)
    pool.mkdir(parents=True, exist_ok=True)
    target = resolve_collision(pool / drag_filename(source_name, when, duration_s))
    manager.save(
        checkout_id, target, fmt="WAV",
        trimmed=trimmed, subtype=bit_depth, mark_saved=False,
    )
    return target
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_drag_export.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/core/drag_export.py tests/unit/test_drag_export.py
git commit -m "feat(core): drag-out export renderer — pool naming, collisions, no state flip"
```

---

### Task 3: Config — export pool dir + bit depth

**Files:**
- Modify: `flashback_sampler/app/config.py` (append after the global-hotkeys block, line 92)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `EXPORT_POOL_DIR_KEY`, `EXPORT_BIT_DEPTH_KEY`, `VALID_EXPORT_BIT_DEPTHS = ("FLOAT","PCM_24","PCM_16")`, `default_export_pool_dir() -> Path`, `load_export_pool_dir(path=None) -> Path`, `save_export_pool_dir(pool_dir, path=None)`, `load_export_bit_depth(path=None) -> str`, `save_export_bit_depth(depth, path=None)` (raises `ValueError` on invalid depth).

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_config.py`, following its existing style of passing an explicit `tmp_path / "config.json"`)

```python
def test_export_pool_dir_defaults_to_documents(tmp_path):
    from pathlib import Path
    from flashback_sampler.app.config import (
        default_export_pool_dir,
        load_export_pool_dir,
        save_export_pool_dir,
    )

    cfg = tmp_path / "config.json"
    assert load_export_pool_dir(cfg) == default_export_pool_dir()
    assert default_export_pool_dir() == (
        Path.home() / "Documents" / "flashback-sampler" / "exports"
    )
    save_export_pool_dir(tmp_path / "pool", cfg)
    assert load_export_pool_dir(cfg) == tmp_path / "pool"


def test_export_bit_depth_roundtrip_and_validation(tmp_path):
    import pytest
    from flashback_sampler.app.config import (
        load_export_bit_depth,
        save_export_bit_depth,
    )

    cfg = tmp_path / "config.json"
    assert load_export_bit_depth(cfg) == "FLOAT"
    save_export_bit_depth("PCM_24", cfg)
    assert load_export_bit_depth(cfg) == "PCM_24"
    with pytest.raises(ValueError):
        save_export_bit_depth("MP3", cfg)


def test_export_bit_depth_ignores_garbage_in_file(tmp_path):
    from flashback_sampler.app.config import (
        EXPORT_BIT_DEPTH_KEY,
        load_export_bit_depth,
    )
    from flashback_sampler.app.config import save_config

    cfg = tmp_path / "config.json"
    save_config({EXPORT_BIT_DEPTH_KEY: "banana"}, cfg)
    assert load_export_bit_depth(cfg) == "FLOAT"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_config.py -q`
Expected: FAIL with `ImportError: cannot import name 'default_export_pool_dir'`.

- [ ] **Step 3: Implement** (append to `app/config.py`)

```python
EXPORT_POOL_DIR_KEY = "export_pool_dir"
EXPORT_BIT_DEPTH_KEY = "export_bit_depth"
VALID_EXPORT_BIT_DEPTHS = ("FLOAT", "PCM_24", "PCM_16")


def default_export_pool_dir() -> Path:
    """Where drag-exported slices land by default — user-visible, since
    the pool doubles as a sample bank (DAW projects reference these
    files in place; never auto-clean the pool)."""
    return Path.home() / "Documents" / "flashback-sampler" / "exports"


def load_export_pool_dir(path: Path | None = None) -> Path:
    raw = get_pref(EXPORT_POOL_DIR_KEY, "", path)
    return Path(raw) if raw else default_export_pool_dir()


def save_export_pool_dir(pool_dir: Path | str, path: Path | None = None) -> None:
    set_pref(EXPORT_POOL_DIR_KEY, str(pool_dir), path)


def load_export_bit_depth(path: Path | None = None) -> str:
    raw = get_pref(EXPORT_BIT_DEPTH_KEY, "FLOAT", path)
    return raw if raw in VALID_EXPORT_BIT_DEPTHS else "FLOAT"


def save_export_bit_depth(depth: str, path: Path | None = None) -> None:
    if depth not in VALID_EXPORT_BIT_DEPTHS:
        raise ValueError(
            f"invalid export bit depth {depth!r}; "
            f"must be one of {VALID_EXPORT_BIT_DEPTHS}"
        )
    set_pref(EXPORT_BIT_DEPTH_KEY, depth, path)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/config.py tests/unit/test_config.py
git commit -m "feat(config): export pool dir + export bit depth preferences"
```

---

### Task 4: `app/drag_out.py` — the Qt file-drag seam

**Files:**
- Create: `flashback_sampler/app/drag_out.py`
- Test: `tests/unit/test_drag_out.py`

**Interfaces:**
- Produces: `build_file_drag_mime(file_path) -> QMimeData`; `perform_file_drag(source_widget, file_path, exec_fn=None) -> bool` — True when the drop was accepted (any action except `Qt.IgnoreAction`). `exec_fn: Callable[[QDrag], Qt.DropAction]` is the injectable blocking-exec seam for tests; production leaves it None.

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the OS file-drag seam (exec injected, no real drag loop)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from flashback_sampler.app.drag_out import build_file_drag_mime, perform_file_drag


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_build_file_drag_mime_carries_local_file_url(qapp, tmp_path):
    f = tmp_path / "slice.wav"
    f.write_bytes(b"")
    mime = build_file_drag_mime(f)
    urls = mime.urls()
    assert len(urls) == 1
    assert urls[0].isLocalFile()
    assert Path(urls[0].toLocalFile()) == f.resolve()


def test_perform_file_drag_true_on_copy(qapp, tmp_path):
    f = tmp_path / "slice.wav"
    f.write_bytes(b"")
    w = QWidget()
    seen = {}

    def fake_exec(drag):
        seen["urls"] = drag.mimeData().urls()
        return Qt.CopyAction

    assert perform_file_drag(w, f, exec_fn=fake_exec) is True
    assert len(seen["urls"]) == 1


def test_perform_file_drag_false_on_ignore(qapp, tmp_path):
    f = tmp_path / "slice.wav"
    f.write_bytes(b"")
    assert perform_file_drag(QWidget(), f, exec_fn=lambda d: Qt.IgnoreAction) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_drag_out.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'flashback_sampler.app.drag_out'`.

- [ ] **Step 3: Implement `flashback_sampler/app/drag_out.py`**

```python
"""
OS file drag-out — offer an already-rendered file to any drop target
(DAW track, Explorer, ...) as a standard CF_HDROP-style file drag.

Kept as a tiny seam so the blocking QDrag.exec loop is injectable in
tests; everything above this (what to render, what "accepted" means for
checkout state) lives in the window controller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QMimeData, QUrl, Qt
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QWidget


def build_file_drag_mime(file_path: Path | str) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(Path(file_path).resolve()))])
    return mime


def perform_file_drag(
    source_widget: QWidget,
    file_path: Path | str,
    exec_fn: Optional[Callable[[QDrag], Qt.DropAction]] = None,
) -> bool:
    """
    Run a blocking OS drag offering `file_path`. Returns True when the
    drop target accepted the file (any action except IgnoreAction —
    some targets report Move/Link even though the file stays put).
    """
    drag = QDrag(source_widget)
    drag.setMimeData(build_file_drag_mime(file_path))
    action = drag.exec(Qt.CopyAction) if exec_fn is None else exec_fn(drag)
    return action != Qt.IgnoreAction
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_drag_out.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/drag_out.py tests/unit/test_drag_out.py
git commit -m "feat(app): injectable OS file-drag seam"
```

---

### Task 5: `SelectableWaveform` — drag-out gesture

Press *inside* an existing selection (not on an edge) arms a selection drag-out; Ctrl+press anywhere arms a full-clip drag-out. Crossing `QApplication.startDragDistance()` emits the signal (one-shot). A press-inside-selection that never crosses the threshold is now a no-op click (previously it started a new selection — starting a new selection inside an existing one now requires clearing first via double-click or clicking outside).

**Files:**
- Modify: `flashback_sampler/app/widgets/selectable_waveform.py`
- Test: `tests/unit/test_selectable_waveform.py`

**Interfaces:**
- Produces: signals `dragOutRequested = Signal(float, float)` (start_frac, end_frac) and `dragFullClipRequested = Signal()`; `is_user_interacting()` also True while a drag-out is armed.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_selectable_waveform.py`; it already has `qapp` and `_new_wave`. Event plumbing is normally covered by the window smoke test, but the gesture state machine is exactly the kind of logic this file unit-tests — drive it with synthesized QMouseEvents.)

```python
from PySide6.QtCore import QEvent, QPointF
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication as _QApp


def _mouse_ev(kind, x, y=100.0, button=Qt.LeftButton,
              buttons=Qt.LeftButton, mods=Qt.NoModifier):
    return QMouseEvent(
        kind, QPointF(x, y), QPointF(x, y), button, buttons, mods
    )


def _press(w, x, mods=Qt.NoModifier):
    w.mousePressEvent(_mouse_ev(QEvent.MouseButtonPress, x, mods=mods))


def _move(w, x, mods=Qt.NoModifier):
    w.mouseMoveEvent(_mouse_ev(
        QEvent.MouseMove, x, button=Qt.NoButton, mods=mods
    ))


def _release(w, x):
    w.mouseReleaseEvent(_mouse_ev(
        QEvent.MouseButtonRelease, x, buttons=Qt.NoButton
    ))


def test_press_inside_selection_and_drag_emits_drag_out(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.25, 0.75)
    got = []
    w.dragOutRequested.connect(lambda s, e: got.append((s, e)))
    _press(w, 500)  # mid-selection, far from both edges
    _move(w, 500 + _QApp.startDragDistance() + 1)
    assert got == [(0.25, 0.75)]
    # selection untouched — the gesture must not repaint the band
    assert w.manual_selection() == (0.25, 0.75)


def test_press_inside_selection_without_move_is_a_noop_click(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.25, 0.75)
    got = []
    w.dragOutRequested.connect(lambda s, e: got.append((s, e)))
    _press(w, 500)
    _release(w, 500)
    assert got == []
    assert w.manual_selection() == (0.25, 0.75)


def test_drag_out_is_one_shot(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.25, 0.75)
    got = []
    w.dragOutRequested.connect(lambda s, e: got.append((s, e)))
    _press(w, 500)
    far = 500 + _QApp.startDragDistance() + 1
    _move(w, far)
    _move(w, far + 50)
    assert len(got) == 1


def test_ctrl_press_and_drag_emits_full_clip(qapp):
    w = _new_wave(qapp, width=1012)
    got = []
    w.dragFullClipRequested.connect(lambda: got.append(True))
    _press(w, 300, mods=Qt.ControlModifier)
    _move(w, 300 + _QApp.startDragDistance() + 1, mods=Qt.ControlModifier)
    assert got == [True]


def test_press_outside_selection_still_paints_new_selection(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.6, 0.8)
    _press(w, 100)  # well outside, not near an edge
    _move(w, 200)
    _release(w, 200)
    sel = w.manual_selection()
    assert sel is not None
    assert sel[0] < 0.25 and sel[1] < 0.25


def test_edge_grab_still_beats_drag_out(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.25, 0.75)
    inner_x, inner_w = w._inner_bounds()
    got = []
    w.dragOutRequested.connect(lambda s, e: got.append((s, e)))
    _press(w, inner_x + 0.25 * inner_w)  # exactly on the start edge
    assert w._dragging_edge == "start"
    assert got == []


def test_is_user_interacting_while_drag_out_armed(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.25, 0.75)
    _press(w, 500)
    assert w.is_user_interacting() is True
    _release(w, 500)
    assert w.is_user_interacting() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_selectable_waveform.py -q`
Expected: new tests FAIL (`AttributeError: ... has no attribute 'dragOutRequested'`; the noop-click test fails because press-inside currently starts a new selection).

- [ ] **Step 3: Implement**

In `selectable_waveform.py`: add `QApplication` to the imports (`from PySide6.QtWidgets import QApplication`). Add the signals below `contextMenuRequested` (line 48):

```python
    # Drag-out: the user is pulling a slice OUT of the app (OS file drag).
    # "selection" mode reports the armed selection's fracs; "full" (Ctrl)
    # asks the host for the whole clip regardless of selection.
    dragOutRequested = Signal(float, float)  # start_frac, end_frac
    dragFullClipRequested = Signal()
```

In `__init__`, after `self._dragging_edge`:

```python
        self._drag_out_origin: QPointF | None = None
        self._drag_out_mode: str | None = None  # "selection" | "full"
```

Add a helper next to `_edge_at`:

```python
    def _frac_inside_selection(self, frac: float) -> bool:
        if not self.has_manual_selection():
            return False
        return float(self._manual_start) < frac < float(self._manual_end)
```

Update `is_user_interacting` to also return True while armed:

```python
        return (
            self._is_dragging
            or self._dragging_edge is not None
            or self._drag_out_origin is not None
        )
```

In `mousePressEvent`, inside the LeftButton branch: FIRST check Ctrl (before the edge check), and add the inside-selection arm between the edge grab and the new-selection fallback:

```python
        if ev.button() == Qt.LeftButton:
            # Priority 1: Ctrl+press arms a full-clip drag-out
            if ev.modifiers() & Qt.ControlModifier:
                self._drag_out_origin = ev.position()
                self._drag_out_mode = "full"
                ev.accept()
                return
            # Priority 2: grab an existing mark edge and drag it
            edge = self._edge_at(ev.position().x())
            if edge is not None:
                # (existing body, unchanged:)
                self._dragging_edge = edge
                self._is_dragging = False
                self._drag_anchor = None
                self.setCursor(Qt.SizeHorCursor)
                ev.accept()
                return
            # Priority 3: press inside the selection arms a drag-out
            if self._frac_inside_selection(self._pos_frac(ev.position().x())):
                self._drag_out_origin = ev.position()
                self._drag_out_mode = "selection"
                ev.accept()
                return
            # Priority 4: begin a new selection drag (existing body)
```

At the TOP of `mouseMoveEvent` (before the edge-drag block):

```python
        # Armed drag-out: fire once past the OS drag threshold. The host
        # runs a blocking QDrag from the signal handler, so disarm first.
        if self._drag_out_origin is not None:
            delta = ev.position() - self._drag_out_origin
            if delta.manhattanLength() >= QApplication.startDragDistance():
                mode = self._drag_out_mode
                self._drag_out_origin = None
                self._drag_out_mode = None
                if mode == "full":
                    self.dragFullClipRequested.emit()
                elif self.has_manual_selection():
                    self.dragOutRequested.emit(
                        float(self._manual_start), float(self._manual_end)
                    )
            ev.accept()
            return
```

At the top of the LeftButton branch of `mouseReleaseEvent` (before the edge-drag finish):

```python
            # An armed drag-out that never crossed the threshold is a
            # plain click — disarm and leave the selection untouched.
            if self._drag_out_origin is not None:
                self._drag_out_origin = None
                self._drag_out_mode = None
                ev.accept()
                return
```

Also update the class docstring's bullet list to document both gestures.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_selectable_waveform.py -q`
Expected: PASS (all, old and new).

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/widgets/selectable_waveform.py tests/unit/test_selectable_waveform.py
git commit -m "feat(widgets): drag-out gesture — press-in-selection + Ctrl full-clip"
```

---

### Task 6: Window wiring — clip deck (right) drag-out

**Files:**
- Modify: `flashback_sampler/app/turntable_window.py` (imports; `__init__` pref load; connections in `_wire_selection_sync` ~line 572; new handlers near `_save_current_clip` ~line 1104)
- Test: `tests/unit/test_turntable_window.py`

**Interfaces:**
- Consumes: `render_drag_file` (Task 2), `perform_file_drag` (Task 4), `load_export_pool_dir`/`load_export_bit_depth` (Task 3), `mark_saved` (Task 1), signals (Task 5).
- Produces: `TurntableWindow._on_clip_drag_out(start_frac, end_frac)`, `._on_clip_drag_full()`, `._drag_current_clip(trimmed: bool)`, `._render_for_drag(slot, co, trimmed) -> Path | None`, attrs `._export_pool_dir: Path`, `._export_bit_depth: str` (Task 7 and 8 build on all of these).

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_turntable_window.py`; reuse its existing `qapp`/`state` fixtures and TurntableWindow construction pattern — mirror how existing tests in that file build and tear down the window)

```python
def _write_one_second(state):
    import numpy as np
    state.active_slot.buffer.write(
        np.zeros((state.active_slot.buffer.sample_rate, 2), dtype=np.float32)
    )


def test_clip_drag_full_exports_and_marks_saved(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.active_slot.checkout_manager
        co = mgr.create(duration_s=0.5)
        win._refresh_clip_side(auto_select_newest=True)
        win._export_pool_dir = tmp_path
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: True,
        )
        win._on_clip_drag_full()
        assert mgr.get(co.id).state == "saved"
        assert len(list(tmp_path.glob("*.wav"))) == 1
    finally:
        win.close()


def test_clip_drag_cancel_deletes_file_and_keeps_clip(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.active_slot.checkout_manager
        co = mgr.create(duration_s=0.5)
        win._refresh_clip_side(auto_select_newest=True)
        win._export_pool_dir = tmp_path
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: False,
        )
        win._on_clip_drag_full()
        assert mgr.get(co.id).state == "pending"
        assert list(tmp_path.glob("*.wav")) == []
    finally:
        win.close()


def test_clip_drag_out_uses_trimmed_range(qapp, state, tmp_path, monkeypatch):
    import soundfile as sf
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.active_slot.checkout_manager
        co = mgr.create(duration_s=0.5)
        win._refresh_clip_side(auto_select_newest=True)
        n = co.audio.shape[0]
        co.trim_in_samples = n // 4
        co.trim_out_samples = n // 2
        win._export_pool_dir = tmp_path
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: True,
        )
        win._on_clip_drag_out(0.25, 0.5)
        files = list(tmp_path.glob("*.wav"))
        assert len(files) == 1
        assert sf.info(str(files[0])).frames == n // 2 - n // 4
    finally:
        win.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_turntable_window.py -q`
Expected: new tests FAIL with `AttributeError: 'TurntableWindow' object has no attribute '_on_clip_drag_full'`.

- [ ] **Step 3: Implement**

Imports in `turntable_window.py`:

```python
from flashback_sampler.app.config import (
    load_export_bit_depth,
    load_export_pool_dir,
)
from flashback_sampler.app.drag_out import perform_file_drag
from flashback_sampler.core.drag_export import render_drag_file
```

(Merge with the existing `from flashback_sampler.app.config import ...` import if there is one.) In `__init__`, next to where the other persisted prefs are loaded:

```python
        self._export_pool_dir: Path = load_export_pool_dir()
        self._export_bit_depth: str = load_export_bit_depth()
```

In `_wire_selection_sync`, next to the existing waveform signal connections (~line 572):

```python
        self.clip_panel.waveform.dragOutRequested.connect(self._on_clip_drag_out)
        self.clip_panel.waveform.dragFullClipRequested.connect(self._on_clip_drag_full)
```

New methods (place after `_save_current_clip`):

```python
    def _render_for_drag(self, slot, co, trimmed: bool):
        """Render `co` into the export pool; returns the path or None on
        failure (already reported to the user)."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            return render_drag_file(
                slot.checkout_manager,
                co.id,
                self._export_pool_dir,
                slot.name,
                bit_depth=self._export_bit_depth,
                trimmed=trimmed,
            )
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return None
        finally:
            QApplication.restoreOverrideCursor()

    def _on_clip_drag_out(self, start_frac: float, end_frac: float) -> None:
        # The clip selection IS the trim (kept in sync by on_clip_sel),
        # so dragging the band exports the trimmed range.
        self._drag_current_clip(trimmed=True)

    def _on_clip_drag_full(self) -> None:
        self._drag_current_clip(trimmed=False)

    def _drag_current_clip(self, trimmed: bool) -> None:
        co = self._currently_displayed_checkout()
        if co is None:
            return
        slot = self._state.active_slot
        path = self._render_for_drag(slot, co, trimmed)
        if path is None:
            return
        if perform_file_drag(self.clip_panel.waveform, path):
            slot.checkout_manager.mark_saved(co.id)
            self.statusBar().showMessage(f"Exported {path.name}", 4000)
            self._refresh_clip_side()
        else:
            path.unlink(missing_ok=True)
```

(`QApplication`, `Qt`, `QMessageBox`, and `Path` are already imported in this module; verify and add any that aren't.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_turntable_window.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/turntable_window.py tests/unit/test_turntable_window.py
git commit -m "feat(app): clip-deck drag-out — trimmed band drag + Ctrl full-clip"
```

---

### Task 7: Window wiring — buffer deck (left) drag-out

**Files:**
- Modify: `flashback_sampler/app/turntable_window.py`
- Test: `tests/unit/test_turntable_window.py`

**Interfaces:**
- Consumes: everything Task 6 produced; `CheckoutManager.create_from_abs_range` (existing); the window's `_buffer_sel_abs` / `_buffer_sel_mode` state (set by `on_buffer_sel` in `_wire_selection_sync`).
- Produces: `TurntableWindow._on_buffer_drag_out(start_frac, end_frac)` — implicit checkout → render → drag; keeps + marks saved on accept (sample-bank UX), discards checkout + deletes file on cancel.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_turntable_window.py`)

```python
def test_buffer_drag_out_persists_saved_checkout_on_accept(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        sr = state.active_slot.buffer.sample_rate
        win._export_pool_dir = tmp_path
        win._buffer_sel_abs = (0, sr // 2)
        win._buffer_sel_mode = "user"
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: True,
        )
        win._on_buffer_drag_out(0.0, 0.5)
        cos = state.active_slot.checkout_manager.list()
        assert len(cos) == 1
        assert cos[0].state == "saved"
        assert len(list(tmp_path.glob("*.wav"))) == 1
    finally:
        win.close()


def test_buffer_drag_out_cancel_discards_checkout_and_file(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        sr = state.active_slot.buffer.sample_rate
        win._export_pool_dir = tmp_path
        win._buffer_sel_abs = (0, sr // 2)
        win._buffer_sel_mode = "user"
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: False,
        )
        win._on_buffer_drag_out(0.0, 0.5)
        assert state.active_slot.checkout_manager.list() == []
        assert list(tmp_path.glob("*.wav")) == []
    finally:
        win.close()


def test_buffer_drag_out_without_user_selection_is_noop(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        win._export_pool_dir = tmp_path
        win._buffer_sel_mode = "default"
        called = []
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: called.append(path) or True,
        )
        win._on_buffer_drag_out(0.0, 0.5)
        assert called == []
        assert state.active_slot.checkout_manager.list() == []
    finally:
        win.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_turntable_window.py -q`
Expected: new tests FAIL with `AttributeError: ... no attribute '_on_buffer_drag_out'`.

- [ ] **Step 3: Implement**

Connection in `_wire_selection_sync`, next to the Task 6 connections:

```python
        self.buffer_panel.waveform.dragOutRequested.connect(self._on_buffer_drag_out)
```

(The buffer deck deliberately gets no `dragFullClipRequested` connection — "full clip" has no meaning on a rolling ring.) New method after `_drag_current_clip`:

```python
    def _on_buffer_drag_out(self, start_frac: float, end_frac: float) -> None:
        """Snipe the current buffer selection straight out of the app:
        implicit checkout → render → OS drag. On accept the checkout
        stays on the clip deck as `saved` (the pool + deck form the
        sample bank); on cancel it is discarded."""
        slot = self._state.active_slot
        sel_abs = getattr(self, "_buffer_sel_abs", None)
        if getattr(self, "_buffer_sel_mode", None) != "user" or sel_abs is None:
            return
        try:
            co = slot.checkout_manager.create_from_abs_range(*sel_abs)
        except (RuntimeError, ValueError) as e:
            self.statusBar().showMessage(f"Drag-out failed: {e}", 4000)
            return
        path = self._render_for_drag(slot, co, trimmed=True)
        if path is None:
            slot.checkout_manager.discard(co.id)
            return
        if perform_file_drag(self.buffer_panel.waveform, path):
            slot.checkout_manager.mark_saved(co.id)
            self._refresh_clip_side(auto_select_newest=True)
            self.statusBar().showMessage(f"Exported {path.name}", 4000)
        else:
            slot.checkout_manager.discard(co.id)
            path.unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_turntable_window.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/turntable_window.py tests/unit/test_turntable_window.py
git commit -m "feat(app): buffer-deck drag-out — implicit checkout, sample-bank persist"
```

---

### Task 8: Preferences — Export section

**Files:**
- Modify: `flashback_sampler/app/preferences_dialog.py`
- Modify: `flashback_sampler/app/turntable_window.py` (`_open_preferences_dialog` ~line 510; new setters near `_set_global_hotkeys_enabled` ~line 504)
- Test: `tests/unit/test_preferences_dialog.py`, `tests/unit/test_turntable_window.py`

**Interfaces:**
- Consumes: `save_export_pool_dir`/`save_export_bit_depth` (Task 3); window attrs `_export_pool_dir`/`_export_bit_depth` (Task 6).
- Produces: `PreferencesDialog.__init__` gains keyword-only params `export_pool_dir: str = ""`, `on_export_pool_dir_changed: Callable[[str], None] | None = None`, `export_bit_depth: str = "FLOAT"`, `on_export_bit_depth_changed: Callable[[str], None] | None = None`; widgets `self.export_dir_edit` (read-only QLineEdit), `self.export_dir_btn` (QPushButton "Browse…"), `self.export_depth_combo` (QComboBox, itemData FLOAT/PCM_24/PCM_16). Window methods `_set_export_pool_dir(path_str)`, `_set_export_bit_depth(depth)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_preferences_dialog.py`, following its existing construction style — it builds the dialog with the keyword args and asserts on widgets/callbacks)

```python
def test_export_section_reflects_initial_values(qapp):
    dlg = PreferencesDialog(
        show_notifications=True,
        on_notifications_changed=lambda v: None,
        export_pool_dir="D:/pool",
        export_bit_depth="PCM_24",
    )
    assert dlg.export_dir_edit.text() == "D:/pool"
    assert dlg.export_dir_edit.isReadOnly()
    assert dlg.export_depth_combo.currentData() == "PCM_24"


def test_export_depth_change_fires_callback(qapp):
    got = []
    dlg = PreferencesDialog(
        show_notifications=True,
        on_notifications_changed=lambda v: None,
        export_bit_depth="FLOAT",
        on_export_bit_depth_changed=got.append,
    )
    idx = dlg.export_depth_combo.findData("PCM_16")
    dlg.export_depth_combo.setCurrentIndex(idx)
    assert got == ["PCM_16"]


def test_export_dir_browse_cancel_changes_nothing(qapp):
    # conftest stubs QFileDialog.getExistingDirectory to return "" (cancel)
    got = []
    dlg = PreferencesDialog(
        show_notifications=True,
        on_notifications_changed=lambda v: None,
        export_pool_dir="D:/pool",
        on_export_pool_dir_changed=got.append,
    )
    dlg.export_dir_btn.click()
    assert got == []
    assert dlg.export_dir_edit.text() == "D:/pool"
```

And in `tests/unit/test_turntable_window.py`:

```python
def test_set_export_prefs_persist_and_apply(qapp, state, tmp_path, monkeypatch):
    import flashback_sampler.app.turntable_window as tw
    saved = {}
    monkeypatch.setattr(
        tw, "save_export_pool_dir", lambda p: saved.__setitem__("dir", str(p))
    )
    monkeypatch.setattr(
        tw, "save_export_bit_depth", lambda d: saved.__setitem__("depth", d)
    )
    win = TurntableWindow(state)
    try:
        win._set_export_pool_dir(str(tmp_path / "pool"))
        win._set_export_bit_depth("PCM_24")
        assert win._export_pool_dir == tmp_path / "pool"
        assert win._export_bit_depth == "PCM_24"
        assert saved == {"dir": str(tmp_path / "pool"), "depth": "PCM_24"}
    finally:
        win.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_preferences_dialog.py tests/unit/test_turntable_window.py -q`
Expected: FAIL (`TypeError: unexpected keyword argument 'export_pool_dir'`; `AttributeError: ... no attribute '_set_export_pool_dir'`; note the window test also needs `save_export_pool_dir`/`save_export_bit_depth` importable in `turntable_window` — add them to the Task 6 config import).

- [ ] **Step 3: Implement**

`preferences_dialog.py` — add the keyword-only params from the Interfaces block to `__init__`, and add an Export section following the exact pattern of the existing sections (a `<b>Export</b>` QLabel added to the dialog's main layout, then the controls). Imports: add `QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QPushButton` to the existing QtWidgets import. Section code:

```python
        layout.addWidget(QLabel("<b>Export</b>"))
        dir_row = QHBoxLayout()
        self.export_dir_edit = QLineEdit(export_pool_dir)
        self.export_dir_edit.setReadOnly(True)
        self.export_dir_btn = QPushButton("Browse…")

        def _pick_export_dir() -> None:
            chosen = QFileDialog.getExistingDirectory(
                self, "Export folder", self.export_dir_edit.text()
            )
            if not chosen:
                return
            self.export_dir_edit.setText(chosen)
            if on_export_pool_dir_changed is not None:
                on_export_pool_dir_changed(chosen)

        self.export_dir_btn.clicked.connect(_pick_export_dir)
        dir_row.addWidget(self.export_dir_edit, 1)
        dir_row.addWidget(self.export_dir_btn)
        layout.addLayout(dir_row)

        self.export_depth_combo = QComboBox()
        for label, value in (
            ("32-bit float", "FLOAT"),
            ("24-bit PCM", "PCM_24"),
            ("16-bit PCM", "PCM_16"),
        ):
            self.export_depth_combo.addItem(label, value)
        depth_idx = self.export_depth_combo.findData(export_bit_depth)
        if depth_idx >= 0:
            self.export_depth_combo.setCurrentIndex(depth_idx)

        def _depth_changed(_i: int) -> None:
            if on_export_bit_depth_changed is not None:
                on_export_bit_depth_changed(self.export_depth_combo.currentData())

        self.export_depth_combo.currentIndexChanged.connect(_depth_changed)
        layout.addWidget(self.export_depth_combo)
```

(`layout` = the dialog's main QVBoxLayout — match the actual variable name used by the existing sections.) `turntable_window.py` — extend the Task 6 config import with `save_export_bit_depth, save_export_pool_dir`; add setters next to `_set_global_hotkeys_enabled`:

```python
    def _set_export_pool_dir(self, path_str: str) -> None:
        self._export_pool_dir = Path(path_str)
        save_export_pool_dir(path_str)

    def _set_export_bit_depth(self, depth: str) -> None:
        self._export_bit_depth = depth
        save_export_bit_depth(depth)
```

and pass through in `_open_preferences_dialog`:

```python
            export_pool_dir=str(self._export_pool_dir),
            on_export_pool_dir_changed=self._set_export_pool_dir,
            export_bit_depth=self._export_bit_depth,
            on_export_bit_depth_changed=self._set_export_bit_depth,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_preferences_dialog.py tests/unit/test_turntable_window.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/preferences_dialog.py flashback_sampler/app/turntable_window.py tests/unit/test_preferences_dialog.py tests/unit/test_turntable_window.py
git commit -m "feat(app): Export preferences — pool folder + bit depth"
```

---

### Task 9: Hi-res rates in the Add Source dialog

**Files:**
- Modify: `flashback_sampler/app/add_source_dialog.py` (`SAMPLE_RATE_CHOICES`, line 47)
- Test: `tests/unit/test_add_source_dialog.py` (create)

**Interfaces:**
- Produces: `SAMPLE_RATE_CHOICES = (192000, 176400, 96000, 88200, 48000, 44100, 32000, 22050, 16000, 8000)`. Dialog still defaults to the `default_sample_rate` constructor arg (48000).

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for the Add Source dialog's sample-rate offerings."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.add_source_dialog import (
    SAMPLE_RATE_CHOICES,
    AddSourceDialog,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_hi_res_rates_offered_descending():
    assert SAMPLE_RATE_CHOICES == (
        192000, 176400, 96000, 88200, 48000, 44100, 32000, 22050, 16000, 8000
    )


def test_dialog_still_defaults_to_48k(qapp):
    dlg = AddSourceDialog(default_name="Deck 1")
    assert dlg.result_preset().sample_rate == 48000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_add_source_dialog.py -q`
Expected: the tuple test FAILS (hi-res entries missing). If `result_preset()` returns None before the dialog is accepted, adapt the second test to read the combo directly: `dlg._sr_combo.currentData() == 48000` — check the dialog source while implementing.

- [ ] **Step 3: Implement** — replace line 47:

```python
SAMPLE_RATE_CHOICES = (
    192000, 176400, 96000, 88200, 48000, 44100, 32000, 22050, 16000, 8000
)
```

Verify the combo selects `default_sample_rate` by value (not index) — if it uses `findData`, nothing else changes; if it assumes index 0 is the default, fix it to `findData(default_sample_rate)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_add_source_dialog.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/add_source_dialog.py tests/unit/test_add_source_dialog.py
git commit -m "feat(app): offer hi-res capture rates up to 192k"
```

---

### Task 10: Rate probe in `app/audio_devices.py`

**Files:**
- Modify: `flashback_sampler/app/audio_devices.py`
- Test: `tests/unit/test_audio_devices.py`

**Interfaces:**
- Consumes: `CaptureDevice` (same module), `QualityPreset` (core), `sounddevice` (already imported in this module as `sd` — verify the alias).
- Produces:
  - `ProbeResult(ok: bool, effective_rate: int, message: str = "")` frozen dataclass.
  - `probe_capture_rate(device: CaptureDevice | None, sample_rate: int, channels: int) -> ProbeResult` — `device=None` means the global default loopback. Inputs: `sd.check_input_settings`; loopback/process_loopback: OK when `rate <= mix rate` of the matching WASAPI output device, else falls back to the mix rate with a notice.
  - `apply_rate_probe(preset: QualityPreset, device: CaptureDevice | None) -> tuple[QualityPreset, str | None]` — returns (possibly rate-adjusted) preset + notice message (None when unchanged).
  - `_wasapi_output_mix_rate(name_hint: str | None) -> int | None` (module-private, monkeypatch seam).

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_audio_devices.py`; mirror its existing monkeypatch style for `sd`)

```python
def test_probe_input_ok(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    monkeypatch.setattr(
        ad.sd, "check_input_settings", lambda **kw: None, raising=False
    )
    dev = ad.CaptureDevice(kind="input", name="Mic", id=3)
    res = ad.probe_capture_rate(dev, 96000, 2)
    assert res.ok and res.effective_rate == 96000


def test_probe_input_falls_back_to_device_default(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    def boom(**kw):
        raise Exception("unsupported")

    monkeypatch.setattr(ad.sd, "check_input_settings", boom, raising=False)
    monkeypatch.setattr(
        ad.sd, "query_devices",
        lambda idx=None, kind=None: {"default_samplerate": 44100.0},
        raising=False,
    )
    dev = ad.CaptureDevice(kind="input", name="Mic", id=3)
    res = ad.probe_capture_rate(dev, 192000, 2)
    assert not res.ok
    assert res.effective_rate == 44100
    assert "192000" in res.message and "44100" in res.message


def test_probe_loopback_over_mix_rate_falls_back(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    monkeypatch.setattr(ad, "_wasapi_output_mix_rate", lambda hint: 48000)
    dev = ad.CaptureDevice(kind="loopback", name="Speakers", id="spk")
    res = ad.probe_capture_rate(dev, 96000, 2)
    assert not res.ok
    assert res.effective_rate == 48000
    assert "24000" in res.message  # honest Nyquist notice


def test_probe_loopback_at_or_below_mix_rate_ok(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    monkeypatch.setattr(ad, "_wasapi_output_mix_rate", lambda hint: 48000)
    dev = ad.CaptureDevice(kind="loopback", name="Speakers", id="spk")
    assert ad.probe_capture_rate(dev, 48000, 2).ok
    assert ad.probe_capture_rate(dev, 16000, 2).ok


def test_probe_loopback_unknown_mix_rate_is_permissive(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    monkeypatch.setattr(ad, "_wasapi_output_mix_rate", lambda hint: None)
    res = ad.probe_capture_rate(None, 96000, 2)
    assert res.ok and res.effective_rate == 96000


def test_apply_rate_probe_rebuilds_preset(monkeypatch):
    import flashback_sampler.app.audio_devices as ad
    from flashback_sampler.core.quality_presets import QualityPreset

    monkeypatch.setattr(ad, "_wasapi_output_mix_rate", lambda hint: 48000)
    preset = QualityPreset(
        name="CUSTOM", sample_rate=96000, channels=2, buffer_seconds=300.0
    )
    adjusted, notice = ad.apply_rate_probe(preset, None)
    assert adjusted.sample_rate == 48000
    assert adjusted.buffer_seconds == 300.0 and adjusted.channels == 2
    assert notice is not None

    ok_preset = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2, buffer_seconds=300.0
    )
    same, none_notice = ad.apply_rate_probe(ok_preset, None)
    assert same is ok_preset and none_notice is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_audio_devices.py -q`
Expected: FAIL with `AttributeError: module ... has no attribute 'probe_capture_rate'`.

- [ ] **Step 3: Implement** (append to `app/audio_devices.py`; add `from flashback_sampler.core.quality_presets import QualityPreset` and `from dataclasses import dataclass` if absent; the module already imports sounddevice — keep its alias, assumed `sd`)

```python
@dataclass(frozen=True)
class ProbeResult:
    """Outcome of asking whether a device can honestly deliver a rate."""
    ok: bool
    effective_rate: int
    message: str = ""


def _wasapi_output_mix_rate(name_hint: str | None) -> int | None:
    """
    Shared-mode mix-format rate of the WASAPI output device matching
    `name_hint` (falling back to the default output). PortAudio reports
    a WASAPI output's `default_samplerate` from its mix format, which is
    exactly the rate Windows hands to loopback captures. None = unknown.
    """
    try:
        hostapis = sd.query_hostapis()
        was = next(
            (i for i, h in enumerate(hostapis) if "WASAPI" in h.get("name", "")),
            None,
        )
        if was is None:
            return None
        devices = sd.query_devices()
        outputs = [
            d for d in devices
            if d["hostapi"] == was and d["max_output_channels"] > 0
        ]
        if name_hint:
            hint = name_hint.casefold()
            for d in outputs:
                if hint in d["name"].casefold():
                    return int(d["default_samplerate"])
        didx = hostapis[was].get("default_output_device", -1)
        if didx is not None and didx >= 0:
            return int(devices[didx]["default_samplerate"])
    except Exception:
        return None
    return None


def probe_capture_rate(
    device: CaptureDevice | None,
    sample_rate: int,
    channels: int,
) -> ProbeResult:
    """
    Can this source honestly deliver `sample_rate`? Loopback rates above
    the output mix format add no information (Windows hands loopback
    audio at the mix rate), so we fall back with a notice instead of
    silently upsampling. Unknown capabilities are treated permissively —
    the capture backends already handle format conversion.
    """
    kind = device.kind if device is not None else "loopback"
    if kind == "input":
        try:
            sd.check_input_settings(
                device=device.id, samplerate=sample_rate,
                channels=channels, dtype="float32",
            )
            return ProbeResult(True, sample_rate)
        except Exception:
            try:
                info = sd.query_devices(device.id)
                fallback = int(info["default_samplerate"])
            except Exception:
                fallback = 48_000
            return ProbeResult(
                False, fallback,
                f"'{device.name}' can't open at {sample_rate} Hz — "
                f"capturing at {fallback} Hz instead.",
            )
    # loopback / process_loopback: capped by the output mix format
    mix = _wasapi_output_mix_rate(device.name if device is not None else None)
    if mix is None or sample_rate <= mix:
        return ProbeResult(True, sample_rate)
    return ProbeResult(
        False, mix,
        f"Output mix format is {mix} Hz — a {sample_rate} Hz capture "
        f"won't contain content above {mix // 2} Hz. "
        f"Capturing at {mix} Hz instead.",
    )


def apply_rate_probe(
    preset: QualityPreset,
    device: CaptureDevice | None,
) -> tuple[QualityPreset, str | None]:
    """Probe `device` for `preset.sample_rate`; return the (possibly
    rate-adjusted) preset plus a user-facing notice, or (preset, None)."""
    probe = probe_capture_rate(device, preset.sample_rate, preset.channels)
    if probe.ok:
        return preset, None
    adjusted = QualityPreset(
        name=preset.name,
        sample_rate=probe.effective_rate,
        channels=preset.channels,
        buffer_seconds=preset.buffer_seconds,
        description=preset.description,
    )
    return adjusted, probe.message
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_audio_devices.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/audio_devices.py tests/unit/test_audio_devices.py
git commit -m "feat(app): capture-rate probe with honest loopback fallback"
```

---

### Task 11: Probe wiring in `_on_add_source`

**Files:**
- Modify: `flashback_sampler/app/turntable_window.py` (`_on_add_source`, ~line 1247)
- Test: `tests/unit/test_turntable_window.py`

**Interfaces:**
- Consumes: `apply_rate_probe` (Task 10); the existing `_on_add_source` flow (`dlg.result_preset()` / `result_name()` / `result_device()` → `self._state.add_slot(preset, name=name)`).

- [ ] **Step 1: Write the failing test** (append to `tests/unit/test_turntable_window.py`. `_on_add_source` runs a modal dialog, so test the seam the same way the module will call it: monkeypatch `apply_rate_probe` in the window module and call the small helper this task extracts.)

```python
def test_add_source_applies_rate_probe(qapp, state, monkeypatch):
    import flashback_sampler.app.turntable_window as tw
    from flashback_sampler.core.quality_presets import QualityPreset

    adjusted = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2, buffer_seconds=60.0
    )
    monkeypatch.setattr(
        tw, "apply_rate_probe", lambda preset, device: (adjusted, "mix is 48k")
    )
    win = TurntableWindow(state)
    try:
        requested = QualityPreset(
            name="CUSTOM", sample_rate=96000, channels=2, buffer_seconds=60.0
        )
        result = win._probe_and_notify(requested, None)
        assert result.sample_rate == 48000  # notice shown via stubbed QMessageBox
    finally:
        win.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_turntable_window.py -q`
Expected: FAIL with `AttributeError: ... no attribute '_probe_and_notify'`.

- [ ] **Step 3: Implement**

Import in `turntable_window.py` (extend the existing audio_devices import): `apply_rate_probe`. Add the helper next to `_on_add_source`:

```python
    def _probe_and_notify(self, preset, device):
        """Rate-probe the requested preset against the chosen device;
        show the honest-fallback notice when the rate was adjusted."""
        adjusted, notice = apply_rate_probe(preset, device)
        if notice:
            QMessageBox.information(self, "Sample rate adjusted", notice)
        return adjusted
```

In `_on_add_source`, after `preset` and `device` are read from the accepted dialog and before `self._state.add_slot(preset, name=name)`:

```python
        preset = self._probe_and_notify(preset, device)
```

(Read the actual `_on_add_source` body first — insert after `dlg.result_device()` is read so the probe sees the chosen device, including None for the global default.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_turntable_window.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/turntable_window.py tests/unit/test_turntable_window.py
git commit -m "feat(app): probe capture rate on add-source with fallback notice"
```

---

### Task 12: Docs, full-suite green, manual Ableton acceptance

**Files:**
- Modify: `README.md` ("Using it" section + CLI flags note)
- No new tests — this task is verification.

- [ ] **Step 1: Update README** — add to the "Using it" numbered list (after the SAVE bullet):

```markdown
7. **Drag it into your DAW.** Grab the inside of a selection band on
   either deck and drag it out of the window — the slice lands as a
   32-bit-float WAV on whatever accepts file drops (an Ableton track,
   Explorer, a sampler). Ctrl+drag on the clip deck exports the whole
   untrimmed clip. Exports live in the pool folder (Preferences →
   Export; default `Documents/flashback-sampler/exports`) and the
   dragged clip stays on the right deck as your sample bank — never
   move pool files a DAW project still references.
```

And note the hi-res rates where the README mentions `--sample-rate` / the Add Source dialog: rates up to 192k are offered; when a device can't honestly deliver a rate (loopback is capped at the Windows output mix format), Flashback notifies and captures at the honest rate.

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest tests/unit -q`
Expected: ALL PASS, no skips introduced by this branch.

- [ ] **Step 3: Launch the app for a smoke check**

Run: `python -m flashback_sampler.app.main --buffer-minutes 0.5`
Expected: window opens; start capture on the default loopback source; make a selection on the left deck; drag it into an open Explorer window → a WAV appears in Explorer and the clip lands on the right deck marked saved. Verify `sf.info` on the pool file reports FLOAT.

- [ ] **Step 4: Manual acceptance in Ableton Live 12 (user-driven)** — hand the running build to Ryon with this checklist:

1. Buffer-deck selection → drag onto an Ableton **Session** clip slot and an **Arrangement** track — audio lands and plays.
2. Clip-deck trimmed drag + Ctrl full-clip drag into Ableton.
3. Cancel a drag mid-flight (Esc) — no file left in the pool, no stray clip from a buffer-deck cancel.
4. Delete the clip in Ableton, recover the same file from the export pool.
5. Save the Ableton project, reopen — the sample reference still resolves.
6. Add Source at 96k on loopback — the honest-fallback notice appears when the output mix format is 48k; set Windows output to 96k (or use a 96k-capable interface input) and confirm 96k capture works.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: drag-out workflow + hi-res rates in README"
```

---

### Branch wrap-up (after all tasks)

Feature-PR tier per global instructions: run `/simplify` (one combined pass) then `/code-review` at **medium** effort (inline, single pass — never the multi-agent workflow), address findings, then superpowers:finishing-a-development-branch.
