# Zig core phase 2, part 2 — mixer, playback, and the end of the Python buffer

Date: 2026-08-30. Status: approved in brainstorm, spec under review.
Tracker: epic #17 (PRs d, e, f). Parent spec:
`2026-08-16-zig-core-phase2-design.md`. Where this document differs from
the parent, this document wins.

## Goal

After PR f, no audio frame is produced, mixed, played, or downsampled
by Python. Zig owns every audio thread and every per-sample loop.
Python is a shell: it creates handles, starts and stops them, and reads
numbers. The three audio libraries (`sounddevice`, `soundcard`,
`soundfile`) and the FLAC path are gone.

## Standing rules

- **Python will disappear.** Every line of logic written in this part
  lives in Zig. `native.py` keeps only ctypes declarations and one-line
  calls. No maths, no loops, no fallbacks in Python.
- **Idiomatic, allocation-free Zig on the audio path.** Fixed buffers,
  atomics, `*anyopaque` + vtable interfaces (the `std.mem.Allocator`
  idiom `Backend.zig` already uses). Allocation only at `init`/`bind`
  on the control thread.
- **Portability eye.** `Mixer` and `Playback` talk to `Backend.zig`
  only and never import `wasapi.zig`. A CoreAudio / AAudio / PipeWire
  backend is one new file.
- **Zig test count must rise per PR** and every new file gets its own
  `pub const` in `root.zig` (`refAllDecls` is one level deep).
- Owner is learning Zig: each PR carries a "Zig concepts in this PR"
  section, and comments explain constraints the code cannot show.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| PR split | d Mixer (+ #41 rider), e Playback + output enumeration, f delete Python buffer + peak bins to Zig + deps + FLAC + soak. Three PRs to `dev`. | Parent spec's PR f (native enumeration) already landed inside PRs a/b. |
| `writer_active` ownership | Control-thread-owned. `start()` stores true before `Thread.spawn`; `stop()` stores false after `join`. The worker never writes it. | The scope that spawns is the scope that joins, so it owns the flag across both. Closes the start-window race for `Capture` and `Mixer` with one rule. |
| Mixer shape | N `Capture`s → N staging `Ring`s (2 s each) → one mixer thread (10 ms tick) sums, clips, writes the target `Ring`. | Sources arrive on separate streams with separate timing; a staging ring per source absorbs the skew. Port of the Python design. |
| Per-source gain | None in the mixer. Each staging ring and the target ring already apply `Ring.gain`. | One mechanism. 1/N pre-mix gain stays the caller's job, as today. |
| Playback rate | The render stream opens at the clip's recorded rate and channels. The backend resamples to the engine mix rate. | A 96 kHz clip is never touched by us; the WAV on disk stays 96 kHz. |
| Resampling | Borrowed from the OS (WASAPI `AUTOCONVERTPCM \| SRC_DEFAULT_QUALITY`, the flag capture already uses in the other direction). Recorded wall: the `Backend` contract says "the backend accepts the requested rate"; a platform that cannot (raw ALSA) gets a Zig resampler behind the same contract. | No resampler code now; the seam is where one would go. |
| Render delivery | Event-driven (`SetEventHandle` + wait), not polled. | The documented low-latency WASAPI render path; the thread sleeps at zero CPU until the engine asks. Capture polls only because of a loopback quirk that render does not have. |
| Output device picker | Not in this part. `AppState.set_output_spec` stays wired with endpoint-string ids; the app uses the system default at launch. | UI belongs to the #16 arc. |
| Peak bins | Move to Zig: `Ring.peakBins` + `fb_ring_peak_bins`, next to `summaryBins`. | Both are "downsample the ring for a display"; one seqlock read primitive serves both. Kicking maths into the Python shell has no future. |
| `soundfile` | Deleted. Tests decode WAV with a stdlib `struct` reader in `tests/fixtures/wavread.py`. | Its production role (FLAC) dies; a test oracle must not be the code under test and should not cost a C dependency. |
| FLAC | Deleted: format, menu actions, dialog filter, tests, docs. | Pure WAV FLOAT until other formats earn a place. |
| #41 (OOM at arm) | `fb_ring_create` reports `out_of_memory` distinctly from `invalid_arg`. The 4 GB app-level stop stays until #16 gives the message a home. | The engine-side catch is delivered; the UI half is #16's. |
| #26 | Closes in PR f with the soak: no Python write path remains. | The last `fb_ring_write` production caller is the Python mixer, gone in d. |

## Architecture after PR f

```
Python (Qt shell, ctypes only)         Zig (flashback_core)
------------------------------         --------------------------------------------
NativeCaptureSource ─ fb_capture_* ──► Capture ── thread ── Backend.Stream
NativeMixedSource ── fb_mixer_* ─────► Mixer ── N × (Capture → staging Ring) ── mixer thread ─► target Ring
NativeScrubPlayer ── fb_playback_* ──► Playback ── render thread ── Backend.RenderStream
audio_devices ────── fb_devices_list ► Backend.enumerate (capture + render endpoints)
waveform / checkout ─ fb_ring_read, fb_ring_summary_bins, fb_ring_peak_bins, fb_wav_write
```

## PR d — `Mixer.zig`, `writer_active` ownership, #41 rider

### `writer_active` (Ring / Capture)

- `Capture.start()`: `ring.writer_active.store(true, .release)` then
  `Thread.spawn`. `Capture.stop()`: set stop flag, `join`, drain a
  pending flush, `ring.writer_active.store(false, .release)`. `run`
  no longer touches the flag; its LIFO-defer comment goes.
- Test: a flush requested between `start()` and the worker's first
  loop iteration (FakeBackend blocks the first `next`) is deferred,
  not executed immediately. Probe from the control thread.
- Mutation pin: remove the store in `start()` → the test goes red.

### `Mixer.zig`

```zig
pub const max_sources = 8;
pub const stage_seconds = 2.0;
pub const tick_ms = 10;

const Source = struct { capture: Capture, stage: Ring, cursor: u64 };

pub const Mixer = struct {
    target: *Ring,                       // host-owned
    sources: [max_sources]Source, n_sources: u8,
    scratch: [Ring.max_write_frames * 2]f32,   // one source's read, per tick
    sum:     [Ring.max_write_frames * 2]f32,
    thread: ?std.Thread, stop_flag, running, xruns, err_len (atomics), err_buf: [256]u8,
};
```

- `init(allocator, backend, target, specs []const Backend.Spec) !Mixer`:
  rejects `n == 0` or `n > max_sources` (`error.InvalidArgument`);
  builds one staging `Ring` (target's rate and channels, 2 s) and one
  `Capture` per spec. Allocation happens here only.
- `start()`: `target.writer_active = true`; start every capture; on a
  failure stop those started, clear the flag, return the error; spawn
  the mixer thread.
- Loop (`run`), each tick: `target.drainPendingFlush()`; sleep
  `tick_ms`; for each source `avail = stage.total_written - cursor`;
  if `avail > stage.capacity` the mixer fell behind: `cursor =
  total_written - capacity`, `xruns += 1`; `n = min(avail)` capped at
  `Ring.max_write_frames` (one publish per tick); if `n == 0` continue;
  read `n` frames from each stage with `Ring.read` (seqlock, never
  blocks the capture) into `scratch`, add into `sum`; clamp to
  [-1, 1]; `target.write(sum[0 .. n * channels])`; advance cursors.
- `stop()`: stop flag, `join`, stop every capture, drain a pending
  flush, `target.writer_active = false`.
- `stats()` returns `Capture.Stats` (same struct): `running`,
  `frames_written` = target frames written by the mixer, `xruns` = own
  + Σ captures, `mix_rate` = first source's. `lastError` = own message
  or the first non-empty capture message.
- No diagnostic printing.

### ABI (`abi.zig`, `flashback_core.h`, `native.py`)

```
fb_mixer_create(target: *Ring, specs: [*]const FbCaptureSpec, n: usize) ?*Mixer
fb_mixer_start(*Mixer) FbStatus      fb_mixer_stop(*Mixer) void
fb_mixer_destroy(*Mixer) void        fb_mixer_stats(*const Mixer, out: *Capture.Stats) void
fb_mixer_last_error(*const Mixer) [*:0]const u8
```

`FbStatus` gains `out_of_memory = 5`. `fb_ring_create` gains an
optional `status: ?*FbStatus` out-parameter: `invalid_arg` for a
rejected config, `out_of_memory` when the allocation fails. Python
raises `MemoryError` with the requested byte count on
`out_of_memory` and `ValueError` on `invalid_arg`.

### Python

`core/mixed_capture.py` becomes `NativeMixedSource`, a handle wrapper
with the `CaptureSource` protocol (`start`, `stop`, `is_running`,
`xrun_count`, `last_error`, `mix_rate`, `close`), inert after
`close()` like `NativeCaptureSource`. `AppState.build_capture_for_slot`
passes the specs through instead of factories; the staging rings are
no longer visible to Python. The old class goes to `_ToRemove/`.

### Tests

Zig (FakeBackend, scripted packets and timing): common-span maths with
skewed packet timing; sum and clip; lapped cursor → one xrun, no
crash, cursor lands on the oldest valid frame; flush during mixing →
only post-flush frames in the target; start failure of source 2
unwinds source 1 and clears `writer_active`; `writer_active` is true
across the whole `start()`…`stop()` window; `out_of_memory` from
`fb_ring_create` under a failing allocator. Python: wrapper tests with
`native` mocked; `build_capture_for_slot` builds the mixer at ≥ 2
specs. Hardware (`tests/hw`): 2-source mixed capture for 2 s records
frames on both.

## PR e — render backend, `Playback.zig`, output enumeration

### `Backend.zig`

```zig
pub const Kind = enum(u8) { loopback = 0, input = 1, process = 2, render = 3 };

pub const RenderStream = struct {
    ptr: *anyopaque, vtable: *const VTable,
    pub const VTable = struct {
        /// Blocks up to timeout_ms until the engine wants frames. false = timeout.
        wait: *const fn (*anyopaque, timeout_ms: u32) bool,
        /// Frames the engine can take now (buffer_size - padding).
        available: *const fn (*anyopaque) Error!u32,
        /// Copies frames into the device buffer. Caller passes at most `available()` frames.
        write: *const fn (*anyopaque, frames: []const f32) Error!void,
        stop: *const fn (*anyopaque) void,
        deinit: *const fn (*anyopaque) void,
        mixRate: *const fn (*anyopaque) u32,
    };
};
// Backend.VTable gains:
openRender: *const fn (*anyopaque, Spec) Error!RenderStream,   // opens AND starts; called on the render thread
```

`Spec.rate` / `Spec.channels` are the clip's. The backend must accept
them (resample to its mix rate); `Error.FormatRejected` otherwise.

### `wasapi.zig` / `WasapiBackend.zig`

- `IAudioRenderClient` vtable (`GetBuffer`, `ReleaseBuffer`) and its
  IID; `AUDCLNT_STREAMFLAGS_EVENTCALLBACK`.
- `openRender`: activate the endpoint (`eRender`, by id or default),
  `Initialize` shared mode with `EVENTCALLBACK | AUTOCONVERTPCM |
  SRC_DEFAULT_QUALITY` at the spec's rate/channels (float32),
  `SetEventHandle`, `GetBufferSize`, `GetService(IAudioRenderClient)`,
  `Start`. `wait` = `WaitForSingleObject` on the event; `available` =
  `GetBufferSize - GetCurrentPadding`; `write` = `GetBuffer` /
  copy / `ReleaseBuffer`. Silence is written with
  `AUDCLNT_BUFFERFLAGS_SILENT` when the frames are all zero.
- `FakeBackend` gains a scripted render sink: fixed `available`,
  records every `write` into a growable test buffer, `wait` returns
  immediately.

### `Playback.zig`

```zig
pub const State = extern struct { running: u8, playing: u8, cursor: u64, clip_frames: u64, mix_rate: u32 };

pub const Playback = struct {
    allocator, backend, spec: Backend.Spec,
    clip: []f32,                          // owned copy, allocated at bind only
    cursor: atomic u64, playing: atomic bool, reopen: atomic bool,
    thread: ?std.Thread, stop_flag, running, err_len (atomics), err_buf: [256]u8,
};
```

- `bind(frames, rate, channels)`: pause; free and re-allocate `clip`;
  cursor 0; if `rate`/`channels` differ from `spec`, update `spec` and
  set `reopen`. The stream opens lazily on the first `play()`.
- Render loop: `wait(100)`; on timeout continue; `want = available()`;
  if not playing write `want` zeros; else copy
  `min(want, remaining)` frames from `clip`, zero-pad the tail, and
  when the cursor reaches `clip.len` store `playing = false`
  (auto-stop, no loop — the UI re-calls `play` for LOOP, as today).
  `reopen` set → `stop`/`deinit`/`openRender` on the render thread.
- `play()` rewinds when at end; `pause()`; `seek(frames)` clamps to
  `[0, clip.len]`; `setDevice(id)` copies the id and sets `reopen`.
  All state is atomic; no lock anywhere.
- `state()`; `lastError`.

### ABI

```
fb_playback_create(device_id: [*:0]const u8, rate: u32, channels: u16) ?*Playback
fb_playback_bind(*Playback, frames: [*]const f32, n_frames: usize, rate: u32, channels: u16) FbStatus
fb_playback_play / pause / seek(*Playback, frames: u64) / set_device(*Playback, id: [*:0]const u8)
fb_playback_state(*const Playback, out: *Playback.State) void
fb_playback_last_error(*const Playback) [*:0]const u8
fb_playback_destroy(*Playback) void
```

### Python

`core/scrub_player.py` becomes `NativeScrubPlayer`, a handle wrapper
with the names `turntable_window.py` already calls: `bind`, `play`,
`pause`, `stop`, `seek_samples`, `seek`, `set_device`,
`cursor_samples`, `cursor_seconds`, `is_playing`,
`source_length_samples`, `open`, `close`. `bind` passes the checkout's
rate and channels. The 20 `_audio_callback` tests are replaced by
wrapper tests against a mocked `native`; the fill logic is tested in
Zig.

### Output enumeration

`WasapiBackend.enumerate` tags render endpoints `.render`.
`audio_devices._list_native_devices` keeps offering them as loopback
capture candidates (one endpoint, two roles); `list_output_devices` /
`default_output_device` read the same native list, `OutputDevice.id`
becomes the endpoint string, `max_output_channels` = `mix_channels`.
The `sounddevice` import in `audio_devices.py` goes.
`AppState.set_output_spec` and `output_spec` carry the string id.

### Tests

Zig: partial tail zero-pads and auto-stops; paused writes zeros;
seek past end clamps; bind while playing pauses; rebind at a new rate
reopens on the render thread; `available() == 0` does not spin;
`openRender` failure sets `last_error` and `running = false`. Python:
`_on_play_clip_clicked` and `_update_clip_playback_state` with
`native` mocked; `list_output_devices` from a fake native list.
Hardware: play a 1 s tone on the default output and observe
`cursor` advance and `playing` drop to 0.

## PR f — Python buffer out, peak bins in Zig, deps and FLAC out, soak

### `Ring.peakBins`

```zig
pub const PeakBin = extern struct { min: f32, max: f32 };
/// Per channel, per bin: min and max sample over [abs_start, abs_end).
/// out.len == n_bins * channels. Reads through the seqlock like `read`.
pub fn peakBins(self: *Ring, abs_start: u64, abs_end: u64, n_bins: usize, out: []PeakBin) Error!void
export fn fb_ring_peak_bins(ring: *Ring, abs_start: u64, abs_end: u64, n_bins: usize, out: [*]PeakBin) FbStatus
```

Port of `_peak_bins_impl` (`buffer.py:40-165`): same bin edges, same
`out_of_range` behaviour on a lapped window, same headroom guard
band. A parity test runs the numpy implementation and the Zig one on
the same ring contents before the numpy one is deleted; the parity
test goes with it, and a Zig test pins the bin arithmetic directly.

Deviation (PR f plan): `peakBins`/`fb_ring_peak_bins` take a window
length `n_frames`, not `(abs_start, abs_end)` — the retry loop
re-snapshots and re-clamps inside Zig, as `fb_ring_summary_bins`
already does. `out` layout `[bin][channel]{min,max}`.

### Deletions

Deviation (PR f plan): `get_rms_levels` was numpy maths; it now calls
`fb_ring_rms` (`Ring.rmsLatest`). Two behaviour differences, both
fail-safe: `peakBins`' seqlock verify is stricter than numpy's
one-clause check (a flush during the scan retries instead of being
accepted), and `rmsLatest` does not retry a torn window (numpy's
`get_latest` retried three times) — both surface as zeros, which the
meter and the waveform already draw as silence.

- `AudioCircularBuffer`, `_peak_bins_impl`, `RingDerivedOps`,
  `make_ring_buffer` (`core/buffer.py` is deleted). `native.py` keeps
  ctypes declarations and `NativeAudioCircularBuffer`, whose methods
  are one-line ABI calls or unit conversion (dB ↔ linear, frames ↔
  seconds). `core/__init__.py` stops re-exporting the Python class.
- `tests/unit/test_buffer.py` loses the two-way fixture and runs
  native-only; `tests/fixtures/fake_capture.py`, `test_checkout.py`,
  `test_capture_source.py`, `test_drag_export.py` construct the native
  buffer. `test_native_smoke.py`'s fallback test goes.
- Deleted modules move to `_ToRemove/` for one approval at the end of
  the PR.

### Dependencies

`sounddevice`, `soundcard`, `soundfile` out of `pyproject.toml`,
`requirements.txt`, `flashback_sampler.spec` (`collect_all` loop and
docstring), `packaging/README.md`. Stale comments naming `soundcard`
(`capture_source.py`, `fake_capture.py`, READMEs) are rewritten.
`tests/fixtures/wavread.py`: stdlib `struct` reader for RIFF/WAVE
FLOAT32 and PCM16/24, used by every test that decoded a WAV.

### FLAC

`CheckoutFormat` becomes WAV only; `_DEFAULT_SUBTYPE` loses FLAC; the
PCM_24 coercion and the soundfile route in `checkout.save` go; the two
"Save … as FLAC" actions and the dialog filter in
`turntable_window.py` go; the four FLAC tests go; README rows updated.

### Docs

`README.md` / `PLATFORM.md`: capture, mixing, and playback are
Windows-only in this phase (the core cross-compiles; no other
`Backend` exists yet); arm-time RAM reservation wording. `ZIG-101.md`
(untracked): a note at the top listing the sections this part made
stale. The parent spec gets a "Deviations recorded by part 2" block
pointing here.

### Soak and closure

Owner runs `soak_test.py` (ported: `NativeCaptureSource` single-source
run and a 2-source `NativeMixedSource` run) for 300 s with audio
playing, plus Task Manager RSS and CPU idle-armed. Numbers go on #17
next to the "before" and "after PR a" tables. Then: close #26, tick
a–f on #17, close #17 by hand, delete the remote branches listed
there after approval.

## Error handling

Unchanged from part 1: `Error` sets on `open`/`start`; audio loops
never return an error, they record `last_error` and stop; ABI creates
return null with `fb_last_error()`, plus the new `fb_ring_create`
status out-parameter; Python surfaces `last_error` on the existing
slot timer.

## Out of scope

Output device picker (#16), a Zig resampler, non-Windows backends,
FLAC or any other format, #41's arm-time UI message, the `#16` UI
arc.

## Risks to measure, not assume

- `AUTOCONVERTPCM` on a shared-mode render stream at 96 kHz: verify on
  hardware in PR e before the rate decision stands.
- Event-driven render buffer size: `GetBufferSize` after
  `Initialize(0, 0)` decides the fill granularity; measure the idle
  CPU against today's PortAudio stream.
- `Ring.read` from the mixer thread against a staging ring written at
  4096-frame chunks: confirm the guard band holds at a 2 s capacity
  (the disjointness proof in `Ring.zig` assumes the readable window is
  never smaller than one chunk plus headroom).
- Peak-bin parity: the numpy version has bin-edge rounding that the
  Zig port must match exactly, or every waveform golden shifts.

## Deviations recorded by the plan (2026-08-30)

The plan (`docs/superpowers/plans/2026-08-30-zig-core-phase2-d-f.md`)
is authoritative where it differs below.

- `stop()` clears `writer_active` BEFORE draining the pending flush.
  After the join no writer exists; clearing first sends a late flush
  down `Ring.flush`'s immediate path instead of deferring it to a
  writer that will never come.
- `Mixer.init` is in-place (`init(self: *Mixer, ...)`): each `Capture`
  holds a pointer to its staging ring inside `Mixer.sources`.
- The mixer tick sleeps through `std.Io.sleep` on the
  `global_single_threaded` Io (no kernel32 import in `Mixer`).
- `NativeMixedSource` lives in `core/native_capture.py`, sharing a
  `_NativeSource` base with `NativeCaptureSource`.
- `fb_ring_peak_bins(ring, n_frames, n_bins, out)` takes a window
  length, not absolute bounds: the retry-on-lap loop must live in Zig.
- `fb_ring_rms` moves the level meter's RMS (`get_rms_levels`) into
  Zig; the spec's deletion list missed it.
- `tests/conftest.py` hard-requires the native library; no Python
  half remains to fall back to.
- Render endpoints are enumerated twice (`.loopback` and `.render`) so
  Python stays filter-only.
- `NativeScrubPlayer` drops `open()`: the stream opens lazily on the
  first `play()`.
- PR d: the `writer_active` start-window test parks `FakeBackend` in
  `open()` (a `hold` knob), not in the first `next()`: the window to
  pin is before any stream exists.
- PR d: `audio_devices.build_mixed_capture_source(...)` is the mixed
  factory, beside `build_capture_source`; both share one
  `_spec_kwargs` helper. `state.build_capture_for_slot` passes specs
  through and never picks a class itself.
- PR d rider (not in the spec): `Capture.start` and `Playback.play`
  reset `err_buf[0]` with `err_len`. `lastError()` slices
  `err_buf[0..len :0]`; a length reset alone trips the sentinel check
  on a restart after a recorded error.
- PR e: `NativeScrubPlayer.bind(audio, sample_rate)` — channels come
  from the array shape (1-D reshapes to `[N, 1]`); the checkout's rate
  is the second argument.
- PR e: wrapper `stop()` = `pause()` + `seek_samples(0)`. Zig has no
  unbind; a clip stays bound until the next `bind` or `close`.
- PR e: `bind` against a render thread mid-copy uses a two-flag
  handshake (`playing` + `in_copy`, both `seq_cst`) instead of a lock.
  `bind` clears `playing`, then spins until `in_copy` is false; the
  thread raises `in_copy` before it reads `playing` or the clip.
- PR e: after an `openRender` failure the thread exits with `done`
  set; the next `play()` joins it and spawns again, so a fixed device
  is retried without a `destroy`.
- PR e: `fb_playback_seek` takes `u64`; Python `seek_samples` clamps
  with `max(0, int(pos))` and Zig clamps to `clip_frames`.
- PR e: the FakeBackend render sink records writes through a
  test-supplied `render_allocator` (`std.testing.allocator` is a
  compile error outside a `test` block, and `FakeBackend` is analyzed
  in the DLL build).
- PR e: `Playback.max_fill_frames = 8192` caps one write; a larger
  `available()` is filled over several wakes. `fill` takes the open
  stream's channel count as a parameter so a rebind cannot resize a
  write under a stream opened at the old count.
