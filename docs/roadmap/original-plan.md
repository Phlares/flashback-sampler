# Plan: flashback-sampler — checkout workflow, channel selection, and native PySide6 UI

## Context

We've built a working Python prototype of a circular audio buffer (`flashback-sampler`) that continuously captures Windows system audio via WASAPI loopback (soundcard library on a COM-initialized background thread) and lets the user save arbitrary slices to WAV. Today it runs as a CLI smoke test (`tests/test_quick.py`). The core ring buffer, capture, playback, and export code is all in place and has been exercised end-to-end.

The user now wants to turn this into a **standalone desktop applet** with two priority features:

- **P1 — "Check-out" workflow.** Pull a fixed-duration slice (~3 min) out of the live ring buffer as a discrete clip, preview-scrub it forward and backward, and decide to keep or discard — all while the buffer keeps recording uninterrupted. Mental model: a DJ with one turntable still spinning, pulling a record off the rack to audition before committing. Multiple clips can be pulled and sit in a tray; one can be previewed at a time.
- **P2 — Channel selection.** Choose which input device/channel(s) feed the buffer.

Downstream ambition (shapes today's architecture): this app should eventually become a **VST/DAW plugin and/or OBS plugin**. Webviews don't survive inside VST hosts, so the UI is being built in **PySide6 native**, and the audio core must stay framework-agnostic so a future C++/JUCE port can reuse the DSP logic without porting UI code.

The user has supplied a rich design reference set. The canonical visual language is the **Erebus mood board** (see Palette section below), which is warmer and more thermal than the purple `DESIGN.md` that accompanies `Flashback_example/code.html`. The TP-7 / OP-1 hardware layout (rotary encoder + 3 tactile buttons + waveform strip above) is the target control scheme.

Methodology: **test-driven development**, committing the current stable baseline first, then iterating red-green-refactor through milestones with small, frequent commits.

---

## Design addendum — accepted from frontend-design skill (2026-04-12)

The frontend-design skill produced a locked-in Erebus visual system that supersedes the draft palette / fonts / wireframes further down in this document. When sections disagree, this addendum wins. The invocation transcript and full spec live in the conversation; key accepted decisions:

### Window form factor (supersedes "640×960 portrait" in the original wireframes)
- **Primary:** 960 × 520 landscape. Two tracks live left and right (not top and bottom). Rotary + transport sits in a central 60 px vertical sliver between them — "bridge of a DJ mixer" metaphor.
- **Secondary:** 1280 × 220 hyper-wide "dock strip" form factor for the caddy use case.
- **Reasoning:** landscape maps cleanly onto future VST3 channel strips and OBS docks. Portrait wastes vertical real estate next to DAWs / IDEs. DJ metaphor is literally side-by-side, not stacked.

### Controls (supersedes "rotary = duration, MODE / PLAY / CHECK OUT" in milestones)
- **Rotary = live scrub playhead for the ring buffer.** Always active, always useful. Turn it to travel back in time through the buffer. Readout is mounted in the knob hub itself (`−02:17` in 36 pt Monaspace Krypton).
- **Two buttons only** — `MARK IN` and `CHECK OUT`. `MODE` is deleted.
- **Press rotary** = set mark-in at current cursor. **Press-and-hold rotary** = trigger checkout around current cursor.
- **Duration preset cluster** — 8 vertical preset cells (0:15, 0:30, 1:00, 2:00, 3:00, 5:00, 10:00, MAX). One glows ember at a time. Replaces the stepper.
- `PLAY` button appears only on Track 2 (clip transport) since Track 1 is always rolling — no play needed on a live input.

### Palette (supersedes the thermal-heavy Erebus palette further down)
Drop-in for `flashback_sampler/app/ui/theme/palette.py`:

```python
EREBUS = {
    "void":            "#08070a",
    "chassis":         "#0e0d10",
    "plate":           "#161418",
    "ridge":           "#1e1b20",
    "cream":           "#f2eddf",
    "bone":            "#a8a398",
    "ash":             "#5a5652",
    "hairline_faint":  "rgba(242,237,223,0.06)",
    "hairline":        "rgba(242,237,223,0.12)",
    "hairline_strong": "rgba(242,237,223,0.22)",
    "ember":           "#ff5a1f",
    "ember_hot":       "#ff8a3d",
    "ember_deep":      "#c73a0d",
    "rec":             "#ff2a1c",
    "signal":          "#e8e0d2",
    "signal_dim":      "#8a857a",
    "signal_rms":      "#b0aa9a",
    "meter_floor":     "#241510",
    "meter_low":       "#5f2812",
    "meter_mid":       "#c04614",
    "meter_hot":       "#ff7a1e",
    "meter_peak":      "#ffc400",
    "meter_clip":      "#ff2a1c",
}
```

- Thermal gradient is used **only** on the VU level meter (20 discrete segments mapped to `meter_*`). It is not a primary brand color.
- `ember` is the single brand accent, reserved for: check-out CTA tell bar, rotary indicator line, mark-in/out handles, xrun warnings, the ember-colored "— CLIP HELD —" negative-space divider in State B.
- Waveforms render in `signal` cream — **never** in ember.

### Typography — all-mono via GitHub Monaspace (SIL OFL, bundled locally)
Supersedes Space Grotesk / Inter / Roboto Mono entirely.

| Role | Family | Weight | pt | Tracking | Case |
|---|---|---|---|---|---|
| `display-xl` | Monaspace Krypton | 500 | 28 | −10 | normal |
| `display` | Monaspace Krypton | 500 | 20 | −5 | normal |
| `readout` | Monaspace Krypton | 700 | 36 | −15 | tabular, normal |
| `label` | Monaspace Neon | 500 | 9 | +80 | UPPER |
| `label-sm` | Monaspace Neon | 500 | 8 | +120 | UPPER |
| `body` | Monaspace Argon | 400 | 10 | 0 | normal |
| `data` | Monaspace Krypton | 500 | 11 | 0 | tabular, normal |

**Discipline rule (load-bearing):** no sans-serif in any commit. If Inter, Space Grotesk, or Roboto appears, revert. Monaspace Argon is the only concession for comfortable prose (tooltips, error messages).

Iosevka is the fallback family if Monaspace ever has to be pulled.

### Component specs (summary — full detail in chat transcript)
- **WaveformView:** recessed screen, custom `paintEvent` paints a 1 px hairline on top+left, 1 px #000 on bottom+right, 2 px chassis inset, void interior fill. Peak-bin rendering (min/max vertical lines), no RMS fill in State A. RMS fill only in State B with `signal_rms` at 40%.
- **RotaryKnob:** 220 px outer diameter, 14 px bezel ring (radial gradient void→chassis to fake recession), 192 px dial face in `ridge`, 12 engraved ticks at 30° in `bone` 40%, 3 × 28 px `ember` indicator line, 60 px `void` hub with 1 px `hairline_strong` ring and the `readout` time display mounted inside the hub.
- **TactileButton (primary — "Check Out"):** 168 × 52, 6 px radius, `ridge` fill, `label` text in `ember`, 2 px `ember` tell bar at bottom edge, 1 px top-inside hairline at 14% cream.
- **TactileButton (secondary — "Mark In", clip `PLAY`/`IN`/`OUT`):** 88 × 44 (PLAY variant 128 × 44), 6 px radius, `plate` fill, cream text, no ember bar, pressed state inset-paints a 1 px `void` line at the top edge.
- **LevelMeter:** 6 px wide per channel, 20 discrete horizontal segments with 1 px gaps, color-mapped via `meter_*` palette, 1.2 s peak hold, 200 ms clip flash.
- **StatusBar:** 28 px tall, label-sm cream, separator is a vertical bar glyph `│` at 20% cream — no drawn line. xrun count turns `ember` if > 0, `rec` if > 5.
- **Track divider (State A → B):** no line. 24 px of empty chassis background with a single `label-sm` `— CLIP HELD —` in `ember` at 60% opacity, centered. Negative space is the divider.

### Wireframes — accepted landscape versions
State A (960 × 520, live only) and State B (960 × 520, checkout active) both use the landscape split: Track 1 left, rotary cluster center sliver, duration presets / Track 2 right. ASCII renderings live in the chat transcript; will be reproduced in-repo under `docs/wireframes/` during M5.

### Milestone impact of these decisions
- **M0:** add Monaspace fonts to `app/ui/theme/fonts/` (download step documented in `packaging/README.md` when that lands). Drop `palette.py` above into `flashback_sampler/app/ui/theme/palette.py` in M4.
- **M4:** initial window geometry = 960 × 520. Load Monaspace fonts via `QFontDatabase.addApplicationFont(...)`. Apply Erebus `chassis` fill to main window.
- **M5:** `WaveformView` implements the recessed-screen paintEvent. Left half of window.
- **M6:** `RotaryKnob` (central sliver) + 2 tactile buttons + 8-preset duration cluster. Right half: Track 2 (checkout clip) when a clip is held, otherwise a diagnostic column (xrun graph, buffer occupancy history).
- **M8:** no sans-serif audit — grep the repo for Inter/Roboto/SpaceGrotesk before tagging.

---

## Revised milestone sequence (decided 2026-04-12)

After shipping M7 (device pickers + config persistence + preview routing), the user confirmed the multi-source architecture direction. Revised order:

1. **M8 — Visual polish** (next): Monaspace font bundling, `TactileButton` custom `paintEvent` (ember tell-bar + inset pressed state), topographical background painting on the main window, no-sans-serif audit (grep-and-verify no Inter/Roboto/SpaceGrotesk slipped in).
2. **M9 — Backlog B1–B5** (after M8): right-click drag-to-select context menus on both waveforms, sub-pixel scrubbing precision, mouse-wheel rotary control, right-click context menu on checkout list items, settings dialog for buffer duration etc. See detailed spec below.
3. **M10 — Shape B multi-source refactor + per-source granularity**: see detailed spec below.

Skipped: M7.1 (virtual-cable stopgap docs). User has a clear path forward without it.

## M10 spec — Shape B multi-source (2026-04-12)

### Topology — Shape B

One `AudioCircularBuffer` instance per source. Each source is an independent capture slot with its own ring, capture thread, checkout manager, anchor state, and UI track. Rejected alternatives: Shape A (single N-channel buffer, harder to scrub sources independently) and a hybrid muxed-then-split model.

### New data structures

```
class CaptureSlot:
    id: str
    name: str                 # user-given label, e.g. "Discord voice"
    route: CaptureRoute       # backend + config (see M10.1 below)
    buffer: AudioCircularBuffer
    checkout_manager: CheckoutManager
    capture_source: CaptureSource | None   # lazy — built on start
    anchor_offset_s: float = 0.0
    duration_preset_idx: int = DEFAULT_DURATION_INDEX
    # Per-slot settings (the granularity knobs)
    quality_preset: str = "FULL"
    sample_rate: int = 48_000
    channels: int = 2
    buffer_seconds: float = 900.0

class AppState:
    # replaces the current single-buffer / single-checkout-mgr fields
    slots: list[CaptureSlot]
    active_slot_id: str | None   # which slot drives the transport cluster
    scrub_player: ScrubPlayer   # still shared — one active preview at a time
    output_spec: OutputDevice
```

The preview model from M7 (one scrub player, one active preview) still holds. The user clicks a clip in any slot's checkout list, the shared scrub player binds to it regardless of which slot produced it.

### Per-source granularity presets

UI surface: an "Add Source" dialog with a vertical preset cluster mirroring the duration preset look.

| Preset | SR | Ch | Default dur | RAM (approx) |
|---|---|---|---|---|
| FULL    | 48000 | stereo | 15 min | 346 MB |
| MUSIC   | 48000 | stereo |  5 min | 115 MB |
| VOICE   | 22050 | mono   | 15 min |  79 MB |
| CHAT    | 16000 | mono   | 10 min |  38 MB |
| SCRATCH | 16000 | mono   |  3 min |  11 MB |
| CUSTOM  | dropdowns (SR / channels / duration) with live RAM readout |

- Dialog also shows the **total project RAM** (sum across all active slots) with a warning color when it crosses a configurable budget (default 4 GB, adjustable in Settings / B5).
- Changing the quality preset of an **existing slot** rebuilds its `AudioCircularBuffer` (confirmation modal — buffered audio is lost; existing checkouts are preserved because they're immutable snapshots).

### Deferred: dtype configurability

Currently the buffer is hardcoded `np.float32`. Making dtype per-slot (e.g. `int16` for another 2× savings) requires refactoring `get_peak_bins`, `get_rms_levels`, `ScrubPlayer.bind()`, WAV/FLAC export, and the seqlock lap math. Deferred until sample rate / channels / duration prove insufficient in practice. Sample rate and channel count give us a 12× range; that's enough headroom for v1.

### UI layout changes

The current single-column layout (Track 1 waveform → transport → Track 2 → checkout list) becomes:

```
[Title: FLASHBACK]
[Source strip: [ + Add Source ] [ Slot A ] [ Slot B ] ... ]  <- slot tabs/chips
[Track 1 — buffer view of the ACTIVE slot]
[Transport cluster (rotary, presets, check out) — ACTIVE slot]
[Track 2 — checkout clip (global, any slot's clip)]
[Checkout list — filtered to active slot by default, toggle for "ALL SOURCES"]
[Action row]
[Status bar — shows per-slot xrun totals]
```

- A **source strip** at the top lets you switch the "active" slot (the one the big transport cluster drives). Each slot chip shows: name, tiny live waveform strip, xrun count, record dot if capturing.
- Track 1 is the ACTIVE slot's buffer view. Switching slots instantly retargets it.
- The checkout list filters by default to the active slot but has an "ALL SOURCES" toggle to flatten everything.
- The action row still has PREVIEW / SAVE / DISCARD because there's one preview engine.
- Status bar grows a per-slot xrun + RAM summary.

Alternative to tabs: stacked compact "mini track" widgets along the left edge, with only the active one getting the big transport. Decision deferred until M9's settings dialog is in place and we can prototype both in a frontend-design pass.

### Capture backend abstraction (prerequisite)

`flashback_sampler/core/capture_backend.py` (new):
- `class CaptureBackend(Protocol)` — minimal interface: construct from a route spec, start, stop, is_running, xrun_count.
- Each slot's capture source is built by dispatching on `CaptureRoute.backend` — the existing `WasapiSystemLoopback` (soundcard) and `MicLineIn` (sounddevice AudioCapture) become concrete backends. New backends (`WasapiProcessLoopback`, etc.) plug in without touching `CaptureSlot` or the UI.

### TDD plan for M10

- **M10.1** — `CaptureBackend` / `CaptureRoute` abstraction. Retrofit existing loopback and mic-capture as backends. Tests: route dispatch, backend isolation, existing functional tests unchanged.
- **M10.2** — `CaptureSlot` dataclass and `AppState.slots` refactor with ONE slot (backward compatible). All existing tests must still pass with the single-slot AppState wrapping the existing flow.
- **M10.3** — Quality presets + RAM math in a pure helper module. Unit-test the math.
- **M10.4** — "Add Source" dialog UI. Creates a new slot with the chosen preset. Multi-slot AppState now live.
- **M10.5** — Source strip / slot switcher + per-slot track targeting. UI now shows multiple sources.
- **M10.6** — Checkout list filtering (per-slot vs. all sources). Preview works across slots.
- **M10.7** — Per-slot flush / remove. Confirmation modals.
- **M10.8** — Total project RAM budget guard + warnings.

---

## Post-M6 feature backlog (user-requested, 2026-04-12)

From the second M6 eyeball pass. Queued for post-current-priorities — land after M7 (device picker) and M8 (visual polish) unless they block other work.

### B1. Context-menu interactions on waveforms

**On the live buffer (Track 1):**
- Left-click-drag on the waveform to paint a **selection band** that lives in waveform-pixel space (not time-offset space — the band anchors to the buffered audio and scrolls with it as new audio comes in). First click sets mark-in, drag-release sets mark-out. A second click clears.
- Right-click anywhere on a committed selection opens a context menu:
  - `Check Out Segment`
  - (later) `Check Out From Mark-In to Now`
  - `Clear Selection`
- Creating a checkout from a selection uses an **absolute sample range** snapshot, not "seconds ago" — so the clip's start/end stay pinned to the original audio even if the ring has advanced. `CheckoutManager.create_from_abs_range(abs_start, abs_end)` is the new core API this needs; wraps `AudioCircularBuffer._copy_abs_range()`.

**On the checkout clip (Track 2):**
- Left-click-drag within Track 2's waveform to paint a **trim selection** (sets `Checkout.trim_in_samples` / `trim_out_samples`). The already-painted ember playhead continues to show scrub position.
- Right-click on a committed trim selection opens a context menu:
  - `Preview Selection`
  - `Export Selection as WAV…` — skips the Save As flow's format picker, always writes WAV
  - `Export Selection as FLAC…`
  - `Set Mark-In to Playhead` / `Set Mark-Out to Playhead`
  - `Clear Trim`
- `Checkout.trimmed_audio()` already respects trim_in/out, so the export path plugs straight in.

**Widget work:**
- Add selection state (`_mark_in_frac`, `_mark_out_frac`) and a semi-transparent ember fill rect to `WaveformView.paintEvent`.
- `ClickableWaveform` grows mouse handlers for drag-to-paint.
- `QMenu` instances constructed on demand in the main window, with slots that read the selection from the widget and call the appropriate core methods.

### B2. Higher-precision scrubbing

Current state: click-to-seek on Track 2 jumps to whole-pixel positions. At 500 bins × ~800 px, that's ~1.6 px per bin, which works but feels coarse on long clips.

- Capture `event.position().x()` as a `float`, not `int`. Don't truncate.
- Store the playhead fraction internally as `float` and render sub-pixel via `QLineF` (which is float-aware — already the case in WaveformView but the *input* is currently integer-fractioned).
- Same treatment for the Track 1 ghost anchor playhead — the rotary value is continuous, so the ghost position can be smooth even though bins are discrete.
- Match for mouse-drag-scrubbing: move events while left button is held should update the cursor continuously, not just on click.
- Acceptance: scrubbing a 15-minute clip with a 1 px movement changes the cursor by `~0.9 seconds` (15 * 60 / 960 px) instead of jumping by whatever bin landed nearest.

### B3. Mouse-wheel rotary control

- `RotaryKnob` grabs focus on click. While focused, `wheelEvent` advances the value by a tunable `wheel_step` (default: 1/60 of the full range per notch, so 60 ticks traverses the whole sweep). Shift modifier = 5× step; Ctrl modifier = 0.2× step for fine positioning.
- Focus ring: a thin ember circle just inside the bezel when focused.
- Keyboard arrow keys while focused (up/right = increase, down/left = decrease) — cheap win, same math as wheel.

### B4. Generally applicable: right-click → export

Once B1's `Export Selection as WAV/FLAC` is in, extend the same context menu model to the checkout list: right-click an item in the `QListWidget` → `Save As WAV`, `Save As FLAC`, `Discard`, `Rename`. Replaces the separate `SAVE` / `DISCARD` buttons in the action row for power users while keeping the buttons for discoverability.

### B5. Buffer duration UI control

Currently only changeable via `--buffer-minutes` CLI flag. Add a settings dialog (gear icon in the title row) with:
- Buffer duration (1 s resolution, 10 s minimum, 60 min max — the upper bound keeps RAM sane)
- Sample rate (48 kHz default; show only rates the selected device supports — needs M7's device picker first)
- Channel count (1 / 2 — for mono mic capture; M7 multi-channel support)
- Max simultaneous checkouts
- Max total checkout RAM
- Default output directory for saves
- Preview device (separate from capture device — also M7)

Changing any setting rebuilds the `AppState`'s AudioCircularBuffer (with a confirmation dialog, since existing audio is lost) and restarts capture if it was running.

---

## Non-goals (explicit out-of-scope for this plan)

- Raspberry Pi hardware path — `hardware/encoder.py` stays as-is, no changes.
- True VST/OBS plugin packaging — architectural prep only; actual JUCE/OBS port is a later project.
- Per-app audio isolation (capturing only Spotify without system mix) — would need VB-CABLE; deferred.
- Mac/Linux support — the loopback path is Windows-only for now; tests stay cross-platform via fake audio sources.
- Non-local authentication, telemetry, cloud sync, accounts.

---

## Architecture

### Framework-agnostic core vs. UI layer

```
flashback_sampler/
  core/                    [NO Qt imports, NO soundcard imports]
    buffer.py              # existing — AudioCircularBuffer
    capture_source.py      # NEW — abstract CaptureSource base class (inject-friendly)
    checkout.py            # NEW — Checkout + CheckoutManager
    channels.py            # NEW — ChannelRouter (P2)
    scrub_player.py        # NEW — long-lived OutputStream + manual read cursor
    exporter.py            # REFACTORED from playback.py — AudioExporter only
  io/                      [may import soundcard/sounddevice]
    loopback_capture.py    # existing — implements CaptureSource (Windows WASAPI)
    mic_capture.py         # existing capture.py renamed — implements CaptureSource
    playback_device.py     # existing playback portion — sounddevice OutputStream wrapper
  app/                     [may import PySide6]
    __init__.py
    main.py                # QApplication entry, assembles the object graph
    state.py               # AppState — holds buffer, capture, player, checkout mgr
    controllers/
      capture_controller.py
      checkout_controller.py
      device_controller.py
    ui/
      main_window.py       # QMainWindow subclass
      widgets/
        waveform_view.py   # QWidget custom-painted waveform
        rotary_knob.py     # QWidget custom-painted rotary encoder
        tactile_button.py  # QPushButton subclass with Erebus styling
        buffer_track.py    # "Track 1" — live rolling waveform
        checkout_track.py  # "Track 2" — checked-out clip, static w/ scrub
        device_picker.py
        checkout_tray.py
      theme/
        erebus.qss         # Qt stylesheet
        palette.py         # Palette constants (Python-accessible)
        icons/             # bundled SVG icons
        fonts/             # bundled .woff2 or .ttf (Space Grotesk, Inter, Roboto Mono)
  hardware/                [unchanged — Pi-only, not touched]
    encoder.py
tests/
  unit/
    test_buffer.py
    test_checkout.py
    test_scrub_player.py
    test_channels.py
    test_exporter.py
    test_capture_source_interface.py
  integration/
    test_fake_capture_to_buffer.py
    test_checkout_nonblocking.py
  fixtures/
    sine_source.py         # deterministic SineCaptureSource for tests
    silence_source.py
  test_quick.py            # keep as interactive dev tool, unchanged
conftest.py
pyproject.toml              # updated — adds PySide6, pytest, pytest-cov, pytest-timeout
```

**Separation discipline:**

- `flashback_sampler/core/*` is pure Python + numpy. No Qt, no soundcard, no sounddevice. Unit-testable headless on any platform. This is the layer that can later be ported to C++/JUCE or called from a VST wrapper.
- `flashback_sampler/io/*` owns all platform audio APIs (soundcard, sounddevice). Implements the `CaptureSource` interface defined in core.
- `flashback_sampler/app/*` owns all Qt code. Controllers hold references to core objects and translate Qt signals/slots into core calls.
- Tests never touch real audio hardware except in a clearly-marked integration tier that can be skipped in CI.

### Buffer — seqlock pattern for non-blocking reads

**Risk identified in design review:** `AudioCircularBuffer.get_segment()` currently does `.copy()` under `_lock`. For a 3-minute stereo float32 slice that's ~70 MB memcpy, ~20–40 ms of lock-held time, which will starve the audio callback and cause capture glitches.

**Fix (before M2):** introduce an optimistic/seqlock read pattern on top of the existing lock:

1. Under lock, snapshot `write_pos` and `total_written`, compute slice index range, release lock.
2. Copy the slice outside the lock.
3. Re-acquire lock briefly, verify `total_written - snapshot_total < buffer_size - slice_len` (i.e. the writer did not lap us during the copy).
4. If lapped, retry once with a smaller slice or return a short read with a `stale=True` flag.

`get_segment()` keeps the same signature. `write()` stays unchanged. Writer never blocks.

Also track `total_written` as a monotonic sample count so checkouts resolve "seconds ago" to an **absolute sample range** at creation time. This prevents drift if the user saves a checkout 30 s after creating it — the saved audio is still the audio that was in the ring when the checkout was taken.

### Checkout — immediate in-RAM snapshot + background WAV

- `Checkout` = dataclass holding: `id` (uuid), `created_at` (monotonic), `abs_sample_start`, `abs_sample_end`, `sample_rate`, `channels`, `audio: np.ndarray` (the snapshot), `trim_in_samples`, `trim_out_samples`, `temp_path: Optional[Path]`, `state: Literal["pending", "ready", "saved", "discarded"]`.
- On creation: call `buffer.get_segment()` (seqlock read), store the resulting ndarray in `self.audio`, transition to `pending`. Kick off a background thread that writes `self.audio` to a temp WAV via `soundfile`. On write completion, transition to `ready` and emit `checkout_ready` signal.
- Scrubbing reads from `self.audio` in RAM — not from disk. Disk is only used so that "Save" is an `os.replace()` of an already-written file (no re-encode unless converting to FLAC).
- Save flow: if target format matches temp format (WAV→WAV), `os.replace(temp_path, final_path)`. If format differs (WAV temp → FLAC final), re-encode via `soundfile.write(final_path, self.audio, sr, format="FLAC")`. Discard flow: delete temp file, drop `audio` ref.
- `CheckoutManager` owns the dict of active checkouts and a `tempfile.TemporaryDirectory` bound to app lifetime. On startup it sweeps and deletes stale `flashback_sampler_checkout_*` dirs from previous crashes. Configurable `max_active_checkouts` and `max_total_ram_mb` with a visible warning in the tray when approached.

### ScrubPlayer — single long-lived OutputStream

- One `sd.OutputStream(callback=...)` instance, created on first play, reused for the lifetime of the app.
- Callback reads `self._cursor:self._cursor+frames` from whichever ndarray is currently bound (`self._source`) and advances the cursor. Seek = atomic assignment to `_cursor`. Pause = flag read by callback which zero-fills. Stop = zero-fill + `_source=None`.
- TDD-friendly: unit tests drive the callback directly with a numpy output buffer and assert on read positions, seek behavior, wrap-around, and pause state. No real audio device required for 95% of the tests.
- "One active preview at a time" is enforced by `ScrubPlayer` only ever having one `_source` — switching to a different checkout atomically swaps the ndarray and resets the cursor.

### CaptureSource interface

```
class CaptureSource(Protocol):
    sample_rate: int
    channels: int
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_running(self) -> bool: ...
    def xrun_count(self) -> int: ...
```

Every concrete source (`LoopbackCapture`, `MicCapture`, `SineCaptureSource`, `SilenceCaptureSource`) pushes frames into an injected `AudioCircularBuffer` via `buffer.write(frames)`. The core never constructs sources itself — the app layer injects them. Tests use fakes and never touch soundcard.

**Fix before M4:** sample-rate negotiation. Today the capture hardcodes 48 kHz and works by luck. `LoopbackCapture.start()` should query the device's native rate and either (a) pass it to the buffer on construction, or (b) resample at write time via `scipy.signal.resample_poly`. Option (a) is simpler; the buffer exposes `sample_rate` as read-only and the app creates the buffer after opening the capture source.

**Xrun counting:** every `CaptureSource` exposes `xrun_count()`. The UI surfaces it in the status bar so users don't blame the app for OS-level glitches.

---

## Palette — Erebus (derived from mood board)

Extracted visually from `TEOP1.jpg` (Teenage Engineering OP-1 chassis), `topography.png` (thermal gradient), `ee2437f14f2efa89abf623a310c758f9.jpg` (Yamaha burnt-orange / slate split), and inferred from the other ~10 reference images. **The remaining Erebus images should be spot-checked during M0 to confirm and refine — values here are a working draft.**

```
# Base chassis — warm charcoal, never pure black
erebus.base              = #0b0a09  # darkest — void / recessed slot interior
erebus.surface           = #161311  # main chassis
erebus.surface-container = #221c18  # raised functional plate
erebus.surface-raised    = #2e2621  # top-elevation panels
erebus.surface-bone      = #c9bda6  # cream/ivory — OP-1 chassis tone (light variant, optional)

# Thermal gradient — the Erebus signature, used for primary CTAs and data
erebus.thermal-0         = #3b0500  # deep ember
erebus.thermal-1         = #8a1200  # dark fire
erebus.thermal-2         = #d14600  # burnt orange (PRIMARY solid)
erebus.thermal-3         = #f88a1a  # orange flame
erebus.thermal-4         = #ffcc33  # amber crown
erebus.thermal-hot       = #ff6a00  # vibrant CTA mid-stop

# Accents
erebus.amber             = #ffaa33  # LED-style readout text
erebus.record            = #c11530  # record dot, error, destructive
erebus.bronze            = #7a4024  # secondary elements
erebus.warm-grey         = #6b6055  # disabled / muted

# Text
erebus.on-surface        = #e8e0d2  # primary text — warm cream
erebus.on-surface-muted  = #9a8e7a  # secondary text
erebus.on-primary        = #140800  # text on thermal-2 background

# Topographical overlays
erebus.topo-line         = #3b2015  # 10% opacity background etchings
```

### Fonts

- **Headline / display:** Space Grotesk (bundled local .woff2)
- **Body / labels:** Inter (bundled local .woff2)
- **Mono / readouts:** Roboto Mono (bundled local .woff2) — used for all numeric data (time, sample pos, fill %, RMS, bitrates)

Qt loads fonts via `QFontDatabase.addApplicationFont(...)` at startup. Files live in `app/ui/theme/fonts/`.

### Styling rules (carried from DESIGN.md, adapted for Qt)

- **No-line rule:** no 1 px borders for containment; use tonal shifts, `QGraphicsDropShadowEffect` inner-shadows (custom painting), and elevation jumps between `surface` tiers.
- **Rounded everything:** minimum 4 px radius; prefer 16–24 px for large panels, 12 px for buttons, full-circle for knobs.
- **Tactile buttons:** default state has soft top-highlight + bottom-shadow; hover raises; press depresses (inset shadow, 1 px offset).
- **Recessed screens:** waveform displays sit inside a darker `base` tier with inner shadow — "glass set into the chassis."
- **Topographical background:** subtle radial-dot or concentric-ring pattern painted into the main window background at ~8% opacity.
- **Thermal gradient on CTAs:** record / play buttons use a vertical gradient from `thermal-2` to `thermal-1` with a 1 px top highlight at 15% opacity.

---

## Wireframes (first pass — refine with frontend design skill after `/reload-plugins`)

### State A — Live only (no active checkout)

```
+------------------------------------------------------------------+
|  FLASHBACK SAMPLER                            [ - ][ [] ][ X ]   |
+------------------------------------------------------------------+
|  . . . . . . . . . . . . . (topographical background) . . . .  |
|                                                                  |
|  +------------------------------------------------------------+  |
|  |  LIVE BUFFER                      48.0kHz  2ch   Realtek  |  |
|  |  +------------------------------------------------------+ |  |
|  |  |                         /\                          | |  |
|  |  |                    /\  /  \    /\       /\          | |  |
|  |  |  /\    /\    /\   /  \/    \  /  \  /\ /  \   /\    | |  |  <- rolling waveform
|  |  | /  \  /  \  /  \ /          \/    \/  V    \ /  \   | |  |     (peak bins)
|  |  |/    \/    \/    v                       \    v    \ | |  |
|  |  +------------------------------------------------------+ |  |
|  |  00:00:14 / 15:00     fill 1.6%       L -18dB  R -19dB    |  |
|  +------------------------------------------------------------+  |
|                                                                  |
|                  .-------.                                       |
|                 /         \                                      |
|                |    |      |    <- rotary encoder                |
|                 \   *     /         (scrub / select duration)    |
|                  '-------'                                       |
|                    32%                                           |
|                                                                  |
|       +-----+       +---------+       +-----+                    |
|       |MODE |       |  PLAY   |       |CHECK|                    |
|       +-----+       +---------+       | OUT |                    |
|                     (thermal)         +-----+                    |
|                                                                  |
|  [ Device v ]    [ Src: Speakers (Realtek) ]    xruns: 0         |
+------------------------------------------------------------------+
```

### State B — Checkout active (two-track vertical stack)

```
+------------------------------------------------------------------+
|  FLASHBACK SAMPLER                            [ - ][ [] ][ X ]   |
+------------------------------------------------------------------+
|  +------------------------------------------------------------+  |
|  |  LIVE BUFFER [REC]                        (rolling)        |  |  <- Track 1
|  |  +------------------------------------------------------+  |  |     Always on top
|  |  |  ~~~~~~~~~^^^~~~^^~^~^^~~~~^^~~^~^~^^~^~~^^^~^~~    |  |  |     compressed when
|  |  +------------------------------------------------------+  |  |     track 2 is open
|  |  00:02:47 / 15:00     fill 18%        L -14 R -13          |  |
|  +------------------------------------------------------------+  |
|                                                                  |
|                  .-------.       +-----+  +-------+  +-----+     |
|                 /         \      |MODE |  | PLAY  |  |CHECK|     |  <- transport
|                |   [TRANS] |     +-----+  +-------+  | OUT |     |     (compact)
|                 \    *    /                          +-----+     |
|                  '-------'                                       |
|                                                                  |
|  +------------------------------------------------------------+  |
|  |  CHECKOUT  clip_2026-04-12_17-05-22   3:00   pending->ready|  |  <- Track 2
|  |  +------------------------------------------------------+  |  |
|  |  | [  <||                                         ||>  ] |  |  |     Static waveform
|  |  |  ^^^~~~^^^^~^^~~^~^^^~~~^^^^~^^~^~~^~^^~~^^~^^~^^^   |  |  |     mark-in / mark-out
|  |  |     ^IN                        OUT^                  |  |  |     handles
|  |  +------------------------------------------------------+  |  |
|  |  00:00:47 / 03:00      [ < ] [ >||< ] [ > ]              |  |
|  |                                                            |  |
|  |  Format: (WAV) (FLAC)    [  SAVE AS...  ]   [ DISCARD ]   |  |
|  +------------------------------------------------------------+  |
|                                                                  |
|  [ Device v ]    [ Src: Speakers (Realtek) ]    xruns: 0         |
+------------------------------------------------------------------+
```

**Layout notes:**
- Track 1 is always visible. When Track 2 is active, Track 1 compresses (smaller waveform strip) but capture never stops.
- Transport cluster (rotary + 3 buttons) moves between Track 1 and Track 2 and stays horizontally centered — TP-7 control-strip metaphor.
- Checkout tray (list of all pending/ready checkouts) is reachable via a small side drawer or a horizontal strip at the bottom of Track 2 when more than one clip is in flight. For M5 we assume at most one visible at a time and defer the tray UI to M6.
- Initial window size: 640×960 (3:4 portrait, matches `code.html` reference chassis). Min 560×820. Resizable.

---

## Milestones (TDD — red, green, refactor, commit)

### M0 — Baseline + setup  (~1 hour)

- [ ] Commit current working state (capture extra_settings param, LoopbackCapture + COM init, updated `test_quick.py`, bumped requirements).
- [ ] Run `/reload-plugins` to pick up user's pre-installed frontend design tools.
- [ ] Spot-check remaining Erebus images and refine palette if needed.
- [ ] Add dev deps: `pytest`, `pytest-cov`, `pytest-timeout`, `PySide6` (pin to LTS).
- [ ] `pyproject.toml` updates; `tests/` scaffolding; `conftest.py`.
- [ ] First failing test: `tests/unit/test_buffer.py::test_empty_buffer_returns_zero_length` (will pass immediately — this is the red-green-refactor warm-up and confirms CI plumbing).

### M1 — Ratify the ring buffer with tests  (~2 hours)

Write tests against existing `buffer.py`, surface any latent bugs, add fixtures.

- [ ] `fixtures/sine_source.py` — `SineCaptureSource` producing deterministic float32 [N, ch] blocks.
- [ ] `test_buffer.py`:
  - empty, partial, exactly-full, wrapped
  - `get_latest(seconds)` boundary cases (0 s, more than buffered, more than capacity)
  - `get_segment(start_ago, end_ago)` correctness, clamping, wrap-around
  - thread-safety smoke test (writer + reader concurrent)
  - `total_written` monotonicity
  - `status()` shape
- [ ] Add `get_peak_bins(seconds, n_bins)` method — returns `(n_bins, 2, channels)` min/max pairs. Used by waveform view. Test: sine wave → symmetric peaks.

### M2 — Seqlock read + Checkout core  (~4 hours)

- [ ] Refactor `get_segment()` to seqlock pattern. Tests must still pass.
- [ ] New test: `test_get_segment_nonblocking_under_writer_load` — writer thread pounds the buffer; main thread takes 100 segments; assert writer never stalls > 2 ms.
- [ ] `core/checkout.py`:
  - `Checkout` dataclass
  - `CheckoutManager.create(duration_s, anchor="latest"|"oldest"|"now_minus_n")`
  - `.save(id, path, fmt="WAV"|"FLAC")`
  - `.discard(id)`
  - `.list()`
  - TempDir lifecycle (creation, sweep on startup, cleanup on exit)
- [ ] `test_checkout.py`:
  - snapshot correctness (sine in → sine out)
  - buffer keeps writing during checkout creation (inject writer thread, assert it's not blocked)
  - save as WAV → file exists, correct length, correct samples
  - save as FLAC → file exists, decodes back to matching floats
  - discard → temp file removed
  - multiple simultaneous checkouts
  - max-ram cap enforced
  - stale tempdir sweep
- [ ] Commit.

### M3 — ScrubPlayer  (~3 hours)

- [ ] `core/scrub_player.py` with callback-driven cursor.
- [ ] `test_scrub_player.py`:
  - drive callback directly with a dummy output buffer
  - seek updates cursor
  - pause zero-fills
  - wrap / end-of-source behavior (stop vs. loop — we want stop)
  - `bind(ndarray)` swaps source atomically
- [ ] Integration: one test that uses a real sounddevice OutputStream (skipped on CI via marker `@pytest.mark.audio_hw`).
- [ ] Commit.

### M4 — PySide6 shell + packaging smoke  (~6 hours)

**Crucial: prove packaging works before investing in UI.** Identified as the #1 deferred risk in design review.

- [ ] `app/main.py` — minimal QApplication, one QMainWindow with a placeholder label and a "Start / Stop Capture" button.
- [ ] `app/state.py` + `controllers/capture_controller.py` — wire a real `LoopbackCapture` through the controller, update a level meter.
- [ ] `app/ui/main_window.py` — skeletal layout, no theming yet.
- [ ] Apply Erebus palette via `theme/erebus.qss` — just background + primary colors for now.
- [ ] **Packaging smoke:** PyInstaller onedir build targeting Windows, `--collect-all soundcard --collect-all sounddevice --collect-all soundfile --collect-all PySide6`. Run the built EXE on the same machine, verify capture still works. Document the magic incantation in `packaging/README.md`.
- [ ] Commit.

### M5 — Live buffer widget (Track 1)  (~4 hours)

- [ ] `widgets/waveform_view.py` — QWidget that paints peak bins via `QPainter`. Takes `(n_bins, 2, channels)` data and draws centered bars.
- [ ] `widgets/buffer_track.py` — composes waveform + time readout + level meter.
- [ ] Controller pushes `get_peak_bins(...)` results to the widget at 30 Hz via `QTimer` on the Qt main thread (capture thread → signal → main thread slot).
- [ ] Device picker dropdown calls `list_input_devices()` and rebinds the capture source.
- [ ] Visual: apply Erebus recessed-screen styling to the waveform container.
- [ ] Manual visual QA on Windows. Commit.

### M6 — Checkout track + transport (Track 2)  (~6 hours)

- [ ] `widgets/rotary_knob.py` — custom-painted circular control with mouse drag → angle. Emits `valueChanged(float)`.
- [ ] `widgets/tactile_button.py` — subclass `QPushButton` with Erebus styling (thermal gradient when primary).
- [ ] `widgets/checkout_track.py` — shows checked-out clip waveform + mark-in/mark-out handles + Save/Discard row.
- [ ] "Check out" button pulls a slice (default 3 min — rotary selects duration), creates Checkout, shows in Track 2.
- [ ] Scrubber interaction → `ScrubPlayer.seek()`.
- [ ] Format toggle WAV/FLAC, Save As dialog, Discard confirmation.
- [ ] Layout: Track 1 compresses when Track 2 appears; transport cluster stays centered.
- [ ] Commit.

### M7 — Channel selection (P2)  (~4 hours)

- [ ] `core/channels.py` — `ChannelRouter` takes a source spec `{device_id, channel_mask, downmix}` and produces frames shaped for the buffer.
- [ ] `test_channels.py` — L-only, R-only, specific channels of a fake 8-ch source, downmix to mono/stereo.
- [ ] `widgets/device_picker.py` — expanded with a channel-selection sub-panel per device.
- [ ] Persist last-used device + channel mask to `%APPDATA%\flashback-sampler\config.json`.
- [ ] Commit.

### M8 — Erebus visual polish  (~6 hours)

- [ ] Pull all fonts local, load via `QFontDatabase`.
- [ ] Full Erebus palette wired into `erebus.qss`.
- [ ] Custom paint for topographical background.
- [ ] Custom paint for tactile button states (hover rise, press depress).
- [ ] Recessed screen inner-shadow on waveform containers (`QGraphicsDropShadowEffect` or custom).
- [ ] Thermal gradient on primary CTA.
- [ ] Icon set — bundled SVG, tinted via palette.
- [ ] Final visual QA pass against Erebus mood board; iterate.
- [ ] Commit.

### M9 — Stretch / cleanup

- [ ] Checkout tray UI for >1 simultaneous clips.
- [ ] Configurable buffer duration.
- [ ] Keyboard shortcuts (spacebar = play/pause preview, `[` `]` = mark in/out, etc.).
- [ ] Xrun history graph in status bar.
- [ ] Cleanup: delete `tests/test_quick.py` or downgrade to a documented dev tool.

---

## Critical files

### To be created

- `flashback_sampler/core/capture_source.py`
- `flashback_sampler/core/checkout.py`
- `flashback_sampler/core/scrub_player.py`
- `flashback_sampler/core/channels.py`
- `flashback_sampler/core/exporter.py` (split from `playback.py`)
- `flashback_sampler/io/__init__.py`
- `flashback_sampler/app/main.py`
- `flashback_sampler/app/state.py`
- `flashback_sampler/app/controllers/*`
- `flashback_sampler/app/ui/main_window.py`
- `flashback_sampler/app/ui/widgets/waveform_view.py`
- `flashback_sampler/app/ui/widgets/rotary_knob.py`
- `flashback_sampler/app/ui/widgets/tactile_button.py`
- `flashback_sampler/app/ui/widgets/buffer_track.py`
- `flashback_sampler/app/ui/widgets/checkout_track.py`
- `flashback_sampler/app/ui/widgets/device_picker.py`
- `flashback_sampler/app/ui/theme/erebus.qss`
- `flashback_sampler/app/ui/theme/palette.py`
- `tests/unit/test_buffer.py`
- `tests/unit/test_checkout.py`
- `tests/unit/test_scrub_player.py`
- `tests/unit/test_channels.py`
- `tests/unit/test_exporter.py`
- `tests/integration/test_fake_capture_to_buffer.py`
- `tests/integration/test_checkout_nonblocking.py`
- `tests/fixtures/sine_source.py`
- `tests/fixtures/silence_source.py`
- `conftest.py`
- `packaging/README.md`

### To be modified (reuse existing logic)

- `flashback_sampler/core/buffer.py` — add seqlock reads, `get_peak_bins()`, `total_written` usage in checkouts. **Keep existing API.** Existing `get_segment()` / `get_latest()` / `write()` / `status()` / `get_rms_levels()` all continue to work.
- `flashback_sampler/core/capture.py` → move to `io/mic_capture.py`, implement `CaptureSource` protocol.
- `flashback_sampler/core/loopback_capture.py` → move to `io/loopback_capture.py`, implement `CaptureSource` protocol. **Keep existing COM init logic — it works.**
- `flashback_sampler/core/playback.py` → split: `AudioExporter` moves to `core/exporter.py` (pure), `AudioPlayback` logic is superseded by `ScrubPlayer` + a lighter `PlaybackDevice` wrapper in `io/playback_device.py`. The old `AudioPlayback.play()` / `play_from_buffer()` code can be cannibalized.
- `tests/test_quick.py` — updated import paths only; keep functional.
- `requirements.txt` + `pyproject.toml` — add PySide6, pytest family.

### Existing functions to reuse (verified in code)

- `AudioCircularBuffer.write(frames)` — core/buffer.py:48 — wrap-around handled, thread-safe.
- `AudioCircularBuffer.get_segment(start_ago, end_ago)` — core/buffer.py:88 — will be refactored to seqlock but keeps semantics.
- `AudioCircularBuffer.get_rms_levels(window_seconds)` — core/buffer.py:133 — reused by level meter.
- `AudioCircularBuffer.status()` — core/buffer.py:140 — reused by status bar.
- `LoopbackCapture._run()` COM initialization pattern — loopback_capture.py — must preserve the `CoInitializeEx` / `CoUninitialize` bracket when moving to `io/`.
- `AudioExporter.save_latest` / `save_segment` / `_write` / `generate_filename` — playback.py:80–115 — reused by checkout save path.

---

## Verification strategy

### Unit tests (run on every commit)

```
pytest tests/unit/ -v --cov=flashback_sampler/core --cov-report=term-missing
```

Target: ≥90% coverage of `flashback_sampler/core/*`. Every public method on `AudioCircularBuffer`, `CheckoutManager`, `ScrubPlayer`, `ChannelRouter`, `AudioExporter` has at least one test.

### Integration tests (run locally, opt-in)

```
pytest tests/integration/ -v -m "not audio_hw"
```

Fake capture sources only. Exercises the full pipeline headless: fake source → buffer → checkout → export → re-read → assert sample equality.

### Hardware tests (manual, pre-release)

```
pytest tests/integration/ -v -m audio_hw
```

Exercises real sounddevice OutputStream and real soundcard LoopbackCapture. Skipped in CI, run by the developer on Windows before tagging a release.

### End-to-end smoke (manual)

1. `python -m flashback_sampler.app.main` — window opens, live waveform rolls as Windows audio plays.
2. Click Check Out — Track 2 appears with a 3-min clip, waveform rendered, scrubber at 0.
3. Drag scrubber, press play — audio scrubs forward/back through the clip; capture (Track 1) keeps rolling.
4. Click Save As, pick WAV — file saved, opens cleanly in Audacity.
5. Create a second checkout — Track 2 swaps to new clip; previous clip still accessible via tray (M6+).
6. Click Discard — temp file deleted, Track 2 collapses.
7. Quit — tempdir cleaned, no orphaned WAVs in `%TEMP%`.
8. Run PyInstaller build, launch EXE on a clean Windows profile, repeat steps 1–7.

### Per-milestone gates

No milestone is "done" until:
- All new tests pass.
- No regressions in existing tests.
- Manual visual QA on Windows.
- Small, focused commit with a conventional message.

---

## Open items / decisions still pending

1. **Plugin reload.** User has pre-installed frontend design tools; action for execution phase: `/reload-plugins` as the first step of M0 before any wireframe work is refined. Use the frontend design skill to revise the ASCII wireframes above into something sharper before M5.
2. **Erebus palette spot-check.** Current palette is derived from 3 of 13 Erebus images. Verify against the remaining 10 before committing it to `palette.py` in M4/M8.
3. **Sample-rate strategy.** Pick between (a) query-and-match at open time vs. (b) resample-at-write. Recommend (a) — simpler, lower CPU, buffer just inherits the device rate. Decide during M4.
4. **Checkout duration UX.** Is duration selected via rotary knob before checkout, or is there a fixed default with trim handles after the fact? Recommend: rotary selects default duration; after checkout, in/out handles on the clip can tighten the selection further.
5. **Packaging target.** PyInstaller onedir for Windows confirmed. Mac/Linux not targeted this plan.
6. **VST/OBS plugin path.** Not in scope; design decisions (framework-agnostic core, no Qt in core/io) preserve the option.

---

## Permissions needed for execution

When this plan is approved and we exit plan mode, the agent will need permission to:
- Run `/reload-plugins` (session-level, no file changes)
- Invoke the installed frontend design skill (`Skill` tool)
- Write and edit files under `C:\Users\Ryon\Documents\dev\flashback-sampler\`
- Run `git add` / `git commit` in that repo
- Run `pip install` for dev dependencies (`pytest`, `pytest-cov`, `pytest-timeout`, `PySide6`)
- Run `pytest` repeatedly
- Run `python` to launch the app for manual smoke tests
- Run `pyinstaller` for the M4 packaging smoke test
- Read image files from `C:\Users\Ryon\Documents\dev\UI_UX references\Erebus\`
