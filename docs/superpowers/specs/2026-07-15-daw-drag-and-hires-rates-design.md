# Flashback Sampler — DAW drag-out + hi-res sample rates

**Date:** 2026-07-15
**Status:** Approved (this session's scope)
**Context:** First step of the DAW-integration arc. Competitive reference is
Birds Things' *Rolling Sampler* ($19, VST3/CLAP/AU + standalone). Key intel:
Rolling Sampler only records audio the DAW routes into the plugin — it does
NOT capture system audio (their docs point at loopback drivers for that), and
it only creates files when a selection is dragged out. Flashback's multi-track
system/process capture is a real differentiator; what we lack is the
"snipe a slice off the waveform and drop it on a DAW track" gesture.

**Decision:** test the easiest path first. An OS-level file drag out of the
standalone app works with every DAW (Ableton, Reaper, FL, Bitwig, …) and
requires no plugin binary. The VST/bridge arc is deferred until this UX is
dialed in (see "Deferred: VST arc" below).

**Session success criterion:** drag a slice from Flashback into Ableton
Live 12 on Windows and have the project reference survive save/reopen.

---

## Feature 1 — Drag a slice out of the app

### Mechanism (approved: render-at-drag-start)

When a drag gesture crosses Qt's drag-start threshold, render the slice from
RAM to a real WAV file in the **export pool**, then start a standard OS file
drag (`QDrag` with a `file://` URL → `CF_HDROP` on Windows). Plain file drops
are the one mechanism every DAW accepts; it is also portable to macOS/Linux
via the same Qt API later.

Rejected alternatives: virtual-file drags (`CF_FILEDESCRIPTOR`) — COM-heavy,
Windows-only, Ableton compatibility doubtful; pre-rendering every checkout —
writes disk for clips never dragged and doesn't cover left-deck drags anyway.

Rendering is synchronous on the GUI thread with a wait cursor (audio capture
runs on its own threads and is unaffected). The audio is RAM-resident, so a
typical slice renders in tens of ms; a worst-case 15-minute grab is a few
hundred ms.

### Export pool (the "sample bank")

- New config key `export_pool_dir`, default `<captures dir>/exports/`,
  editable in Preferences. Files persist — DAW projects reference dropped
  files at their original path until the user collects/consolidates, so the
  pool must never be auto-cleaned.
- File naming: `<source>_<YYYYMMDD-HHMMSS>_<len>s.wav`, collision-suffixed
  (`_2`, `_3`, …). The pool self-documents.
- The pool + the right deck together form a **sample bank**: every
  successfully dragged slice remains findable (as a file, and as a saved
  clip on the right deck) even if the user later deletes it from the DAW.

### Drag sources (approved: both decks, drags persist)

- **Right deck (checkout deck):** press-and-drag a checked-out clip →
  renders the *trimmed* range → OS drag. On successful drop the clip is
  marked `saved` (pointing at the pool file).
- **Left deck (buffer deck):** drag the current selection straight off the
  buffer → implicit checkout → render → OS drag. On successful drop the
  checkout **persists on the right deck in `saved` state** (user amendment:
  the just-dragged clip must be recoverable — sample-bank UX). On a
  cancelled drag the transient checkout is discarded.
- **Cancelled drags** (Esc / drop rejected → `Qt.IgnoreAction`): delete the
  just-rendered file; no clip persists from a left-deck cancel.

### Render format

- Default **32-bit float WAV** — bit-perfect with the internal float32
  buffers, universally supported.
- New config key `export_bit_depth` ∈ {FLOAT (default), PCM_24, PCM_16},
  in Preferences.
- **Bug fix folded in:** `CheckoutManager.save()` currently calls
  `sf.write(...)` with no subtype, and libsndfile's WAV default is PCM_16 —
  today's "lossless" saves silently downcast float32 → 16-bit.
  `save()` gains an explicit `subtype` parameter (default FLOAT) used by
  both the SAVE dialog and the drag renderer.

### Component boundaries

- `core/drag_export.py` (pure Python, no Qt):
  `render_drag_file(manager, checkout_id, pool_dir, bit_depth) -> Path` —
  naming, collision handling, delegation to `CheckoutManager.save()`.
- App layer: a small drag controller/seam (e.g.
  `app/drag_out.py`) that builds the `QMimeData`/`QDrag` from a rendered
  path and interprets the drop action (persist vs cleanup). The deck
  widgets call it from their mouse handlers; new UI actions go through the
  action registry per project convention.

## Feature 2 — Hi-res / DAW-standard sample rates

### Presets (approved amendment: DAW-standard preset)

Extend the preset cluster in `core/quality_presets.py`:

- **DAW 44.1k** — 44 100 Hz stereo (Ableton's default project rate),
  5 min default buffer.
- **HI-RES 96k** — 96 000 Hz stereo, 5 min default buffer (~220 MB).
- Existing FULL/MUSIC (48k) presets unchanged; 48k remains the app default.

### Custom row

The Add Source dialog gains a **Custom** row: sample rate (44.1k, 48k,
88.2k, 96k, 176.4k, 192k), channels, buffer length — with the live RAM
readout (192k stereo float32 ≈ 46 MB/min, so the math display matters).
RAM math reuses `compute_ram_bytes` unchanged.

### Probe + honest fallback (approved)

On source selection, probe whether the device can truly deliver the
requested rate:

- **Mic / line-in (sounddevice):** validate via the backend's
  supported-settings check (`check_input_settings`).
- **Loopback (WASAPI):** query the output device's shared-mode mix format.
  Windows hands loopback audio at the mix format rate; requesting more
  cannot add information.

If the device can't deliver the rate, show a notice — e.g. "output mix
format is 48 kHz — a 96k capture won't contain content above 24 kHz" — and
fall back to the honest rate rather than silently upsampling. Internal
dtype stays float32 everywhere.

## Testing

TDD throughout (failing test first).

- **Core units (pytest, no Qt):** render naming + collision suffixes;
  subtype correctness (read back the written WAV's subtype: FLOAT/PCM_24/
  PCM_16); save() default-subtype regression test; probe fallback logic
  against fake device backends; preset table additions + RAM math.
- **App seam tests:** the drag controller is factored so tests can assert
  the prepared mime data / file path / drop-action handling without a real
  OS drag loop (project conftest already stubs Qt statics).
- **Acceptance (manual, user's machine):** drag from both decks into
  Ableton Live 12 (session + arrangement views) and into Explorer; delete
  clip in Ableton and recover it from the pool; save/reopen the Ableton
  project and confirm the reference survives; cancelled-drag cleanup.

## Deferred: VST arc (context for future sessions)

Not in scope now; recorded so the next session doesn't re-derive it:

- The app is Python/PySide6 — a loadable VST3/CLAP requires native code.
  Two candidate shapes: (a) **thin C++ "tap" plugin** that forwards DAW
  track audio via shared-memory IPC to the running Flashback app (DAW
  tracks become capture sources next to system/process audio — the caddy
  model); (b) **native JUCE port** of the ring buffer with in-plugin
  waveform + drag-out (Rolling Sampler parity, one instance per track).
- Rolling Sampler's precedent suggests plugin-input-only capture is
  acceptable to the market; the caddy model would exceed it.
- The drag mechanism built here (render → file drag) is exactly what the
  in-plugin drag-out needs too; the UX learned here transfers.
- macOS/Linux untested; a friend's Mac is available for eventual testing.
