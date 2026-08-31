# Zig core phase 2 — capture and playback move into Zig

Date: 2026-08-16. Status: approved in brainstorm, spec under review.
Tracker: issue #17 becomes the phase-2 epic (retitle it; one sub-issue per PR).

## Goal

The audio thread never touches Python again. Zig owns capture, mixing,
and playback threads. Python keeps the Qt shell: it starts, stops,
configures, and reads. Result: no GIL jitter on the write path (#26),
three Python dependencies gone (`sounddevice`, `soundcard`,
`soundfile`), and the PortAudio DLL gone from the bundle.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Backend | Hand-written WASAPI bindings in Zig. No miniaudio, no zaudio, no zigwin32. | miniaudio has no per-process loopback (mackron/miniaudio#484, open since 2022). Every capture path here uses the same COM interfaces; one binding covers all of them plus playback. Zero-external-deps rule survives. |
| Reference for the port | `flashback_sampler/io/win32_process_loopback.py` (1181 lines) | It already declares the vtables, the format fallback chain, and the event loop. The Zig backend is a transcription plus `IMMDeviceEnumerator` and `IAudioRenderClient`. |
| Platforms | Windows only in phase 2. `Backend` interface leaves the slot for CoreAudio / ALSA / PipeWire later. | `PLATFORM.md` is Windows-first. Linux candidates: allyourcodebase/pipewire, andrewrk/pulseaudio. |
| Playback | Moves to Zig (`Playback.zig`). | Same COM layer, small surface. Removes the last `sounddevice` user. |
| Mixed capture | Moves to Zig (`Mixer.zig`), after flush semantics are settled. | Spirit of the project: no Python audio thread. Reuses `Ring` as the staging primitive. |
| Python buffer | Deleted in the last PR of the phase, with the parity harness. | Once Zig is the only implementation, parity has nothing to compare. Regression tests run against Zig alone. |
| FLAC | Dropped. | The ring is float32; WAV FLOAT is bit-identical to RAM. FLAC has no float subtype, so a FLAC checkout quantizes to int24 — less faithful, only smaller. Revisit if users ask. |
| Multichannel endpoints (#28) | Request stereo float32 with `AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM \| SRC_DEFAULT_QUALITY`; the audio engine downmixes and rate-converts. Fallback if a hardware test shows loopback rejects it: reject with a clear error. | Cheap, no mixing code in Zig. `soundcard` already uses these flags on loopback. |
| Sample rate honesty | Probe `IAudioClient::GetMixFormat` and surface the mix rate. The hires spec's "output mix format is 48 kHz" notice stays. | Requesting a higher rate cannot add information. |
| Arm-time memory | Documented, not changed. `Ring.init` commits every page at construction; a 900 s stereo 48 kHz slot is ~345 MB at arm. Add-source RAM readout gains "reserved at arm" wording. | Safe behaviour: a RAM shortfall surfaces at arm, not mid-take. Full UI treatment belongs to the UI arc (#16). |
| Carried issues | All five absorbed: #21, #26, #28 in PR a; #20, #23 in PR c. | See PR table. |

## Architecture

```
Python (Qt shell)                    Zig (flashback_core)
-----------------                    ------------------------------------------
CaptureSlot ── fb_capture_* ───────► Capture ── thread ── WasapiBackend.Stream
                                        │                     (loopback | input | process)
                                        └─► Ring.write, Summary.update, flush requests
MixedSlot  ── fb_mixer_* ──────────► Mixer ── N × Capture → staging Rings ─► mixer thread ─► target Ring
ScrubPlayer ── fb_playback_* ──────► Playback ── thread ── WasapiBackend.RenderStream
audio_devices ── fb_devices_* ─────► Backend.enumerate
waveform / checkout ── fb_ring_read, fb_ring_summary_bins, fb_wav_write   (unchanged)
```

Python holds opaque handles. No audio frame crosses the ABI on the
write path. Read paths are unchanged from phase 1.

## Zig modules (`core/src/`)

### `wasapi.zig` — COM bindings

Hand-written `extern struct` vtables and GUIDs. No code generation, no
translate-c. Surface:

- `IUnknown`, `IMMDeviceEnumerator`, `IMMDeviceCollection`, `IMMDevice`,
  `IPropertyStore` (friendly name)
- `IAudioClient` (Initialize, GetBufferSize, Start, Stop, Reset,
  SetEventHandle, GetService, GetMixFormat, IsFormatSupported)
- `IAudioCaptureClient` (GetBuffer, ReleaseBuffer, GetNextPacketSize)
- `IAudioRenderClient` (GetBuffer, ReleaseBuffer)
- `IActivateAudioInterfaceAsyncOperation` + completion handler,
  `AUDIOCLIENT_ACTIVATION_PARAMS`, `AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`
- `WAVEFORMATEX`, `WAVEFORMATEXTENSIBLE`, stream flags, `HRESULT` helpers
- kernel32: `CreateEventW`, `WaitForSingleObject`, `CloseHandle`,
  `CoInitializeEx`/`CoUninitialize` (ole32), `CoCreateInstance`

Style reference: superelectric.dev/post/post1.html (COM vtables in Zig).

### `Backend.zig` — the interface

```zig
pub const Kind = enum { loopback, input, process };
pub const Device = struct { id: []const u8, name: []const u8, kind: Kind, mix_rate: u32, mix_channels: u16 };
pub const Spec = struct { device_id: []const u8, kind: Kind, pid: u32 = 0, rate: u32, channels: u16 };
pub const Stream = struct {
    ptr: *anyopaque,
    start: *const fn (*anyopaque) Error!void,
    stop: *const fn (*anyopaque) void,
    // Blocks until the next packet or timeout; returns the packet as f32 frames
    // already in the requested rate/channels. Returns null on timeout.
    next: *const fn (*anyopaque, timeout_ms: u32) Error!?[]const f32,
    release: *const fn (*anyopaque, n_frames: usize) void,
    deinit: *const fn (*anyopaque) void,
};
pub const Backend = struct {
    ptr: *anyopaque,
    enumerate: *const fn (*anyopaque, std.mem.Allocator) Error![]Device,
    open: *const fn (*anyopaque, Spec) Error!Stream,
    openRender: *const fn (*anyopaque, Spec) Error!RenderStream,
};
```

Two implementations: `WasapiBackend` (Windows) and `FakeBackend`
(tests: scripted packets, injectable errors, controllable timing).
`Capture`, `Mixer`, and `Playback` take a `Backend` and never import
`wasapi.zig`.

### `Capture.zig`

One per source. Owns a `std.Thread`. Loop: `stream.next()` → apply
gain via existing `Ring.write` path → `Summary.update` → check flush
request → repeat. Fields exposed through the ABI: `running`,
`frames_written`, `xruns` (WASAPI `AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY`
plus timeout gaps), `last_error` (fixed 256-byte buffer, no allocation
in the loop), `mix_rate` (for the honesty notice).

Flush (#20): `fb_ring_flush` becomes a request flag the capture thread
executes between packets. The writer and the flush are the same thread,
so nothing can republish over it. `Ring.flush` stays for the standalone
case (no capture attached) and is documented as single-writer-only.

Summary (#23): `Summary` gets the same seqlock discipline as `Ring`
(generation counter around bin updates; readers retry).

`Ring.init` (#21): validates `rate > 0`, `channels` in `1..2`,
`seconds > 0`, and returns `error.InvalidArgument`. The ABI's guard
becomes a pass-through.

WAV `data_len` (#28): `wav.writeFile` returns `error.TooLong` past `u32`
bytes; the ABI maps it to a status code.

### `Mixer.zig`

Port of `MixedCaptureSource`: N `Capture`s each writing a 2 s staging
`Ring`, one mixer thread polling every 10 ms, reading the common
available span, summing, hard-clipping to [-1, 1], writing the target
`Ring`. Same `xruns`/`last_error` shape as `Capture`, so `CaptureSlot`
treats both alike. Lands after PR c so the mixer thread inherits the
settled flush semantics.

### `Playback.zig`

Port of `ScrubPlayer`: one render thread, a bound `[]const f32` source
(Zig-owned copy made at `bind`), cursor, `playing` flag, zero-fill on
pause, auto-stop at end. Seek is an atomic cursor store. Uses
`Backend.openRender`.

### `abi.zig` additions

```
fb_devices_list(out: *FbDeviceList) FbStatus   / fb_devices_free(*FbDeviceList)
fb_capture_create(ring, summary, spec: *const FbCaptureSpec) ?*Capture
fb_capture_start / fb_capture_stop / fb_capture_destroy
fb_capture_stats(*Capture, out: *FbCaptureStats) void      // running, frames, xruns, mix_rate
fb_capture_last_error(*Capture) [*:0]const u8
fb_capture_request_flush(*Capture) void
fb_mixer_create(target_ring, summary, specs: [*]const FbCaptureSpec, n) ?*Mixer  (+ start/stop/destroy/stats)
fb_playback_create(device_id, rate, channels) ?*Playback
fb_playback_bind(*Playback, frames, n_frames) FbStatus / seek / play / pause / state / destroy
fb_last_error() [*:0]const u8      // new: thread-local message for a null-returning create
```

`flashback_core.h` mirrors every addition. `native.py` grows the ctypes
signatures; each Python replacement module is a thin handle wrapper
that satisfies the existing `CaptureSource` protocol (`start`, `stop`,
`is_running`, `xrun_count`, `last_error`).

## Error handling

- Zig side: `error` sets on `open`/`start`; the audio loop never returns
  an error, it records `last_error` and stops (`running = false`).
- ABI: `FbStatus` codes as in phase 1; null handle on create failure with
  `fb_last_error()` for the message.
- Python: `CaptureSlot` polls `stats` on its existing timer and surfaces
  `last_error` in the source-status widget, unchanged.
- COM apartment: each Zig audio thread calls `CoInitializeEx(MTA)` on
  entry and `CoUninitialize` on exit. Python never initializes COM for
  audio after this phase.

## Testing

- **Zig unit tests via `FakeBackend`**: format negotiation order, xrun
  accounting on discontinuity flags and timeouts, flush ordering
  (write / flush / write yields only post-flush frames), `Summary`
  seqlock retry, `Ring.init` rejects, mixer common-span math and clip,
  playback cursor / pause / end-of-source. Gate: **the test count must
  rise** per PR (`refAllDecls` is one level deep; new files must be
  reachable from `root.zig`).
- **Python tests**: handle wrappers tested with `native` mocked; the
  existing `CaptureSource` protocol tests run unchanged against the
  wrappers.
- **Hardware tests** (`audio_hw` marker, CI-excluded): loopback,
  mic, process loopback, playback, mixed. Owner runs them on the
  Windows box before each merge. Unraid box later for Linux.
- **Soak**: `soak_test.py` before PR a and after PR g on the same
  branch. The write-tail-latency number closes #26. Also record
  Task Manager RSS and CPU% of the app idle-armed, before and after —
  that is the "less chunky" claim.
- CI: existing 6 legs. Cross-compile check keeps building the core for
  macOS/Linux; `wasapi.zig` is behind `builtin.os.tag == .windows`.

## PR sequence

App works at every merge. One sub-issue per PR under epic #17.

| PR | Delivers | Deletes | Closes |
|---|---|---|---|
| a | `wasapi.zig`, `Backend.zig`, `WasapiBackend` (loopback + input), `FakeBackend`, `Capture.zig`, ABI + header + `native.py`, `Ring.init` validation, WAV length guard, `capture.py` and `loopback_capture.py` become handle wrappers | Python threads in those two files | #21, #28, #26 (measured, comment with numbers) |
| b | Process loopback via `ActivateAudioInterfaceAsync` on the same backend | `io/win32_process_loopback.py` | — |
| c | Flush as capture-thread request; `Summary` seqlock | — | #20, #23 |
| d | `Mixer.zig`; `mixed_capture.py` becomes a handle wrapper | Python mixer thread | — |
| e | `Playback.zig`; `scrub_player.py` becomes a handle wrapper | last `sounddevice` import | — |
| f | `fb_devices_*`; `audio_devices.py` reads the ABI | `soundcard`/`sounddevice` probing | — |
| g | Delete Python `AudioCircularBuffer`, `make_ring_buffer` fallback, parity harness, `RingDerivedOps` dual-impl seams; drop `sounddevice`, `soundcard`, `soundfile` from `pyproject.toml` and the spec; remove FLAC menu items; docs: arm-time memory, RAM readout wording, `PLATFORM.md`, `README.md`; final soak | Python buffer + deps | #17 (by hand, after numbers land) |

Each PR description carries a "Zig concepts in this PR" section
(COM vtables in Zig, `*anyopaque` interfaces, `std.Thread`, atomics,
`builtin.os.tag` gating, error sets across the ABI).

## Out of scope

Non-Windows backends (slot only), FLAC, resampling in Zig, downmix
code in Zig (engine does it; reject is the fallback), any UI beyond
the RAM-readout wording, CLAP/VST hosts, the UI arc (#16).

## Risks to measure, not assume

- `AUTOCONVERTPCM` on loopback streams: verify on hardware in PR a
  before relying on it for #28.
- Process-loopback `IAudioClient` has no device: `GetMixFormat` returns
  `E_NOTIMPL`; keep the format fallback chain from the Python port.
- COM apartment rules with Qt's own COM use in the same process: audio
  threads are MTA and Qt's are STA; they do not share interface
  pointers, so this should be safe. Verify on hardware.
- Cross-compile legs must still build with `wasapi.zig` present.

## Deviations recorded by the part-1 plan (2026-08-16)

The plan (`docs/superpowers/plans/2026-08-16-zig-core-phase2-capture.md`)
is authoritative where it differs below; the spec is amended, not
overruled.

- `fb_capture_create(ring, spec)` — no separate `summary` argument; the
  `Summary` lives inside `Ring`.
- Device enumeration ships in PR a (opening a device by id needs it) and
  process enumeration in PR b; output-device enumeration moves with
  playback. The spec's PR f (enumeration) folds into a/b/e, so the phase
  is six PRs: a capture, b process loopback, c flush + summary, d mixer,
  e playback, f delete Python buffer + deps.
- Flush (#20) is fixed inside `Ring` (`writer_active` + `flush_pending`;
  the writer executes a pending flush before its next write), not in
  `Capture`. `fb_ring_flush` and Python are unchanged.
- Loopback, input, and process streams all poll WASAPI (no
  `EVENTCALLBACK`): one loop for every kind, and it sidesteps the known
  event-driven-loopback quirk.
- The plan covers PRs a–c. PRs d–f get a second plan after PR a merges.

## Deviations recorded during PR a (2026-08-20)

- `RoInitialize` is resolved at runtime via `LoadLibraryW`/
  `GetProcAddress` against `combase.dll`, not `extern "combase"`: Zig
  0.16 bundles no `combase` import lib, so linking against it fails
  the build. Same approach `win32_process_loopback.py` already used
  with `ctypes.WinDLL("combase.dll")`.
- `guid()`'s inner `comptime { }` block is removed: Zig 0.16 rejects a
  function returning a comptime-only value from a runtime call site
  (the tests call `guid()` at runtime). The `comptime s` parameter
  already forces compile-time evaluation everywhere the brief needs
  it; dropping the inner block does not change output for any input,
  and the byte-exact GUID tests pin that.
- `NativeCaptureSource` is inert after `close()`: a closed handle is
  `None` on the Python side, so `start()` raises, and `last_error()` /
  `xrun_count()` / `mix_rate()` return `None` / zero instead of
  reaching the freed Zig handle. Beyond the plan's text; added because
  `fb_capture_last_error` and `fb_capture_stats` take a non-optional
  pointer, so passing a freed handle through is undefined behavior in
  the DLL, not a catchable Python exception.
- Capture (loopback, mic/line-in, per-process) is Windows-only as of
  this PR; the README and module docstrings say so.

## Deviations recorded by part 2 (2026-08-30)

Part 2 (PRs d, e, f) is specified in
`2026-08-30-zig-core-phase2-d-f-design.md`, which wins where the two
differ. Recorded there by the PR f plan: `fb_ring_peak_bins` takes a
window length, not absolute bounds; `fb_ring_rms` moves the level
meter's RMS into Zig; `tests/fixtures/wavread.py` replaces soundfile as
the WAV oracle; the test session hard-requires the native library.
