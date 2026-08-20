# Zig Core Phase 2 — Part 1 (capture) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture runs on a Zig-owned thread that writes straight into `Ring`; Python only starts, stops, and reads. Covers spec PRs **a** (WASAPI backend, device loopback + mic, device enumeration), **b** (per-process loopback), **c** (flush on the writer thread, `Summary` seqlock). PRs d–f (Mixer, Playback, delete the Python buffer) get their own plan once PR a is merged and `Backend.zig` exists as code to cite — the phase-1 handoff recorded that most defects came from plan text written ahead of running code.

**Architecture:** `Backend.zig` is a `*anyopaque` + vtable interface (the `std.mem.Allocator` idiom): `enumerate`, `open`. `open` returns a `Stream` whose `next()` yields f32 packets already converted to the requested rate/channels. `WasapiBackend.zig` implements it over hand-written COM bindings in `wasapi.zig`; `FakeBackend.zig` implements it for tests. `Capture.zig` owns a `std.Thread` that loops `stream.next()` → `Ring.write()` and publishes stats through atomics. `abi.zig` exports handles; `native.py` binds them; `native_capture.py` is the `CaptureSource` wrapper.

**Tech Stack:** Zig 0.16.0 (pinned; zero external deps; Windows COM via `extern` + vtable structs), ctypes, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-16-zig-core-phase2-design.md` — read it first. Deviations recorded there by this plan: `fb_capture_create` takes `(ring, spec)` (the `Summary` lives inside `Ring`); device enumeration lands in PR a (needed to open a device by id) and process enumeration in PR b, so the spec's PR f collapses into a/b/e; flush (#20) is fixed inside `Ring` (`writer_active` + `flush_pending`), not in `Capture`; loopback and input streams poll (no `EVENTCALLBACK`) — one loop for every kind.

**Amended 2026-08-20** after a pre-flight verification pass (~160 claims checked against the working tree; 27 findings). Material changes: branches and PRs target `dev`, not `main` (CI budget is spent — no CI runs on feature branches or PRs; local gates are the merge gate); capture threads init with `RoInitialize`, not `CoInitializeEx` (the port proved `ActivateAudioInterfaceAsync` fails with `E_ILLEGAL_METHOD_CALL` under COM-only init); the process-loopback build floor stays 19041 (the plan had silently raised it to 20348); `FbProcess` carries `ppid` so the `resolve_audio_root_pid` behaviour (same-named ancestor walk — Spotify/Chrome child PIDs) survives the port; Task 12's generation assertions account for `Summary.init` calling `poison()`; the sequester steps account for `_ToRemove/` being gitignored; the straggler sweeps now reach `flashback_sampler.spec`, `core/__init__.py`, `packaging/`, and `soak_test.py`.

## Global Constraints

- **Zero external Zig dependencies.** `core/build.zig.zon` never gains a `.dependencies` entry. No zigwin32, no miniaudio, no translate-c.
- **Zig 0.16.0 pinned** in `core/build.zig.zon` (`minimum_zig_version = "0.16.0"`) and all three CI `mlugg/setup-zig` `version:` fields (`test.yml` pytest job, `test.yml` zig job, `release.yml`). Never float.
- **Pre-1.0 std drift is expected.** Snippets target 0.16; if a std call does not resolve, fix the call site to the pinned std API and keep the design. Tests define behaviour. Known 0.16 facts from phase 1: `std.Thread.spawn/join/yield` exist; there is no `std.Thread.sleep` (use kernel32 `Sleep` on Windows, spin+yield in tests); `std.Io.Mutex` needs an `Io` (`std.Io.Threaded.global_single_threaded.io()`); `refAllDecls` is one level deep.
- **RT-safety invariant:** `Ring.write` and the capture loop never lock, allocate, or fail. Any change adding a lock/alloc/error path to the loop is wrong regardless of what it fixes.
- **Windows-only backend, OS-gated.** `wasapi.zig` and `WasapiBackend.zig` are imported only under `builtin.os.tag == .windows`. The cross-compile legs (`x86_64-windows`, `aarch64-macos`, `x86_64-linux-gnu`) must stay green.
- **Idiomatic Zig, not Python-in-Zig:** file-as-struct, caller-supplied allocators, error sets internally / status codes only at the ABI, `*anyopaque` + vtable for the interface, no speculative comptime.
- **Instructional comments** (owner directive): where a Zig concept first load-bears (`callconv(.winapi)`, `extern struct` vtables, `*anyopaque` interfaces, atomics, `builtin.os.tag`), a short comment says what it buys. Each PR description carries a "Zig concepts in this PR" section.
- **TDD + mutation-check:** every test seen red before green; compound guards get one mutation per clause; verify by edit-then-revert on the real source. **The gate for Zig tests is "the count rose"**, not "it passed" — a new file not re-exported from `root.zig` runs zero tests silently.
- **Shipped optimize mode is ReleaseSafe.** Zig tests run in Debug.
- **PRs → `dev`** (the default branch), one per task-group below; the app must work at every merge. Owner merges. **The CI-minutes budget is spent: no workflow runs on feature branches, `dev`, or PRs.** The merge gate is local: `python -m pytest tests/unit -q -m "not audio_hw and not perf"` + `zig build --build-file core/build.zig test --summary all` + the cross-compile builds, run before every push. CI fires only when the owner promotes `dev` → `main` in a batch — that is where the phase-1 "watch the zig job's duration, a hang shows as cancelled" lesson applies. Deletion policy: sequester to `_ToRemove/`, never `rm -rf` — and note `_ToRemove/` is **gitignored**, so moves into it stage nothing; see the sequester recipes in Tasks 7 and 10. `CLAUDE.md` is gitignored — restate load-bearing rules in dispatches.
- **Execute in the primary checkout, not a worktree.** `soak_test.py` and `ZIG-101.md` are untracked repo-root files and `CLAUDE.md` is gitignored; a worktree has none of them. Branch-per-PR in the main checkout.
- **Shell on this machine:** no `cd` compounds, no `$( )`, no `&&`. Use `zig build --build-file core/build.zig test` and `--build-file` for every zig invocation.
- Python side: no new pip dependencies; `native.py` / `native_capture.py` are ctypes + numpy only.
- **Issues are status truth.** Open a sub-issue when a PR is scoped, comment when something material is learned, close via `Closes #NN` in the PR body. Tick the epic (#17) checkbox on merge.

**Task → PR map:** Task 0 = setup (no PR) · Tasks 1–8 = PR **a** `feat/zig-capture` · Tasks 9–10 = PR **b** `feat/zig-process-loopback` · Tasks 11–13 = PR **c** `feat/zig-flush-summary`.

**Windows constants used throughout** (from the Windows SDK; where the Python port declares one — about half of these — the value below matches it byte-for-byte; the rest are SDK-only, trust this table):

| Name | Value |
|---|---|
| `COINIT_MULTITHREADED` | `0x0` |
| `CLSCTX_ALL` | `0x17` |
| `eRender` / `eCapture` | `0` / `1` |
| `eConsole` | `0` |
| `DEVICE_STATE_ACTIVE` | `0x1` |
| `STGM_READ` | `0` |
| `AUDCLNT_SHAREMODE_SHARED` | `0` |
| `AUDCLNT_STREAMFLAGS_LOOPBACK` | `0x00020000` |
| `AUDCLNT_STREAMFLAGS_EVENTCALLBACK` | `0x00040000` (not used — see Task 5) |
| `AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM` | `0x80000000` |
| `AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY` | `0x08000000` |
| `AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY` | `0x1` |
| `AUDCLNT_BUFFERFLAGS_SILENT` | `0x2` |
| `WAVE_FORMAT_PCM` / `WAVE_FORMAT_IEEE_FLOAT` | `1` / `3` |
| `VT_LPWSTR` / `VT_BLOB` | `31` / `0x41` |
| `TH32CS_SNAPPROCESS` | `0x2` |
| `WAIT_OBJECT_0` / `WAIT_TIMEOUT` | `0` / `0x102` |
| `CLSID_MMDeviceEnumerator` | `{BCDE0395-E52F-467C-8E3D-C4579291692E}` |
| `IID_IMMDeviceEnumerator` | `{A95664D2-9614-4F35-A746-DE8DB63617E6}` |
| `IID_IAudioClient` | `{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}` |
| `IID_IAudioCaptureClient` | `{C8ADBD64-E71E-48A0-A4DE-185C395CD317}` |
| `IID_IActivateAudioInterfaceCompletionHandler` | `{41D949AB-9862-444A-80F6-C261334DA5EB}` |
| `IID_IAgileObject` | `{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}` |
| `IID_IUnknown` | `{00000000-0000-0000-C000-000000000046}` |
| `PKEY_Device_FriendlyName` | fmtid `{A45C254E-DF1C-4EFD-8020-67D146A850E0}`, pid `14` |

---

### Task 0: Toolchain, tracker, and the "before" number

**Files:** none (gh + shell only)

**Interfaces:**
- Produces: sub-issue numbers for PRs a, b, c; the pre-phase-2 soak numbers posted on epic #17.

- [ ] **Step 1: Verify toolchain**

Run: `zig version`
Expected: `0.16.0`. Run: `zig build --build-file core/build.zig test --summary all` and note the test count printed (phase 1 ended at 44). Every later Zig task must raise this number.

- [ ] **Step 2: Baseline the Python suite**

Run: `python -m pytest tests/unit -q -m "not audio_hw and not perf"`
Expected: green (phase 1 ended at 524 passed).

- [ ] **Step 3: Record the "before" soak**

Run: `python soak_test.py` for the default duration with audio playing through the default output. Post the printed table (frames written, dropped_callbacks, discontinuities, shortfall) as a comment on epic #17 titled "Phase 2 — before". Also record from Task Manager: app RSS and CPU % with one 60 s slot armed and idle. This is the number PR a's tail-latency measurement (#26) is compared against.

- [ ] **Step 4: Open the sub-issues**

Run three `gh issue create` calls, one per PR, and edit the epic's task list (`gh issue edit 17 --body ...`) to reference them:

- "Zig core p2-a: WASAPI backend + Capture (loopback, mic) + device enumeration" — body: link spec, list Tasks 1–8, note "Closes #21, #28 by construction; #26 measured here".
- "Zig core p2-b: per-process loopback on the Zig backend" — Tasks 9–10.
- "Zig core p2-c: flush on the writer thread + Summary seqlock" — Tasks 11–13, "Closes #20, #23".

Record the three numbers as `<A>`, `<B>`, `<C>` — used in PR bodies below.

- [ ] **Step 5: Branch**

Run: `git checkout -b feat/zig-capture dev`

---

### Task 1: `convert.zig` — one WASAPI packet → interleaved f32

**Files:**
- Create: `core/src/convert.zig`
- Modify: `core/src/root.zig` (re-export)

**Interfaces:**
- Produces:
  ```zig
  pub const SampleTag = enum(u8) { f32 = 0, i16 = 1 };
  pub const SourceFormat = struct { tag: SampleTag, channels: u16 };
  /// Writes `n_frames` frames of `src` into `out` as interleaved f32 with `dst_channels`.
  /// `silent` zero-fills. Returns the written slice: out[0 .. n_frames * dst_channels].
  /// Asserts out.len >= n_frames * dst_channels. No allocation.
  pub fn packet(src: [*]const u8, n_frames: usize, fmt: SourceFormat, silent: bool, dst_channels: u16, out: []f32) []f32
  ```
  Channel rule (mirrors `win32_process_loopback.py:1143-1154`): equal → copy; 2→1 mean; 1→2 duplicate; else copy `min(src,dst)` channels and zero the rest.

- [ ] **Step 1: Failing tests**

Create `core/src/convert.zig`:

```zig
//! Pure sample-format conversion for one capture packet. No OS calls,
//! no allocation — testable everywhere, used by WasapiBackend on Windows.
const std = @import("std");

pub const SampleTag = enum(u8) { f32 = 0, i16 = 1 };
pub const SourceFormat = struct { tag: SampleTag, channels: u16 };

test "f32 stereo → stereo is a straight copy" {
    const src = [_]f32{ 0.1, -0.1, 0.2, -0.2 };
    var out: [4]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 2, .{ .tag = .f32, .channels = 2 }, false, 2, &out);
    try std.testing.expectEqualSlices(f32, &src, got);
}

test "i16 scales by 1/32768" {
    const src = [_]i16{ 32767, -32768, 0, 16384 };
    var out: [4]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 2, .{ .tag = .i16, .channels = 2 }, false, 2, &out);
    try std.testing.expectApproxEqAbs(@as(f32, 32767.0 / 32768.0), got[0], 1e-6);
    try std.testing.expectApproxEqAbs(@as(f32, -1.0), got[1], 1e-6);
    try std.testing.expectApproxEqAbs(@as(f32, 0.0), got[2], 1e-6);
    try std.testing.expectApproxEqAbs(@as(f32, 0.5), got[3], 1e-6);
}

test "silent flag zero-fills regardless of source bytes" {
    const src = [_]f32{ 0.9, 0.9 };
    var out: [2]f32 = .{ 7, 7 };
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 1, .{ .tag = .f32, .channels = 2 }, true, 2, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0, 0 }, got);
}

test "stereo → mono averages" {
    const src = [_]f32{ 0.5, -0.5, 1.0, 0.0 };
    var out: [2]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 2, .{ .tag = .f32, .channels = 2 }, false, 1, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0.0, 0.5 }, got);
}

test "mono → stereo duplicates" {
    const src = [_]f32{ 0.25, -0.75 };
    var out: [4]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 2, .{ .tag = .f32, .channels = 1 }, false, 2, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0.25, 0.25, -0.75, -0.75 }, got);
}

test "6 → 2 keeps the first two channels" {
    const src = [_]f32{ 1, 2, 3, 4, 5, 6 };
    var out: [2]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 1, .{ .tag = .f32, .channels = 6 }, false, 2, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 2 }, got);
}

test "returned slice length is n_frames * dst_channels" {
    const src = [_]f32{ 0, 0, 0, 0 };
    var out: [8]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 2, .{ .tag = .f32, .channels = 2 }, false, 2, &out);
    try std.testing.expectEqual(@as(usize, 4), got.len);
}
```

Add to `core/src/root.zig` after the `wav` line: `pub const convert = @import("convert.zig");`

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test`
Expected: compile error, `packet` undefined. (Compile-error red counts.)

- [ ] **Step 3: Implement**

Append to `core/src/convert.zig`:

```zig
pub fn packet(src: [*]const u8, n_frames: usize, fmt: SourceFormat, silent: bool, dst_channels: u16, out: []f32) []f32 {
    const dst_len = n_frames * dst_channels;
    std.debug.assert(out.len >= dst_len);
    const dst = out[0..dst_len];
    if (silent) {
        @memset(dst, 0);
        return dst;
    }
    const sc: usize = fmt.channels;
    const dc: usize = dst_channels;
    var f: usize = 0;
    while (f < n_frames) : (f += 1) {
        // One frame at a time; sample() hides the i16/f32 difference so the
        // channel rule below is written once.
        if (sc == dc) {
            for (0..dc) |c| dst[f * dc + c] = sample(src, fmt.tag, f * sc + c);
        } else if (sc == 2 and dc == 1) {
            dst[f] = 0.5 * (sample(src, fmt.tag, f * 2) + sample(src, fmt.tag, f * 2 + 1));
        } else if (sc == 1 and dc == 2) {
            const s = sample(src, fmt.tag, f);
            dst[f * 2] = s;
            dst[f * 2 + 1] = s;
        } else {
            const keep = @min(sc, dc);
            for (0..keep) |c| dst[f * dc + c] = sample(src, fmt.tag, f * sc + c);
            for (keep..dc) |c| dst[f * dc + c] = 0;
        }
    }
    return dst;
}

inline fn sample(src: [*]const u8, tag: SampleTag, index: usize) f32 {
    return switch (tag) {
        // The packet is a byte pointer from WASAPI; reinterpret per sample.
        // `[*]align(1) const T` — WASAPI does not promise 4-byte alignment.
        .f32 => @as([*]align(1) const f32, @ptrCast(src))[index],
        .i16 => @as(f32, @floatFromInt(@as([*]align(1) const i16, @ptrCast(src))[index])) / 32768.0,
    };
}
```

- [ ] **Step 4: Run, verify green and count rose by 7**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: all pass; count = Task 0 count + 7. Mutation check: change `0.5 *` to `1.0 *` in the 2→1 branch → "stereo → mono averages" reddens; revert.

- [ ] **Step 5: Commit**

```bash
git add core/src/convert.zig core/src/root.zig
git commit -m "feat(core): convert.zig — one WASAPI packet to interleaved f32"
```

---

### Task 2: `Backend.zig` interface + `FakeBackend.zig`

**Files:**
- Create: `core/src/Backend.zig`, `core/src/FakeBackend.zig`
- Modify: `core/src/root.zig`

**Interfaces:**
- Produces (every later task uses these names exactly):
  ```zig
  // Backend.zig
  pub const Kind = enum(u8) { loopback = 0, input = 1, process = 2 };
  pub const Error = error{ DeviceNotFound, FormatRejected, ActivationFailed, Unsupported, OutOfMemory };
  /// extern so the ABI passes it through unchanged (Task 6). UTF-8, NUL-terminated, truncated to fit.
  pub const Device = extern struct { kind: u8, is_default: u8, mix_rate: u32, mix_channels: u16, id: [128]u8, name: [128]u8 };
  pub const Spec = struct { kind: Kind, device_id: []const u8, pid: u32 = 0, rate: u32, channels: u16 };
  pub const Packet = struct { frames: []const f32, discontinuity: bool = false };
  pub const Stream = struct {
      ptr: *anyopaque,
      vtable: *const VTable,
      pub const VTable = struct {
          /// Blocks up to timeout_ms. null = nothing arrived. Frames are valid until the next call.
          next: *const fn (*anyopaque, timeout_ms: u32) Error!?Packet,
          /// Idempotent. Unblocks a concurrent next(). Called from the control thread.
          stop: *const fn (*anyopaque) void,
          deinit: *const fn (*anyopaque) void,
          mixRate: *const fn (*anyopaque) u32,
      };
      pub fn next(s: Stream, timeout_ms: u32) Error!?Packet
      pub fn stop(s: Stream) void
      pub fn deinit(s: Stream) void
      pub fn mixRate(s: Stream) u32
  };
  pub const Backend = struct {
      ptr: *anyopaque,
      vtable: *const VTable,
      pub const VTable = struct {
          /// Fills `out`, returns count. Never fails; an empty machine returns 0.
          enumerate: *const fn (*anyopaque, out: []Device) usize,
          /// Opens AND starts the stream. Called on the capture thread.
          open: *const fn (*anyopaque, Spec) Error!Stream,
      };
      pub fn enumerate(b: Backend, out: []Device) usize
      pub fn open(b: Backend, spec: Spec) Error!Stream
  };
  ```
  ```zig
  // FakeBackend.zig — test double
  pub const FakeBackend = @This();
  packets: []const []const f32,        // delivered once each, in order
  discontinuity_at: ?usize = null,     // that packet carries discontinuity=true
  open_error: ?Backend.Error = null,   // open() fails with this
  mix_rate: u32 = 48_000,
  devices: []const Backend.Device = &.{},
  // observed
  opened: std.atomic.Value(bool), stopped: std.atomic.Value(bool), delivered: std.atomic.Value(usize), last_spec: ?Backend.Spec
  pub fn init(packets: []const []const f32) FakeBackend
  pub fn backend(self: *FakeBackend) Backend.Backend
  ```
  Fake `next` semantics: hand out the next packet immediately; when exhausted, spin-yield until `stopped` then return `null`.

- [ ] **Step 1: Failing test (interface round-trip through the fake)**

Create `core/src/Backend.zig` with the declarations above (all types, the two structs' thin `pub fn` forwarders such as `pub fn next(s: Stream, timeout_ms: u32) Error!?Packet { return s.vtable.next(s.ptr, timeout_ms); }`), preceded by:

```zig
//! The audio-backend interface. `*anyopaque` + a vtable of function
//! pointers is the std idiom (std.mem.Allocator, std.Io) for "many
//! implementations, one caller": Capture never learns which backend it
//! runs on, so WasapiBackend and FakeBackend are interchangeable, and a
//! CoreAudio/ALSA backend later is one more file, not a Capture change.
```

Create `core/src/FakeBackend.zig` with only this test at the bottom (types above declared but functions unimplemented):

```zig
test "fake backend hands out packets in order then null after stop" {
    var fake = FakeBackend.init(&.{ &[_]f32{ 1, 1 }, &[_]f32{ 2, 2 } });
    const be = fake.backend();
    const stream = try be.open(.{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 });
    defer stream.deinit();
    try std.testing.expect(fake.opened.load(.acquire));
    const p1 = (try stream.next(10)) orelse return error.Expected;
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 1 }, p1.frames);
    const p2 = (try stream.next(10)) orelse return error.Expected;
    try std.testing.expectEqualSlices(f32, &[_]f32{ 2, 2 }, p2.frames);
    stream.stop();
    try std.testing.expectEqual(@as(?Backend.Packet, null), try stream.next(10));
    try std.testing.expectEqual(@as(u32, 48_000), stream.mixRate());
}

test "fake backend open_error propagates" {
    var fake = FakeBackend.init(&.{});
    fake.open_error = error.DeviceNotFound;
    try std.testing.expectError(error.DeviceNotFound, fake.backend().open(.{ .kind = .input, .device_id = "x", .rate = 48_000, .channels = 2 }));
}

test "fake backend enumerate copies its device list" {
    var fake = FakeBackend.init(&.{});
    var dev = std.mem.zeroes(Backend.Device);
    dev.kind = @intFromEnum(Backend.Kind.loopback);
    dev.mix_rate = 44_100;
    fake.devices = &.{dev};
    var out: [4]Backend.Device = undefined;
    try std.testing.expectEqual(@as(usize, 1), fake.backend().enumerate(&out));
    try std.testing.expectEqual(@as(u32, 44_100), out[0].mix_rate);
}
```

Add both files to `core/src/root.zig`: `pub const Backend = @import("Backend.zig");` and `pub const FakeBackend = @import("FakeBackend.zig");`

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test`
Expected: compile error (missing `init`/`backend`).

- [ ] **Step 3: Implement FakeBackend**

```zig
//! Test double for Backend. Scripted packets, injectable failures,
//! observable lifecycle. Lives in src/ (not a test dir) so Capture.zig's
//! tests can @import it. root.zig's refAllDecls does analyze it, but
//! nothing in abi.zig references it, so none of it is exported from the
//! shared library.
const std = @import("std");
const Backend = @import("Backend.zig");
const FakeBackend = @This();

packets: []const []const f32,
discontinuity_at: ?usize = null,
open_error: ?Backend.Error = null,
mix_rate: u32 = 48_000,
devices: []const Backend.Device = &.{},
opened: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
stopped: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
delivered: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),
last_spec: ?Backend.Spec = null,

pub fn init(packets: []const []const f32) FakeBackend {
    return .{ .packets = packets };
}

pub fn backend(self: *FakeBackend) Backend.Backend {
    return .{ .ptr = self, .vtable = &backend_vtable };
}

const backend_vtable = Backend.Backend.VTable{ .enumerate = enumerate, .open = open };
const stream_vtable = Backend.Stream.VTable{ .next = next, .stop = stop, .deinit = deinit, .mixRate = mixRate };

fn enumerate(ptr: *anyopaque, out: []Backend.Device) usize {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    const n = @min(out.len, self.devices.len);
    @memcpy(out[0..n], self.devices[0..n]);
    return n;
}

fn open(ptr: *anyopaque, spec: Backend.Spec) Backend.Error!Backend.Stream {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    if (self.open_error) |e| return e;
    self.last_spec = spec;
    self.opened.store(true, .release);
    return .{ .ptr = self, .vtable = &stream_vtable };
}

fn next(ptr: *anyopaque, timeout_ms: u32) Backend.Error!?Backend.Packet {
    _ = timeout_ms;
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    const i = self.delivered.load(.acquire);
    if (i < self.packets.len) {
        self.delivered.store(i + 1, .release);
        return .{ .frames = self.packets[i], .discontinuity = (self.discontinuity_at orelse std.math.maxInt(usize)) == i };
    }
    // Exhausted: behave like a quiet device until stop() — a bounded wait
    // so a test that forgets stop() fails instead of hanging.
    var spins: u32 = 0;
    while (!self.stopped.load(.acquire) and spins < 1_000_000) : (spins += 1) std.Thread.yield() catch {};
    return null;
}

fn stop(ptr: *anyopaque) void {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    self.stopped.store(true, .release);
}

fn deinit(ptr: *anyopaque) void {
    _ = ptr;
}

fn mixRate(ptr: *anyopaque) u32 {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    return self.mix_rate;
}
```

- [ ] **Step 4: Run, verify green, count +3**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: pass; count rose by 3. `zig fmt --check core/src` clean.

- [ ] **Step 5: Commit**

```bash
git add core/src/Backend.zig core/src/FakeBackend.zig core/src/root.zig
git commit -m "feat(core): Backend interface + FakeBackend test double"
```

---

### Task 3: `Capture.zig` — the thread that feeds the ring

**Files:**
- Create: `core/src/Capture.zig`
- Modify: `core/src/root.zig`

**Interfaces:**
- Consumes: `Ring` (`write`, `total_written`), `Backend.*`, `FakeBackend`.
- Produces:
  ```zig
  pub const Capture = @This();
  pub const Stats = extern struct { running: u8, frames_written: u64, xruns: u32, mix_rate: u32 };
  pub const max_device_id = 256;
  pub const max_error = 256;
  pub fn init(ring: *Ring, backend: Backend.Backend, spec: Backend.Spec) Capture   // copies spec.device_id into id_buf; truncates at max_device_id-1
  pub fn start(self: *Capture) !void        // spawns; error.AlreadyRunning / spawn errors
  pub fn stop(self: *Capture) void          // idempotent; joins
  pub fn stats(self: *const Capture) Stats
  pub fn lastError(self: *const Capture) [:0]const u8
  ```
  Thread loop (the whole of it):
  1. `stream = backend.open(spec)` — on error: format `"open failed: {s}"` into `err_buf`, `running=false`, return.
  2. `mix_rate.store(stream.mixRate())`, `running=true`.
  3. `while (!stop_flag)`: `pkt = stream.next(100) catch { record error; break }`; if `pkt`: `if (pkt.discontinuity) xruns += 1`; `ring.write(pkt.frames)`; `frames_written += frames`.
  4. `stream.stop(); stream.deinit(); running=false`.
  `stop()` sets `stop_flag`, then joins. `frames_written` counts frames (`frames.len / ring.channels`).

- [ ] **Step 1: Failing tests**

Create `core/src/Capture.zig` with the header comment and struct fields, plus these tests (implementation bodies missing):

```zig
//! One capture source: a Zig-owned thread pulling packets from a
//! Backend.Stream and writing them into a Ring. Python never sees a
//! frame; it starts/stops this and polls stats(). All shared state is
//! atomics — the loop never locks, allocates, or fails.
const std = @import("std");
const Ring = @import("Ring.zig");
const Backend = @import("Backend.zig");
const FakeBackend = @import("FakeBackend.zig");
const Capture = @This();

pub const Stats = extern struct { running: u8, frames_written: u64, xruns: u32, mix_rate: u32 };
pub const max_device_id = 256;
pub const max_error = 256;

ring: *Ring,
backend: Backend.Backend,
kind: Backend.Kind,
pid: u32,
rate: u32,
channels: u16,
id_buf: [max_device_id]u8,
id_len: usize,
thread: ?std.Thread = null,
stop_flag: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
running: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
frames_written: std.atomic.Value(u64) = std.atomic.Value(u64).init(0),
xruns: std.atomic.Value(u32) = std.atomic.Value(u32).init(0),
mix_rate: std.atomic.Value(u32) = std.atomic.Value(u32).init(0),
err_buf: [max_error]u8 = [_]u8{0} ** max_error,
err_len: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),

fn waitUntil(cap: *Capture, comptime pred: fn (*Capture) bool) !void {
    var spins: u32 = 0;
    while (!pred(cap) and spins < 5_000_000) : (spins += 1) std.Thread.yield() catch {};
    if (!pred(cap)) return error.Timeout;
}

test "capture writes every packet into the ring and counts frames" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{ &[_]f32{ 0.1, 0.2, 0.3, 0.4 }, &[_]f32{ 0.5, 0.6 } });
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct { fn f(c: *Capture) bool { return c.frames_written.load(.acquire) == 3; } }.f);
    cap.stop();
    try std.testing.expectEqual(@as(u64, 3), ring.total_written.load(.acquire));
    var out: [6]f32 = undefined;
    try ring.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0.1, 0.2, 0.3, 0.4, 0.5, 0.6 }, &out);
    try std.testing.expectEqual(@as(u8, 0), cap.stats().running);
}

test "stop is idempotent and joins; running flips false" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{});
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .input, .device_id = "dev", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct { fn f(c: *Capture) bool { return c.running.load(.acquire); } }.f);
    cap.stop();
    cap.stop();
    try std.testing.expectEqual(@as(u8, 0), cap.stats().running);
    try std.testing.expect(fake.stopped.load(.acquire));
}

test "open failure lands in lastError and running stays false" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{});
    fake.open_error = error.DeviceNotFound;
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .input, .device_id = "gone", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct { fn f(c: *Capture) bool { return c.err_len.load(.acquire) > 0; } }.f);
    cap.stop();
    try std.testing.expectEqualStrings("open failed: DeviceNotFound", cap.lastError());
    try std.testing.expectEqual(@as(u8, 0), cap.stats().running);
}

test "discontinuity flag increments xruns; mix_rate is published" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{ &[_]f32{ 0, 0 }, &[_]f32{ 0, 0 }, &[_]f32{ 0, 0 } });
    fake.discontinuity_at = 1;
    fake.mix_rate = 44_100;
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct { fn f(c: *Capture) bool { return c.frames_written.load(.acquire) == 3; } }.f);
    cap.stop();
    const s = cap.stats();
    try std.testing.expectEqual(@as(u32, 1), s.xruns);
    try std.testing.expectEqual(@as(u32, 44_100), s.mix_rate);
}

test "spec's device_id and pid reach the backend" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{});
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .process, .device_id = "{abc}", .pid = 4242, .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct { fn f(c: *Capture) bool { return c.running.load(.acquire); } }.f);
    cap.stop();
    const spec = fake.last_spec orelse return error.Expected;
    try std.testing.expectEqualStrings("{abc}", spec.device_id);
    try std.testing.expectEqual(@as(u32, 4242), spec.pid);
    try std.testing.expectEqual(Backend.Kind.process, spec.kind);
}

test "start twice is AlreadyRunning" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{});
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .input, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    defer cap.stop();
    try std.testing.expectError(error.AlreadyRunning, cap.start());
}
```

Add to `core/src/root.zig`: `pub const Capture = @import("Capture.zig");`

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test`
Expected: compile error (`init`/`start`/`stop`/`stats`/`lastError` missing).

- [ ] **Step 3: Implement**

```zig
pub fn init(ring: *Ring, backend: Backend.Backend, spec: Backend.Spec) Capture {
    var self = Capture{
        .ring = ring,
        .backend = backend,
        .kind = spec.kind,
        .pid = spec.pid,
        .rate = spec.rate,
        .channels = spec.channels,
        .id_buf = undefined,
        .id_len = 0,
    };
    // Own the id bytes: the caller's slice (a Python str via ctypes) is
    // gone by the time the thread reads it. Fixed buffer, no allocator.
    const n = @min(spec.device_id.len, max_device_id - 1);
    @memcpy(self.id_buf[0..n], spec.device_id[0..n]);
    self.id_buf[n] = 0;
    self.id_len = n;
    return self;
}

/// Named to avoid shadowing init's `spec` parameter — Zig rejects a local
/// that shadows a declaration.
fn currentSpec(self: *const Capture) Backend.Spec {
    return .{ .kind = self.kind, .device_id = self.id_buf[0..self.id_len], .pid = self.pid, .rate = self.rate, .channels = self.channels };
}

pub fn start(self: *Capture) !void {
    if (self.thread != null) return error.AlreadyRunning;
    self.stop_flag.store(false, .monotonic);
    self.err_len.store(0, .monotonic);
    // std.Thread.spawn takes the function and a tuple of its arguments.
    self.thread = try std.Thread.spawn(.{}, run, .{self});
}

pub fn stop(self: *Capture) void {
    const t = self.thread orelse return;
    self.stop_flag.store(true, .release);
    t.join();
    self.thread = null;
}

pub fn stats(self: *const Capture) Stats {
    return .{
        .running = @intFromBool(self.running.load(.acquire)),
        .frames_written = self.frames_written.load(.acquire),
        .xruns = self.xruns.load(.acquire),
        .mix_rate = self.mix_rate.load(.acquire),
    };
}

pub fn lastError(self: *const Capture) [:0]const u8 {
    const n = self.err_len.load(.acquire);
    return self.err_buf[0..n :0];
}

fn setError(self: *Capture, comptime fmt: []const u8, args: anytype) void {
    // bufPrintZ into a fixed buffer: no allocation on the audio thread.
    const s = std.fmt.bufPrintZ(self.err_buf[0..], fmt, args) catch self.err_buf[0 .. max_error - 1 :0];
    self.err_len.store(s.len, .release);
}

fn run(self: *Capture) void {
    const stream = self.backend.open(self.currentSpec()) catch |e| {
        self.setError("open failed: {s}", .{@errorName(e)});
        return;
    };
    self.mix_rate.store(stream.mixRate(), .release);
    self.running.store(true, .release);
    defer self.running.store(false, .release);
    defer stream.deinit();
    defer stream.stop();
    while (!self.stop_flag.load(.acquire)) {
        const maybe = stream.next(100) catch |e| {
            self.setError("stream failed: {s}", .{@errorName(e)});
            return;
        };
        const pkt = maybe orelse continue;
        if (pkt.discontinuity) _ = self.xruns.fetchAdd(1, .monotonic);
        self.ring.write(pkt.frames);
        _ = self.frames_written.fetchAdd(pkt.frames.len / self.ring.channels, .release);
    }
}
```

Note the `defer` order: declared `deinit` then `stop`, so `stop` runs first (LIFO), then `deinit`, then `running=false` — the reader of `running` sees false only after the stream is torn down.

- [ ] **Step 4: Run, verify green, count +6**

Run: `zig build --build-file core/build.zig test --summary all` (three times — these tests spawn threads).
Expected: pass ×3; count rose by 6. Mutation: comment out `_ = self.xruns.fetchAdd(...)` → the discontinuity test reddens; revert.

- [ ] **Step 5: Commit**

```bash
git add core/src/Capture.zig core/src/root.zig
git commit -m "feat(core): Capture.zig — Zig-owned capture thread over Backend, tested on FakeBackend"
```

---

### Task 4: `Ring.init` validation (#21) and WAV `data_len` guard (#28)

**Files:**
- Modify: `core/src/Ring.zig:60`, `core/src/wav.zig` (writeFile), `core/src/abi.zig` (`fb_ring_create` guard becomes pass-through; `fb_wav_write` maps the new error)

**Interfaces:**
- Produces: `Ring.init` returns `error.InvalidArgument` for `sample_rate == 0`, `channels == 0`, `channels > 2`, `!isFinite(seconds)`, `seconds <= 0`. `wav.writeFile` returns `error.TooLong` when `n_frames * channels * bytes_per_sample > maxInt(u32) - header_len`. `FbStatus` gains nothing (`invalid_arg` / `io_error` cover both).

- [ ] **Step 1: Failing tests**

Append to `core/src/Ring.zig`:

```zig
test "init rejects sample_rate == 0" {
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 0, .channels = 2, .seconds = 1.0 }));
}
test "init rejects channels == 0" {
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 0, .seconds = 1.0 }));
}
test "init rejects channels == 3" {
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 3, .seconds = 1.0 }));
}
test "init rejects seconds <= 0, NaN, and +inf" {
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 0.0 }));
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = std.math.nan(f64) }));
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = std.math.inf(f64) }));
}
```

Append to `core/src/wav.zig` (next to the existing writeFile tests; use the same tmpDir pattern they use):

```zig
test "writeFile rejects a data chunk that would overflow the u32 RIFF sizes without touching disk" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&path_buf, ".zig-cache/tmp/{s}/never.wav", .{tmp.sub_path}) catch unreachable;
    // A slice header with an impossible length: we never read it — the
    // guard must fire on the arithmetic alone.
    const huge: []const f32 = @as([*]const f32, @ptrFromInt(0x1000))[0 .. (std.math.maxInt(u32) / 4) + 1];
    try std.testing.expectError(error.TooLong, writeFile(path, huge, 48_000, 1, .float32));
}
```

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test`
Expected: the four Ring tests fail (init currently succeeds or panics with divide-by-zero — a panic in a test is red too), the wav test fails (no `error.TooLong`).

- [ ] **Step 3: Implement**

At the top of `Ring.init` (before the capacity computation):

```zig
    // The ABI used to be the only guard (issue #21). Hosts that construct
    // a Ring directly (Capture, a future CLAP host) now get the same
    // protection, and fb_ring_create's guard becomes a pass-through.
    if (config.sample_rate == 0 or config.channels == 0 or config.channels > 2 or
        !std.math.isFinite(config.seconds) or config.seconds <= 0) return error.InvalidArgument;
```

In `wav.writeFile` (`core/src/wav.zig:170`, params `path, samples, rate, channels, st`), before `createFile`:

```zig
    const data_len_wide: u64 = @as(u64, samples.len) * st.bytesPerSample();
    if (data_len_wide > std.math.maxInt(u32) - header_len) return error.TooLong;
```

In `abi.zig`, replace `fb_ring_create`'s five-clause `if` with a comment pointing at `Ring.init` and let `Ring.init` failure return null (the existing `catch` already does). Keep the existing ABI tests — they still pass, now through the inner guard. In `fb_wav_write`, extend the catch: `catch |e| return switch (e) { error.TooLong => .invalid_arg, else => .io_error };`

- [ ] **Step 4: Run, verify green, count +5**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: pass; count +5. Mutation: delete the `channels > 2` clause → "rejects channels == 3" reddens; revert.

- [ ] **Step 5: Commit**

```bash
git add core/src/Ring.zig core/src/wav.zig core/src/abi.zig
git commit -m "fix(core): Ring.init validates its config; wav.writeFile guards the u32 data length (#21, #28)"
```

---

### Task 5: `wasapi.zig` + `WasapiBackend.zig` — the Windows backend

**Files:**
- Create: `core/src/wasapi.zig`, `core/src/WasapiBackend.zig`
- Modify: `core/src/root.zig` (OS-gated import), `core/build.zig` (link `ole32` on Windows)

**Interfaces:**
- Consumes: `Backend.*`, `convert.packet`.
- Produces: `WasapiBackend.backend() Backend.Backend` (a stateless singleton: `pub var instance: WasapiBackend = .{};`), `wasapi.guid(comptime str) GUID`, `wasapi.wtf16ToUtf8Z(dst: []u8, src: [*:0]const u16) []u8`. Everything else is private to the two files.
- Hardware-only behaviour; Zig unit tests cover the pure helpers (GUID parser, wide-string copy, format-candidate table) and that the file compiles for `x86_64-windows`.

**Vtable reference:** `flashback_sampler/io/win32_process_loopback.py:181-425` declares `IAudioClient`, `IAudioCaptureClient`, and the activation interfaces in ctypes — for those, the method order there is the SDK's order; cross-check against it. `IMMDeviceEnumerator`, `IMMDeviceCollection`, `IMMDevice`, and `IPropertyStore` exist nowhere in this repo: for those four, the Step 3 snippet below IS the reference (its method order was taken from the SDK headers). Transcribe the snippet exactly. Do not reorder any vtable.

- [ ] **Step 1: Failing tests (pure helpers)**

Create `core/src/wasapi.zig` starting with:

```zig
//! Hand-written WASAPI/COM declarations. Zero external deps: no zigwin32,
//! no translate-c. A COM interface is a pointer to a struct whose first
//! field is a pointer to a vtable of function pointers; `extern struct`
//! gives C layout, `callconv(.winapi)` gives the stdcall/x64 convention
//! COM uses. Method order in each VTable is load-bearing — it IS the
//! binary interface. Reference: io/win32_process_loopback.py:181-425.
const std = @import("std");
const builtin = @import("builtin");

pub const HRESULT = i32;
pub const HANDLE = *anyopaque;
pub inline fn failed(hr: HRESULT) bool {
    return hr < 0;
}

pub const GUID = extern struct { d1: u32, d2: u16, d3: u16, d4: [8]u8 };

/// comptime "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}" → GUID.
pub fn guid(comptime s: []const u8) GUID {
    comptime {
        std.debug.assert(s.len == 38 and s[0] == '{' and s[37] == '}');
        const hex = std.fmt.parseInt;
        var d4: [8]u8 = undefined;
        d4[0] = hex(u8, s[20..22], 16) catch unreachable;
        d4[1] = hex(u8, s[22..24], 16) catch unreachable;
        for (0..6) |i| d4[2 + i] = hex(u8, s[25 + 2 * i .. 27 + 2 * i], 16) catch unreachable;
        return .{
            .d1 = hex(u32, s[1..9], 16) catch unreachable,
            .d2 = hex(u16, s[10..14], 16) catch unreachable,
            .d3 = hex(u16, s[15..19], 16) catch unreachable,
            .d4 = d4,
        };
    }
}

/// NUL-terminated WTF-16 → UTF-8 into dst, truncated to fit, always NUL-terminated. Returns the bytes written (excluding NUL).
pub fn wtf16ToUtf8Z(dst: []u8, src: [*:0]const u16) []u8 {
    const wide = std.mem.span(src);
    var n: usize = 0;
    var it = std.unicode.Wtf16LeIterator.init(wide);
    while (it.nextCodepoint()) |cp| {
        var tmp: [4]u8 = undefined;
        const len = std.unicode.wtf8Encode(cp, &tmp) catch continue;
        if (n + len >= dst.len) break;
        @memcpy(dst[n .. n + len], tmp[0..len]);
        n += len;
    }
    dst[n] = 0;
    return dst[0..n];
}

test "guid parses IID_IUnknown byte-exact" {
    const g = guid("{00000000-0000-0000-C000-000000000046}");
    try std.testing.expectEqual(@as(u32, 0), g.d1);
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0xC0, 0, 0, 0, 0, 0, 0, 0x46 }, &g.d4);
}

test "guid parses IID_IAudioClient" {
    const g = guid("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}");
    try std.testing.expectEqual(@as(u32, 0x1CB9AD4C), g.d1);
    try std.testing.expectEqual(@as(u16, 0xDBFA), g.d2);
    try std.testing.expectEqual(@as(u16, 0x4C32), g.d3);
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0xB1, 0x78, 0xC2, 0xF5, 0x68, 0xA7, 0x03, 0xB2 }, &g.d4);
}

test "wtf16ToUtf8Z copies, truncates, and terminates" {
    const wide = [_:0]u16{ 'S', 'p', 'k', 0xE9 }; // "Spké"
    var dst: [8]u8 = undefined;
    const got = wtf16ToUtf8Z(&dst, &wide);
    try std.testing.expectEqualStrings("Spk\xC3\xA9", got);
    try std.testing.expectEqual(@as(u8, 0), dst[got.len]);
    var small: [3]u8 = undefined;
    const t = wtf16ToUtf8Z(&small, &wide);
    try std.testing.expectEqualStrings("Sp", t);
}
```

Add to `core/src/root.zig`:

```zig
// OS-gated: these two files only compile for Windows targets. On other
// targets `wasapi`/`WasapiBackend` are empty structs and abi.zig's
// capture exports return null/0. builtin.os.tag is a comptime constant, so the
// dead branch is never analyzed on macOS/Linux — that is what keeps the
// cross-compile legs green.
const builtin = @import("builtin");
pub const wasapi = if (builtin.os.tag == .windows) @import("wasapi.zig") else struct {};
pub const WasapiBackend = if (builtin.os.tag == .windows) @import("WasapiBackend.zig") else struct {};
```

- [ ] **Step 2: Run, verify red then green for helpers**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: compile error until the helper bodies exist (they are above — so this step's "red" is a deliberate mutation: temporarily return `.{ .d1 = 0, .d2 = 0, .d3 = 0, .d4 = .{0} ** 8 }` from `guid`, see the two guid tests redden, revert). Then green, count +3.

- [ ] **Step 3: Declare the COM surface**

Append to `core/src/wasapi.zig`:

```zig
// ── ole32 / kernel32 externs ─────────────────────────────────────────
// `extern "ole32"` names the import library; build.zig links it. kernel32
// is always linked on Windows.
pub extern "ole32" fn CoInitializeEx(reserved: ?*anyopaque, coinit: u32) callconv(.winapi) HRESULT;
pub extern "ole32" fn CoUninitialize() callconv(.winapi) void;
pub extern "ole32" fn CoCreateInstance(clsid: *const GUID, outer: ?*anyopaque, ctx: u32, iid: *const GUID, out: *?*anyopaque) callconv(.winapi) HRESULT;
pub extern "ole32" fn CoTaskMemFree(p: ?*anyopaque) callconv(.winapi) void;
pub extern "ole32" fn PropVariantClear(p: *PROPVARIANT) callconv(.winapi) HRESULT;
pub extern "kernel32" fn Sleep(ms: u32) callconv(.winapi) void;
pub extern "kernel32" fn CloseHandle(h: HANDLE) callconv(.winapi) i32;
// WinRT apartment init. The capture threads use this, NOT CoInitializeEx:
// the port proved ActivateAudioInterfaceAsync (Task 9) returns
// E_ILLEGAL_METHOD_CALL (0x8000000E) under COM-only init, and RoInitialize
// is a superset of CoInitializeEx(MTA) — CoUninitialize is the paired
// teardown for both (win32_process_loopback.py:816-824). One init path
// for every capture kind. NOTE: RoInitialize takes ONE argument.
pub extern "combase" fn RoInitialize(init_type: u32) callconv(.winapi) HRESULT;
pub const RO_INIT_MULTITHREADED: u32 = 1;

pub const COINIT_MULTITHREADED: u32 = 0;
pub const CLSCTX_ALL: u32 = 0x17;
pub const eRender: u32 = 0;
pub const eCapture: u32 = 1;
pub const eConsole: u32 = 0;
pub const DEVICE_STATE_ACTIVE: u32 = 1;
pub const STGM_READ: u32 = 0;
pub const AUDCLNT_SHAREMODE_SHARED: u32 = 0;
pub const AUDCLNT_STREAMFLAGS_LOOPBACK: u32 = 0x00020000;
pub const AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM: u32 = 0x80000000;
pub const AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY: u32 = 0x08000000;
pub const AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY: u32 = 0x1;
pub const AUDCLNT_BUFFERFLAGS_SILENT: u32 = 0x2;
pub const WAVE_FORMAT_PCM: u16 = 1;
pub const WAVE_FORMAT_IEEE_FLOAT: u16 = 3;
pub const VT_LPWSTR: u16 = 31;
pub const REFTIME_MS: i64 = 10_000; // REFERENCE_TIME is 100 ns units

pub const CLSID_MMDeviceEnumerator = guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}");
pub const IID_IMMDeviceEnumerator = guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}");
pub const IID_IAudioClient = guid("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}");
pub const IID_IAudioCaptureClient = guid("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}");
pub const IID_IUnknown = guid("{00000000-0000-0000-C000-000000000046}");

pub const PROPERTYKEY = extern struct { fmtid: GUID, pid: u32 };
pub const PKEY_Device_FriendlyName = PROPERTYKEY{ .fmtid = guid("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), .pid = 14 };

pub const PROPVARIANT = extern struct {
    vt: u16,
    r1: u16 = 0,
    r2: u16 = 0,
    r3: u16 = 0,
    // The union: we only ever read pwszVal (VT_LPWSTR) or write blob (VT_BLOB, Task 9). 16 bytes on x64.
    data: extern union { pwszVal: ?[*:0]u16, blob: extern struct { cbSize: u32, pBlobData: ?*anyopaque }, pad: [16]u8 },
};

pub const WAVEFORMATEX = extern struct {
    wFormatTag: u16,
    nChannels: u16,
    nSamplesPerSec: u32,
    nAvgBytesPerSec: u32,
    nBlockAlign: u16,
    wBitsPerSample: u16,
    cbSize: u16,
};

/// Build a plain (non-EXTENSIBLE) format. Shared-mode WASAPI accepts this
/// for ≤ 2 channels — the same shape the Python port negotiates with.
pub fn waveFormat(tag: u16, bits: u16, rate: u32, channels: u16) WAVEFORMATEX {
    const block_align: u16 = channels * bits / 8;
    return .{ .wFormatTag = tag, .nChannels = channels, .nSamplesPerSec = rate, .nAvgBytesPerSec = rate * block_align, .nBlockAlign = block_align, .wBitsPerSample = bits, .cbSize = 0 };
}

// ── COM interfaces ───────────────────────────────────────────────────
// Every interface: `vtbl: *const VTable` first, then helper methods that
// forward. `?*anyopaque` out-params mirror `void**`.

pub const IUnknownVTable = extern struct {
    QueryInterface: *const fn (*anyopaque, *const GUID, *?*anyopaque) callconv(.winapi) HRESULT,
    AddRef: *const fn (*anyopaque) callconv(.winapi) u32,
    Release: *const fn (*anyopaque) callconv(.winapi) u32,
};

pub const IMMDeviceEnumerator = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        EnumAudioEndpoints: *const fn (*IMMDeviceEnumerator, data_flow: u32, state_mask: u32, out: *?*IMMDeviceCollection) callconv(.winapi) HRESULT,
        GetDefaultAudioEndpoint: *const fn (*IMMDeviceEnumerator, data_flow: u32, role: u32, out: *?*IMMDevice) callconv(.winapi) HRESULT,
        GetDevice: *const fn (*IMMDeviceEnumerator, id: [*:0]const u16, out: *?*IMMDevice) callconv(.winapi) HRESULT,
        RegisterEndpointNotificationCallback: *const anyopaque,
        UnregisterEndpointNotificationCallback: *const anyopaque,
    };
    pub fn release(self: *IMMDeviceEnumerator) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IMMDeviceCollection = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        GetCount: *const fn (*IMMDeviceCollection, *u32) callconv(.winapi) HRESULT,
        Item: *const fn (*IMMDeviceCollection, u32, *?*IMMDevice) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IMMDeviceCollection) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IMMDevice = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        Activate: *const fn (*IMMDevice, iid: *const GUID, clsctx: u32, params: ?*PROPVARIANT, out: *?*anyopaque) callconv(.winapi) HRESULT,
        OpenPropertyStore: *const fn (*IMMDevice, stgm: u32, out: *?*IPropertyStore) callconv(.winapi) HRESULT,
        GetId: *const fn (*IMMDevice, out: *?[*:0]u16) callconv(.winapi) HRESULT,
        GetState: *const fn (*IMMDevice, *u32) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IMMDevice) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IPropertyStore = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        GetCount: *const anyopaque,
        GetAt: *const anyopaque,
        GetValue: *const fn (*IPropertyStore, key: *const PROPERTYKEY, out: *PROPVARIANT) callconv(.winapi) HRESULT,
        SetValue: *const anyopaque,
        Commit: *const anyopaque,
    };
    pub fn release(self: *IPropertyStore) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IAudioClient = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        Initialize: *const fn (*IAudioClient, share_mode: u32, flags: u32, buffer_duration: i64, periodicity: i64, fmt: *const WAVEFORMATEX, session: ?*const GUID) callconv(.winapi) HRESULT,
        GetBufferSize: *const fn (*IAudioClient, *u32) callconv(.winapi) HRESULT,
        GetStreamLatency: *const fn (*IAudioClient, *i64) callconv(.winapi) HRESULT,
        GetCurrentPadding: *const fn (*IAudioClient, *u32) callconv(.winapi) HRESULT,
        IsFormatSupported: *const fn (*IAudioClient, u32, *const WAVEFORMATEX, *?*WAVEFORMATEX) callconv(.winapi) HRESULT,
        GetMixFormat: *const fn (*IAudioClient, *?*WAVEFORMATEX) callconv(.winapi) HRESULT,
        GetDevicePeriod: *const fn (*IAudioClient, *i64, *i64) callconv(.winapi) HRESULT,
        Start: *const fn (*IAudioClient) callconv(.winapi) HRESULT,
        Stop: *const fn (*IAudioClient) callconv(.winapi) HRESULT,
        Reset: *const fn (*IAudioClient) callconv(.winapi) HRESULT,
        SetEventHandle: *const fn (*IAudioClient, HANDLE) callconv(.winapi) HRESULT,
        GetService: *const fn (*IAudioClient, *const GUID, *?*anyopaque) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IAudioClient) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IAudioCaptureClient = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        GetBuffer: *const fn (*IAudioCaptureClient, data: *?[*]u8, n_frames: *u32, flags: *u32, dev_pos: ?*u64, qpc_pos: ?*u64) callconv(.winapi) HRESULT,
        ReleaseBuffer: *const fn (*IAudioCaptureClient, u32) callconv(.winapi) HRESULT,
        GetNextPacketSize: *const fn (*IAudioCaptureClient, *u32) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IAudioCaptureClient) void {
        _ = self.vtbl.base.Release(self);
    }
};
```

In `core/build.zig`, after `const mod = b.addModule(...)`:

```zig
    // The WASAPI backend calls ole32 (CoCreateInstance & co) and combase
    // (RoInitialize). Zig ships import libraries for the Windows system
    // DLLs, so this links without an SDK. Only meaningful for Windows
    // targets; harmless elsewhere because wasapi.zig is not even analyzed
    // there (see root.zig).
    if (target.result.os.tag == .windows) {
        mod.linkSystemLibrary("ole32", .{});
        mod.linkSystemLibrary("combase", .{});
    }
```

Run: `zig build --build-file core/build.zig test` and `zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu`. Expected: both green (declarations compile; Linux target never sees them). If `linkSystemLibrary` on a `*std.Build.Module` has a different shape in 0.16, use `lib.linkSystemLibrary(...)` on the artifact instead. If Zig's bundled import libs turn out to lack `combase`, drop the extern and resolve `RoInitialize` at runtime with the same `LoadLibraryW`/`GetProcAddress` pattern Task 9 uses for `ActivateAudioInterfaceAsync` — record which path shipped.

- [ ] **Step 4: Implement `WasapiBackend.zig`**

```zig
//! Backend over WASAPI shared-mode capture. One code path for loopback
//! (an eRender endpoint opened with the LOOPBACK flag), mic/line-in (an
//! eCapture endpoint) and — Task 9 — per-process loopback. Polling, not
//! event-driven: event-driven loopback has a known WASAPI quirk (events
//! stop unless a render stream is also active), and a 10 ms poll is
//! nothing next to a 200 ms WASAPI buffer. One loop for every kind.
const std = @import("std");
const w = @import("wasapi.zig");
const Backend = @import("Backend.zig");
const convert = @import("convert.zig");
const WasapiBackend = @This();

pub var instance: WasapiBackend = .{};

pub fn backend() Backend.Backend {
    return .{ .ptr = &instance, .vtable = &backend_vtable };
}

const backend_vtable = Backend.Backend.VTable{ .enumerate = enumerate, .open = open };
const stream_vtable = Backend.Stream.VTable{ .next = next, .stop = stop, .deinit = deinit, .mixRate = mixRate };

const poll_ms: u32 = 10;
const buffer_duration_ms: i64 = 200;
const max_streams = 16;

/// Format candidates in preference order. First = "what we asked for";
/// AUTOCONVERTPCM makes the engine convert to it. The rest are the
/// Python port's fallbacks for the process-loopback client, which has
/// no device attached so GetMixFormat is not meaningful there — the
/// port never queries it and tries this chain instead
/// (win32_process_loopback.py:955-975).
pub fn candidates(rate: u32, channels: u16) [5]w.WAVEFORMATEX {
    return .{
        w.waveFormat(w.WAVE_FORMAT_IEEE_FLOAT, 32, rate, channels),
        w.waveFormat(w.WAVE_FORMAT_IEEE_FLOAT, 32, 48_000, 2),
        w.waveFormat(w.WAVE_FORMAT_IEEE_FLOAT, 32, 44_100, 2),
        w.waveFormat(w.WAVE_FORMAT_PCM, 16, 44_100, 2),
        w.waveFormat(w.WAVE_FORMAT_PCM, 16, 48_000, 2),
    };
}

/// One open stream. Fixed pool, no allocator: the RT rule reaches the
/// backend too. `scratch` holds one converted packet — sized from
/// GetBufferSize (the largest packet WASAPI can hand us) × dst channels.
const Stream = struct {
    in_use: bool = false,
    client: ?*w.IAudioClient = null,
    capture: ?*w.IAudioCaptureClient = null,
    src_fmt: convert.SourceFormat = .{ .tag = .f32, .channels = 2 },
    dst_channels: u16 = 2,
    mix_rate: u32 = 0,
    stopped: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
    scratch: [scratch_len]f32 = undefined,
    // 200 ms at 192 kHz stereo = 76 800 floats; round up. If GetBufferSize
    // reports more than this, open() fails with Unsupported rather than
    // ever overrunning.
    const scratch_len = 96 * 1024;
};

streams: [max_streams]Stream = [_]Stream{.{}} ** max_streams,

fn enumerate(ptr: *anyopaque, out: []Backend.Device) usize {
    _ = ptr;
    // Called on the host's thread (Qt's main thread is STA): CoInitializeEx
    // then returns RPC_E_CHANGED_MODE, COM stays usable, and we must NOT
    // pair it with CoUninitialize. Only balance a successful init.
    const hr_init = w.CoInitializeEx(null, w.COINIT_MULTITHREADED);
    defer if (!w.failed(hr_init)) w.CoUninitialize();
    var enumr: ?*anyopaque = null;
    if (w.failed(w.CoCreateInstance(&w.CLSID_MMDeviceEnumerator, null, w.CLSCTX_ALL, &w.IID_IMMDeviceEnumerator, &enumr))) return 0;
    const en: *w.IMMDeviceEnumerator = @ptrCast(@alignCast(enumr.?));
    defer en.release();
    var n: usize = 0;
    // Loopback devices are the RENDER endpoints; inputs are the CAPTURE endpoints.
    n += listFlow(en, w.eRender, .loopback, out[n..]);
    n += listFlow(en, w.eCapture, .input, out[n..]);
    return n;
}

fn listFlow(en: *w.IMMDeviceEnumerator, flow: u32, kind: Backend.Kind, out: []Backend.Device) usize {
    var coll: ?*w.IMMDeviceCollection = null;
    if (w.failed(en.vtbl.EnumAudioEndpoints(en, flow, w.DEVICE_STATE_ACTIVE, &coll))) return 0;
    defer coll.?.release();
    var default_id: [128]u8 = undefined;
    var default_len: usize = 0;
    var def: ?*w.IMMDevice = null;
    if (!w.failed(en.vtbl.GetDefaultAudioEndpoint(en, flow, w.eConsole, &def))) {
        defer def.?.release();
        default_len = deviceId(def.?, &default_id).len;
    }
    var count: u32 = 0;
    _ = coll.?.vtbl.GetCount(coll.?, &count);
    var n: usize = 0;
    var i: u32 = 0;
    while (i < count and n < out.len) : (i += 1) {
        var dev: ?*w.IMMDevice = null;
        if (w.failed(coll.?.vtbl.Item(coll.?, i, &dev))) continue;
        defer dev.?.release();
        var d = std.mem.zeroes(Backend.Device);
        d.kind = @intFromEnum(kind);
        const id = deviceId(dev.?, &d.id);
        d.is_default = @intFromBool(std.mem.eql(u8, id, default_id[0..default_len]));
        _ = friendlyName(dev.?, &d.name);
        mixFormat(dev.?, &d.mix_rate, &d.mix_channels);
        out[n] = d;
        n += 1;
    }
    return n;
}

fn deviceId(dev: *w.IMMDevice, dst: []u8) []u8 {
    var wide: ?[*:0]u16 = null;
    if (w.failed(dev.vtbl.GetId(dev, &wide))) {
        dst[0] = 0;
        return dst[0..0];
    }
    defer w.CoTaskMemFree(wide);
    return w.wtf16ToUtf8Z(dst, wide.?);
}

fn friendlyName(dev: *w.IMMDevice, dst: []u8) []u8 {
    var store: ?*w.IPropertyStore = null;
    if (w.failed(dev.vtbl.OpenPropertyStore(dev, w.STGM_READ, &store))) {
        dst[0] = 0;
        return dst[0..0];
    }
    defer store.?.release();
    var pv: w.PROPVARIANT = .{ .vt = 0, .data = .{ .pad = [_]u8{0} ** 16 } };
    if (w.failed(store.?.vtbl.GetValue(store.?, &w.PKEY_Device_FriendlyName, &pv)) or pv.vt != w.VT_LPWSTR or pv.data.pwszVal == null) {
        dst[0] = 0;
        return dst[0..0];
    }
    defer _ = w.PropVariantClear(&pv);
    return w.wtf16ToUtf8Z(dst, pv.data.pwszVal.?);
}

/// Mix format = what the engine runs this endpoint at. Rate 0 = unknown.
fn mixFormat(dev: *w.IMMDevice, rate: *u32, channels: *u16) void {
    var raw: ?*anyopaque = null;
    if (w.failed(dev.vtbl.Activate(dev, &w.IID_IAudioClient, w.CLSCTX_ALL, null, &raw))) return;
    const client: *w.IAudioClient = @ptrCast(@alignCast(raw.?));
    defer client.release();
    var fmt: ?*w.WAVEFORMATEX = null;
    if (w.failed(client.vtbl.GetMixFormat(client, &fmt))) return;
    defer w.CoTaskMemFree(fmt);
    rate.* = fmt.?.nSamplesPerSec;
    channels.* = fmt.?.nChannels;
}

fn open(ptr: *anyopaque, spec: Backend.Spec) Backend.Error!Backend.Stream {
    const self: *WasapiBackend = @ptrCast(@alignCast(ptr));
    if (spec.channels == 0 or spec.channels > 2) return error.Unsupported;
    // Called on the capture thread, which stays alive for the stream's
    // life — so the init here pairs with CoUninitialize in deinit.
    // RoInitialize, not CoInitializeEx: see the declaration in wasapi.zig
    // — process activation (Task 9) hard-requires the WinRT apartment,
    // and it is a superset of COM MTA for the other kinds.
    _ = w.RoInitialize(w.RO_INIT_MULTITHREADED);
    errdefer w.CoUninitialize();
    const slot = self.acquireSlot() orelse return error.OutOfMemory;
    errdefer slot.in_use = false;
    const client = try activate(spec);
    errdefer client.release();
    // Mix rate first: on a real endpoint this works; on the process
    // client (Task 9) it returns E_NOTIMPL and we report 0.
    var mix: ?*w.WAVEFORMATEX = null;
    if (!w.failed(client.vtbl.GetMixFormat(client, &mix))) {
        slot.mix_rate = mix.?.nSamplesPerSec;
        w.CoTaskMemFree(mix);
    } else slot.mix_rate = 0;
    var flags: u32 = w.AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | w.AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
    if (spec.kind != .input) flags |= w.AUDCLNT_STREAMFLAGS_LOOPBACK;
    const cands = candidates(spec.rate, spec.channels);
    var chosen: ?w.WAVEFORMATEX = null;
    for (cands) |c| {
        if (!w.failed(client.vtbl.Initialize(client, w.AUDCLNT_SHAREMODE_SHARED, flags, buffer_duration_ms * w.REFTIME_MS, 0, &c, null))) {
            chosen = c;
            break;
        }
    }
    const fmt = chosen orelse return error.FormatRejected;
    var buf_frames: u32 = 0;
    _ = client.vtbl.GetBufferSize(client, &buf_frames);
    if (@as(usize, buf_frames) * spec.channels > Stream.scratch_len) return error.Unsupported;
    var raw: ?*anyopaque = null;
    if (w.failed(client.vtbl.GetService(client, &w.IID_IAudioCaptureClient, &raw))) return error.ActivationFailed;
    const cap: *w.IAudioCaptureClient = @ptrCast(@alignCast(raw.?));
    errdefer cap.release();
    if (w.failed(client.vtbl.Start(client))) return error.ActivationFailed;
    slot.* = .{
        .in_use = true,
        .client = client,
        .capture = cap,
        .src_fmt = .{ .tag = if (fmt.wFormatTag == w.WAVE_FORMAT_PCM) .i16 else .f32, .channels = fmt.nChannels },
        .dst_channels = spec.channels,
        .mix_rate = slot.mix_rate,
        .scratch = undefined,
    };
    return .{ .ptr = slot, .vtable = &stream_vtable };
}

/// Task 5: default or named endpoint via IMMDeviceEnumerator. Task 9 adds
/// the `.process` arm (ActivateAudioInterfaceAsync).
fn activate(spec: Backend.Spec) Backend.Error!*w.IAudioClient {
    var enumr: ?*anyopaque = null;
    if (w.failed(w.CoCreateInstance(&w.CLSID_MMDeviceEnumerator, null, w.CLSCTX_ALL, &w.IID_IMMDeviceEnumerator, &enumr))) return error.ActivationFailed;
    const en: *w.IMMDeviceEnumerator = @ptrCast(@alignCast(enumr.?));
    defer en.release();
    const flow: u32 = if (spec.kind == .input) w.eCapture else w.eRender;
    var dev: ?*w.IMMDevice = null;
    if (spec.device_id.len == 0) {
        if (w.failed(en.vtbl.GetDefaultAudioEndpoint(en, flow, w.eConsole, &dev))) return error.DeviceNotFound;
    } else {
        // UTF-8 id → wide, NUL-terminated, on the stack.
        var wide: [256:0]u16 = undefined;
        const n = std.unicode.wtf8ToWtf16Le(&wide, spec.device_id) catch return error.DeviceNotFound;
        wide[n] = 0;
        if (w.failed(en.vtbl.GetDevice(en, &wide, &dev))) return error.DeviceNotFound;
    }
    defer dev.?.release();
    var raw: ?*anyopaque = null;
    if (w.failed(dev.?.vtbl.Activate(dev.?, &w.IID_IAudioClient, w.CLSCTX_ALL, null, &raw))) return error.ActivationFailed;
    return @ptrCast(@alignCast(raw.?));
}

fn acquireSlot(self: *WasapiBackend) ?*Stream {
    for (&self.streams) |*s| {
        if (!s.in_use) {
            s.in_use = true;
            return s;
        }
    }
    return null;
}

fn next(ptr: *anyopaque, timeout_ms: u32) Backend.Error!?Backend.Packet {
    const s: *Stream = @ptrCast(@alignCast(ptr));
    var waited: u32 = 0;
    while (!s.stopped.load(.acquire)) {
        var n_frames: u32 = 0;
        if (w.failed(s.capture.?.vtbl.GetNextPacketSize(s.capture.?, &n_frames))) return error.ActivationFailed;
        if (n_frames > 0) {
            var data: ?[*]u8 = null;
            var got: u32 = 0;
            var flags: u32 = 0;
            if (w.failed(s.capture.?.vtbl.GetBuffer(s.capture.?, &data, &got, &flags, null, null))) return error.ActivationFailed;
            const frames = convert.packet(data.?, got, s.src_fmt, flags & w.AUDCLNT_BUFFERFLAGS_SILENT != 0, s.dst_channels, &s.scratch);
            _ = s.capture.?.vtbl.ReleaseBuffer(s.capture.?, got);
            return .{ .frames = frames, .discontinuity = flags & w.AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY != 0 };
        }
        if (waited >= timeout_ms) return null;
        w.Sleep(poll_ms);
        waited += poll_ms;
    }
    return null;
}

fn stop(ptr: *anyopaque) void {
    const s: *Stream = @ptrCast(@alignCast(ptr));
    s.stopped.store(true, .release);
}

fn deinit(ptr: *anyopaque) void {
    const s: *Stream = @ptrCast(@alignCast(ptr));
    if (s.client) |c| _ = c.vtbl.Stop(c);
    if (s.capture) |c| c.release();
    if (s.client) |c| c.release();
    s.* = .{};
    w.CoUninitialize();
}

fn mixRate(ptr: *anyopaque) u32 {
    const s: *Stream = @ptrCast(@alignCast(ptr));
    return s.mix_rate;
}

test "candidates: first entry is the requested format, all five are well-formed" {
    const c = candidates(96_000, 1);
    try std.testing.expectEqual(@as(u32, 96_000), c[0].nSamplesPerSec);
    try std.testing.expectEqual(@as(u16, 1), c[0].nChannels);
    for (c) |f| {
        try std.testing.expectEqual(f.nChannels * f.wBitsPerSample / 8, f.nBlockAlign);
        try std.testing.expectEqual(f.nSamplesPerSec * f.nBlockAlign, f.nAvgBytesPerSec);
    }
}
```

`convert.packet` takes `[*]const u8`; `data` is `?[*]u8` — coercion is implicit. `Stream.scratch` is ~384 KB per slot × 16 slots ≈ 6 MB static; acceptable (it is `undefined` until used and lives in BSS).

- [ ] **Step 5: Verify build on both leg shapes**

Run: `zig build --build-file core/build.zig test --summary all` (Windows host: count +4 total for this task — 3 helpers + 1 candidates)
Run: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe`
Run: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu`
Run: `zig fmt --check core/build.zig core/src`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add core/src/wasapi.zig core/src/WasapiBackend.zig core/src/root.zig core/build.zig
git commit -m "feat(core): wasapi.zig COM bindings + WasapiBackend (enumerate, loopback + input capture)"
```

---

### Task 6: ABI + header + `native.py` + `native_capture.py`

**Files:**
- Modify: `core/src/abi.zig`, `core/include/flashback_core.h`, `flashback_sampler/core/native.py`
- Create: `flashback_sampler/core/native_capture.py`, `tests/unit/test_native_capture.py`

**Interfaces:**
- Consumes: `Capture`, `Backend.Device`, `WasapiBackend.backend()`.
- Produces (C ABI, mirrored exactly in `native.py._declare`):
  ```c
  typedef struct FbCapture FbCapture;
  typedef struct FbDevice { uint8_t kind; uint8_t is_default; uint32_t mix_rate; uint16_t mix_channels; char id[128]; char name[128]; } FbDevice;
  typedef struct FbCaptureSpec { uint8_t kind; uint32_t pid; uint32_t rate; uint16_t channels; const char *device_id; } FbCaptureSpec;
  typedef struct FbCaptureStats { uint8_t running; uint64_t frames_written; uint32_t xruns; uint32_t mix_rate; } FbCaptureStats;
  size_t     fb_devices_list(FbDevice *out, size_t max);        /* 0 on non-Windows */
  FbCapture *fb_capture_create(FbRing *, const FbCaptureSpec *);/* NULL on non-Windows or bad spec */
  FbStatus   fb_capture_start(FbCapture *);                      /* FB_INVALID_ARG if already running, FB_IO_ERROR if spawn failed */
  void       fb_capture_stop(FbCapture *);
  void       fb_capture_destroy(FbCapture *);                    /* stops first */
  void       fb_capture_stats(const FbCapture *, FbCaptureStats *out);
  const char*fb_capture_last_error(const FbCapture *);           /* "" when none; valid until destroy */
  ```
  Python:
  ```python
  # native.py
  def list_devices() -> list[dict]   # keys: kind ("loopback"|"input"|"process"), is_default, mix_rate, mix_channels, id, name
  # native_capture.py
  class NativeCaptureSource:  # satisfies CaptureSource
      def __init__(self, buffer, kind: str, device_id: str = "", pid: int = 0, sample_rate: int = 48_000, channels: int = 2)
      sample_rate: int; channels: int
      def start(self) -> None; def stop(self) -> None; def is_running(self) -> bool
      def xrun_count(self) -> int; def last_error(self) -> str | None
      def frames_written(self) -> int; def mix_rate(self) -> int
      def close(self) -> None   # destroys the handle; idempotent
  ```
  `buffer` must expose `_h` (a `NativeAudioCircularBuffer`); anything else raises `TypeError` at construction. `KIND_INTS = {"loopback": 0, "input": 1, "process": 2}` lives in `native.py`.

- [ ] **Step 1: Failing Zig ABI test**

Append to `core/src/abi.zig` (imports: add `const Capture = @import("Capture.zig"); const Backend = @import("Backend.zig"); const builtin = @import("builtin");`):

```zig
pub const FbCaptureSpec = extern struct { kind: u8, pid: u32, rate: u32, channels: u16, device_id: [*:0]const u8 };

test "fb_capture_create rejects an unknown kind and a bad channel count" {
    const ring = fb_ring_create(48_000, 2, 1.0) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    try std.testing.expectEqual(@as(?*Capture, null), fb_capture_create(ring, &.{ .kind = 9, .pid = 0, .rate = 48_000, .channels = 2, .device_id = "" }));
    try std.testing.expectEqual(@as(?*Capture, null), fb_capture_create(ring, &.{ .kind = 0, .pid = 0, .rate = 48_000, .channels = 3, .device_id = "" }));
}

test "fb_capture stats/last_error on a never-started capture are zero/empty (Windows only)" {
    if (builtin.os.tag != .windows) return error.SkipZigTest;
    const ring = fb_ring_create(48_000, 2, 1.0) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    const cap = fb_capture_create(ring, &.{ .kind = 0, .pid = 0, .rate = 48_000, .channels = 2, .device_id = "" }) orelse return error.CreateFailed;
    defer fb_capture_destroy(cap);
    var st: Capture.Stats = undefined;
    fb_capture_stats(cap, &st);
    try std.testing.expectEqual(@as(u8, 0), st.running);
    try std.testing.expectEqual(@as(u64, 0), st.frames_written);
    try std.testing.expectEqualStrings("", std.mem.span(fb_capture_last_error(cap)));
}

test "fb_devices_list with max 0 writes nothing and returns 0" {
    try std.testing.expectEqual(@as(usize, 0), fb_devices_list(undefined, 0));
}
```

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test`
Expected: compile error (exports missing).

- [ ] **Step 3: Implement the exports**

```zig
// The one backend this build ships. On non-Windows there is none yet:
// capture creation returns null and enumeration returns 0, and the
// Python side reports "capture unavailable on this OS".
fn nativeBackend() ?Backend.Backend {
    if (builtin.os.tag == .windows) return @import("WasapiBackend.zig").backend();
    return null;
}

export fn fb_devices_list(out: [*]Backend.Device, max: usize) usize {
    if (max == 0) return 0;
    const be = nativeBackend() orelse return 0;
    return be.enumerate(out[0..max]);
}

export fn fb_capture_create(ring: *Ring, spec: *const FbCaptureSpec) ?*Capture {
    if (spec.kind > 2 or spec.channels == 0 or spec.channels > 2 or spec.rate == 0) return null;
    const be = nativeBackend() orelse return null;
    const cap = allocator.create(Capture) catch return null;
    cap.* = Capture.init(ring, be, .{
        .kind = @enumFromInt(spec.kind),
        .device_id = std.mem.span(spec.device_id),
        .pid = spec.pid,
        .rate = spec.rate,
        .channels = spec.channels,
    });
    return cap;
}

export fn fb_capture_start(cap: *Capture) FbStatus {
    cap.start() catch |e| return switch (e) {
        error.AlreadyRunning => .invalid_arg,
        else => .io_error,
    };
    return .ok;
}

export fn fb_capture_stop(cap: *Capture) void {
    cap.stop();
}

export fn fb_capture_destroy(cap: *Capture) void {
    cap.stop();
    allocator.destroy(cap);
}

export fn fb_capture_stats(cap: *const Capture, out: *Capture.Stats) void {
    out.* = cap.stats();
}

export fn fb_capture_last_error(cap: *const Capture) [*:0]const u8 {
    return cap.lastError().ptr;
}
```

Update `core/include/flashback_core.h` with the typedefs and prototypes from the Interfaces block (keep the header's "keep in lockstep" comment).

- [ ] **Step 4: Run Zig, verify green, count +3**

Run: `zig build --build-file core/build.zig test --summary all` then `zig build --build-file core/build.zig -Doptimize=ReleaseSafe` (rebuild the DLL Python will load).

- [ ] **Step 5: Failing Python tests**

Create `tests/unit/test_native_capture.py`:

```python
"""NativeCaptureSource + native.list_devices over a FAKE ctypes library.
No hardware, no DLL: every fb_* symbol is a Python stub recording calls."""
import ctypes as C

import pytest

from flashback_sampler.core import native
from flashback_sampler.core.native_capture import NativeCaptureSource


class _FakeLib:
    """Records calls; behaves like the real exports."""

    def __init__(self):
        self.calls = []
        self.started = False
        self.stats = (0, 0, 0, 48_000)  # running, frames, xruns, mix_rate
        self.err = b""
        self.devices = []

    def __getattr__(self, name):  # argtypes/restype assignment is a no-op
        def _fn(*a):
            self.calls.append((name, a))
            if name == "fb_capture_create":
                return 0xC0FFEE
            if name == "fb_capture_start":
                self.started = True
                self.stats = (1,) + self.stats[1:]
                return 0
            if name == "fb_capture_stop":
                self.started = False
                self.stats = (0,) + self.stats[1:]
            if name == "fb_capture_stats":
                st = a[1]._obj if hasattr(a[1], "_obj") else a[1]
                st.running, st.frames_written, st.xruns, st.mix_rate = self.stats
            if name == "fb_capture_last_error":
                return self.err
            if name == "fb_devices_list":
                arr, mx = a
                n = min(len(self.devices), mx)
                for i, d in enumerate(self.devices[:n]):
                    arr[i].kind, arr[i].is_default, arr[i].mix_rate, arr[i].mix_channels = d[:4]
                    arr[i].id, arr[i].name = d[4].encode(), d[5].encode()
                return n
            return None
        return _fn


class _FakeBuffer:
    _h = 0xB0B
    channels = 2
    sample_rate = 48_000


@pytest.fixture
def lib(monkeypatch):
    fake = _FakeLib()
    monkeypatch.setattr(native, "_lib", fake)
    monkeypatch.setattr(native, "_lib_tried", True)
    return fake


def test_conforms_to_capture_source(lib):
    from flashback_sampler.core.capture_source import CaptureSource
    src = NativeCaptureSource(_FakeBuffer(), kind="loopback")
    assert isinstance(src, CaptureSource)
    assert src.sample_rate == 48_000 and src.channels == 2


def test_rejects_non_native_buffer(lib):
    with pytest.raises(TypeError):
        NativeCaptureSource(object(), kind="loopback")


def test_rejects_unknown_kind(lib):
    with pytest.raises(ValueError):
        NativeCaptureSource(_FakeBuffer(), kind="telepathy")


def test_create_passes_spec_fields(lib):
    NativeCaptureSource(_FakeBuffer(), kind="process", device_id="{dev}", pid=77, sample_rate=44_100, channels=1)
    name, args = next(c for c in lib.calls if c[0] == "fb_capture_create")
    spec = args[1]._obj if hasattr(args[1], "_obj") else args[1]
    assert (spec.kind, spec.pid, spec.rate, spec.channels, spec.device_id) == (2, 77, 44_100, 1, b"{dev}")


def test_start_stop_round_trip_and_running(lib):
    src = NativeCaptureSource(_FakeBuffer(), kind="input", device_id="x")
    assert not src.is_running()
    src.start()
    assert src.is_running()
    src.start()  # idempotent — no second fb_capture_start
    assert sum(1 for c in lib.calls if c[0] == "fb_capture_start") == 1
    src.stop()
    src.stop()
    assert not src.is_running()
    assert sum(1 for c in lib.calls if c[0] == "fb_capture_stop") == 1


def test_stats_and_last_error_surface(lib):
    src = NativeCaptureSource(_FakeBuffer(), kind="loopback")
    lib.stats = (0, 12_345, 3, 44_100)
    assert src.frames_written() == 12_345
    assert src.xrun_count() == 3
    assert src.mix_rate() == 44_100
    assert src.last_error() is None
    lib.err = b"open failed: DeviceNotFound"
    assert src.last_error() == "open failed: DeviceNotFound"


def test_close_destroys_once(lib):
    src = NativeCaptureSource(_FakeBuffer(), kind="loopback")
    src.close()
    src.close()
    assert sum(1 for c in lib.calls if c[0] == "fb_capture_destroy") == 1


def test_list_devices_maps_kinds_and_strings(lib):
    lib.devices = [(0, 1, 48_000, 2, "{id-a}", "Speakers"), (1, 0, 44_100, 1, "{id-b}", "Mic")]
    got = native.list_devices()
    assert got == [
        {"kind": "loopback", "is_default": True, "mix_rate": 48_000, "mix_channels": 2, "id": "{id-a}", "name": "Speakers"},
        {"kind": "input", "is_default": False, "mix_rate": 44_100, "mix_channels": 1, "id": "{id-b}", "name": "Mic"},
    ]
```

Run: `python -m pytest tests/unit/test_native_capture.py -q`
Expected: FAIL (`native_capture` module missing).

- [ ] **Step 6: Implement Python**

Append to `flashback_sampler/core/native.py`:

```python
KIND_INTS = {"loopback": 0, "input": 1, "process": 2}
_KIND_NAMES = {v: k for k, v in KIND_INTS.items()}


class FbDevice(C.Structure):
    _fields_ = [("kind", C.c_uint8), ("is_default", C.c_uint8), ("mix_rate", C.c_uint32),
                ("mix_channels", C.c_uint16), ("id", C.c_char * 128), ("name", C.c_char * 128)]


class FbCaptureSpec(C.Structure):
    _fields_ = [("kind", C.c_uint8), ("pid", C.c_uint32), ("rate", C.c_uint32),
                ("channels", C.c_uint16), ("device_id", C.c_char_p)]


class FbCaptureStats(C.Structure):
    _fields_ = [("running", C.c_uint8), ("frames_written", C.c_uint64),
                ("xruns", C.c_uint32), ("mix_rate", C.c_uint32)]


def list_devices(max_devices: int = 64) -> list[dict]:
    """Every active WASAPI endpoint: render endpoints as kind="loopback",
    capture endpoints as kind="input". Empty when the library is missing
    or the OS has no backend."""
    lib = load()
    if lib is None:
        return []
    arr = (FbDevice * max_devices)()
    n = int(lib.fb_devices_list(arr, max_devices))
    return [
        {"kind": _KIND_NAMES.get(d.kind, "input"), "is_default": bool(d.is_default),
         "mix_rate": int(d.mix_rate), "mix_channels": int(d.mix_channels),
         "id": d.id.decode("utf-8", "replace"), "name": d.name.decode("utf-8", "replace")}
        for d in arr[:n]
    ]
```

Add to `_declare`:

```python
    lib.fb_devices_list.argtypes = [C.POINTER(FbDevice), C.c_size_t]
    lib.fb_devices_list.restype = C.c_size_t
    lib.fb_capture_create.argtypes = [C.c_void_p, C.POINTER(FbCaptureSpec)]
    lib.fb_capture_create.restype = C.c_void_p
    lib.fb_capture_start.argtypes = [C.c_void_p]
    lib.fb_capture_start.restype = C.c_int
    lib.fb_capture_stop.argtypes = [C.c_void_p]
    lib.fb_capture_stop.restype = None
    lib.fb_capture_destroy.argtypes = [C.c_void_p]
    lib.fb_capture_destroy.restype = None
    lib.fb_capture_stats.argtypes = [C.c_void_p, C.POINTER(FbCaptureStats)]
    lib.fb_capture_stats.restype = None
    lib.fb_capture_last_error.argtypes = [C.c_void_p]
    lib.fb_capture_last_error.restype = C.c_char_p
```

(`FbDevice`, `FbCaptureSpec`, `FbCaptureStats` must be defined above `_declare` in the file.)

Create `flashback_sampler/core/native_capture.py`:

```python
"""NativeCaptureSource — the CaptureSource that runs on the Zig core.

Python holds a handle. The Zig thread opens the WASAPI stream and writes
straight into the ring; nothing here touches audio frames. One class for
every kind ("loopback", "input", "process") — the kind is a field of the
spec the Zig side receives, not a Python class.
"""
from __future__ import annotations

import ctypes as C

from flashback_sampler.core import native


class NativeCaptureSource:
    def __init__(self, buffer, kind: str, device_id: str = "", pid: int = 0,
                 sample_rate: int = 48_000, channels: int = 2):
        h = getattr(buffer, "_h", None)
        if not h:
            raise TypeError("NativeCaptureSource needs a NativeAudioCircularBuffer (no native ring handle)")
        if kind not in native.KIND_INTS:
            raise ValueError(f"unknown capture kind {kind!r}; expected one of {sorted(native.KIND_INTS)}")
        lib = native.load()
        if lib is None:
            raise RuntimeError("flashback_core library not available")
        self._lib = lib
        self.kind = kind
        self.device_id = device_id
        self.pid = int(pid)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        # Keep the encoded id alive: the spec holds a raw pointer into it
        # for the duration of fb_capture_create (Zig copies it out).
        self._id_bytes = device_id.encode("utf-8")
        spec = native.FbCaptureSpec(native.KIND_INTS[kind], self.pid, self.sample_rate, self.channels, self._id_bytes)
        self._h = lib.fb_capture_create(h, C.byref(spec))
        if not self._h:
            raise RuntimeError("fb_capture_create failed (bad spec, or no capture backend on this OS)")
        self._started = False

    # -- CaptureSource protocol ----------------------------------------
    def start(self) -> None:
        if self._started:
            return
        status = self._lib.fb_capture_start(self._h)
        if status != native._OK:
            raise RuntimeError(f"fb_capture_start failed with status {status}")
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._lib.fb_capture_stop(self._h)
        self._started = False

    def is_running(self) -> bool:
        return bool(self._stats().running)

    def xrun_count(self) -> int:
        return int(self._stats().xruns)

    def last_error(self) -> str | None:
        raw = self._lib.fb_capture_last_error(self._h)
        return raw.decode("utf-8", "replace") if raw else None

    # -- extras -------------------------------------------------------
    def frames_written(self) -> int:
        return int(self._stats().frames_written)

    def mix_rate(self) -> int:
        return int(self._stats().mix_rate)

    def close(self) -> None:
        if self._h:
            self._lib.fb_capture_destroy(self._h)
            self._h = None

    def _stats(self) -> native.FbCaptureStats:
        st = native.FbCaptureStats()
        self._lib.fb_capture_stats(self._h, C.byref(st))
        return st

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
```

- [ ] **Step 7: Run Python tests, verify green**

Run: `python -m pytest tests/unit/test_native_capture.py tests/unit/test_native_smoke.py -q`
Expected: pass. Then `python -m pytest tests/unit -q -m "not audio_hw and not perf"` — still green (nothing else changed yet).

- [ ] **Step 8: Commit**

```bash
git add core/src/abi.zig core/include/flashback_core.h flashback_sampler/core/native.py flashback_sampler/core/native_capture.py tests/unit/test_native_capture.py
git commit -m "feat(core): capture + device ABI, ctypes bindings, NativeCaptureSource"
```

---

### Task 7: `audio_devices.py` runs on the native backend; sequester the Python backends

**Files:**
- Modify: `flashback_sampler/app/audio_devices.py`, `flashback_sampler/core/__init__.py` (drops the module-scope `from .capture import AudioCapture` — without this edit every `import flashback_sampler.core.*` breaks the moment `capture.py` moves), `tests/unit/test_audio_devices.py`, `tests/unit/test_capture_source.py`, `flashback_sampler/platform/capabilities.py` (docstring seam map), `PLATFORM.md`
- Move to `_ToRemove/`: `flashback_sampler/core/capture.py`, `flashback_sampler/core/loopback_capture.py`, `tests/test_loopback_soundcard.py`
- Create: `tests/hw/__init__.py`, `tests/hw/test_native_capture_hw.py`

**Interfaces:**
- Consumes: `native.list_devices()`, `NativeCaptureSource`.
- Produces: `CaptureDevice` gains `mix_rate: int | None = None`. `CaptureDevice.id` for loopback/input is now the **WASAPI endpoint id string** (`""` still means follow-default). `list_capture_devices()`, `default_capture_device()`, `build_capture_source()`, `probe_capture_rate()` keep their signatures. `probe_capture_rate` rule for loopback AND input: if `device.mix_rate` is known and `sample_rate > mix_rate` → not ok, fall back to `mix_rate` with the existing notice; else ok. `_wasapi_output_mix_rate`, `_strip_loopback_hint_suffix`, and the `sd` module import are deleted.

- [ ] **Step 1: Failing tests**

In `tests/unit/test_audio_devices.py`, delete the tests built on the `sounddevice` probing that dies here (`test_probe_input_ok`, `test_probe_input_falls_back_to_device_default`, `test_probe_loopback_over_mix_rate_falls_back`, `test_probe_loopback_at_or_below_mix_rate_ok`, `test_probe_loopback_unknown_mix_rate_is_permissive`, `test_probe_input_passes_integer_device_to_check_input_settings`, `test_probe_input_fallback_passes_integer_device_to_query_devices`, `test_probe_loopback_hint_strips_loopback_suffix_to_match_real_name`, and `test_build_capture_source_input_kind_requires_integer_id` — input ids become endpoint strings, so that rule is gone) and add:

```python
def _fake_devices():
    return [
        {"kind": "loopback", "is_default": True, "mix_rate": 48_000, "mix_channels": 2, "id": "{spk}", "name": "Speakers"},
        {"kind": "loopback", "is_default": False, "mix_rate": 96_000, "mix_channels": 2, "id": "{hp}", "name": "Headphones"},
        {"kind": "input", "is_default": True, "mix_rate": 44_100, "mix_channels": 1, "id": "{mic}", "name": "Mic"},
    ]


def test_list_capture_devices_maps_native_list(monkeypatch):
    monkeypatch.setattr(audio_devices.native, "list_devices", _fake_devices)
    devs = audio_devices.list_capture_devices()
    kinds = [(d.kind, d.id, d.mix_rate, d.is_default) for d in devs]
    assert ("loopback", "{spk}", 48_000, True) in kinds
    assert ("input", "{mic}", 44_100, True) in kinds
    assert all(d.name.endswith("[loopback]") for d in devs if d.kind == "loopback")


def test_probe_over_mix_rate_falls_back_for_loopback_and_input(monkeypatch):
    for kind in ("loopback", "input"):
        dev = audio_devices.CaptureDevice(kind=kind, name="X", id="{x}", mix_rate=48_000)
        r = audio_devices.probe_capture_rate(dev, 96_000, 2)
        assert not r.ok and r.effective_rate == 48_000 and "48000 Hz" in r.message


def test_probe_at_or_below_mix_rate_ok():
    dev = audio_devices.CaptureDevice(kind="loopback", name="X", id="{x}", mix_rate=48_000)
    assert audio_devices.probe_capture_rate(dev, 48_000, 2).ok
    assert audio_devices.probe_capture_rate(dev, 44_100, 2).ok


def test_probe_unknown_mix_rate_is_permissive():
    dev = audio_devices.CaptureDevice(kind="input", name="X", id="{x}")
    assert audio_devices.probe_capture_rate(dev, 192_000, 2).ok


def test_build_capture_source_loopback_and_input_use_native(monkeypatch):
    seen = {}

    class _Src:
        def __init__(self, buffer, kind, device_id="", pid=0, sample_rate=48_000, channels=2):
            seen.update(kind=kind, device_id=device_id, pid=pid, sample_rate=sample_rate, channels=channels)

    monkeypatch.setattr(audio_devices, "NativeCaptureSource", _Src)
    audio_devices.build_capture_source(audio_devices.DEFAULT_LOOPBACK, _FakeBuffer(), 48_000, 2)
    assert seen == dict(kind="loopback", device_id="", pid=0, sample_rate=48_000, channels=2)
    audio_devices.build_capture_source(audio_devices.CaptureDevice(kind="input", name="Mic", id="{mic}"), _FakeBuffer(), 44_100, 1)
    assert seen == dict(kind="input", device_id="{mic}", pid=0, sample_rate=44_100, channels=1)
```

`test_default_loopback_sentinel_follows_live_os_default` and `test_named_loopback_still_pins_to_that_speaker` (tests/unit/test_audio_devices.py:77-92) both build a real `LoopbackCapture` and assert on `cap.speaker_name` — rewrite them to the `NativeCaptureSource` monkeypatch used above, asserting `device_id == ""` for `DEFAULT_LOOPBACK` (follow-default) and `device_id == device.id` for a named device.

`test_apply_rate_probe_rebuilds_preset` (tests/unit/test_audio_devices.py:~269) monkeypatches `_wasapi_output_mix_rate`, which this task deletes — rewrite it to the new mechanism: build `dev = audio_devices.CaptureDevice(kind="loopback", name="X", id="{x}", mix_rate=48_000)` and call `apply_rate_probe(preset_96k, dev)` expecting the 48 000 Hz adjustment with a notice, then `apply_rate_probe(preset_48k, dev)` expecting the same preset back with no notice. (With `device=None` the new probe is permissive, so the old None-device fixture cannot express this test.)

In `tests/unit/test_capture_source.py`, replace `test_audio_capture_conforms_without_starting` and `test_loopback_capture_conforms_without_starting` with:

```python
def test_native_capture_source_conforms_without_starting(monkeypatch):
    from flashback_sampler.core import native
    from flashback_sampler.core.native_capture import NativeCaptureSource

    class _Lib:
        def __getattr__(self, name):
            return lambda *a: 1 if name == "fb_capture_create" else None

    monkeypatch.setattr(native, "_lib", _Lib())
    monkeypatch.setattr(native, "_lib_tried", True)

    class _Buf:
        _h = 1

    src = NativeCaptureSource(_Buf(), kind="loopback")
    assert isinstance(src, CaptureSource)
```

Run: `python -m pytest tests/unit/test_audio_devices.py tests/unit/test_capture_source.py -q`
Expected: FAIL (`native` attribute missing on `audio_devices`, `NativeCaptureSource` missing, `mix_rate` field missing).

- [ ] **Step 2: Implement**

`flashback_sampler/app/audio_devices.py`:
- Imports: delete the `sounddevice` try/import block and the `sd = None` fallback; add `from flashback_sampler.core import native` and `from flashback_sampler.core.native_capture import NativeCaptureSource`.
- `CaptureDevice`: add `mix_rate: int | None = None` after `follow_default`. Update the docstring: loopback/input `id` is the WASAPI endpoint id; `""` = follow default.
- Replace `_list_loopback_devices` and `_list_input_devices` with:

```python
def _list_native_devices() -> list[CaptureDevice]:
    out: list[CaptureDevice] = []
    for d in native.list_devices():
        if d["kind"] not in ("loopback", "input"):
            continue
        is_loop = d["kind"] == "loopback"
        if is_loop and not loopback_supported():
            continue
        out.append(CaptureDevice(
            kind=d["kind"],
            name=f'{d["name"]}  [loopback]' if is_loop else d["name"],
            id=d["id"],
            sample_rate=d["mix_rate"] or 48_000,
            channels=min(2, d["mix_channels"] or 2),
            is_default=d["is_default"],
            mix_rate=d["mix_rate"] or None,
        ))
    return out
```

  and `list_capture_devices()` becomes `return _list_native_devices()`.
- `build_capture_source`: the `loopback` and `input` branches both become `return NativeCaptureSource(buffer=buffer, kind=device.kind, device_id="" if device.follow_default else device.id, sample_rate=sample_rate, channels=channels)`. Leave `process_loopback` on `ProcessLoopbackCapture` (Task 10 moves it).
- `probe_capture_rate`: delete the `sd is None` early return, the `input` branch, `_wasapi_output_mix_rate`, `_strip_loopback_hint_suffix`. Body becomes:

```python
    mix = device.mix_rate if device is not None else None
    if mix is None or sample_rate <= mix:
        return ProbeResult(True, sample_rate)
    return ProbeResult(
        False, mix,
        f"Output mix format is {mix} Hz — a {sample_rate} Hz capture "
        f"won't contain content above {mix // 2} Hz. "
        f"Capturing at {mix} Hz instead.",
    )
```

  Docstring: "Loopback and input rates above the endpoint's mix format add no information."
- `list_output_devices` / `default_output_device` / `OutputDevice` stay on `sounddevice` for now (imported lazily inside the function, as today) — playback moves in PR e.

Sequester — **`_ToRemove/` is gitignored, so `git mv` into it fails and stages nothing.** The recipe (per file: `capture.py`, `loopback_capture.py`, `tests/test_loopback_soundcard.py`):

```bash
mkdir -p _ToRemove/flashback_sampler/core _ToRemove/tests
mv flashback_sampler/core/capture.py _ToRemove/flashback_sampler/core/
mv flashback_sampler/core/loopback_capture.py _ToRemove/flashback_sampler/core/
mv tests/test_loopback_soundcard.py _ToRemove/tests/
git add -u flashback_sampler tests
```

The PR diff shows plain deletions; the bytes survive locally under `_ToRemove/` and the PR body lists them for the owner's one-shot removal approval. Edit `flashback_sampler/core/__init__.py`: delete `from .capture import AudioCapture` (module scope — the package import breaks otherwise). Grep for remaining importers: `grep -rn "core.capture import\|loopback_capture import\|AudioCapture\b\|LoopbackCapture\b" flashback_sampler tests docs soak_test.py flashback_sampler.spec packaging README.md PLATFORM.md` — fix each hit (`capabilities.py` docstring seam list, `PLATFORM.md` seam table row "Loopback backends" → `core/native_capture.py` + `core/WasapiBackend.zig`, `README.md` if it names them). `soak_test.py` still imports `LoopbackCapture` — leave it broken for now, Task 8 Step 1 ports it before the "after" soak. `mixed_capture.py` imports nothing from them (verify).

Create `tests/hw/__init__.py` (empty) and `tests/hw/test_native_capture_hw.py`:

```python
"""Hardware tests: real WASAPI endpoints. Run by hand on a Windows box:
    pytest tests/hw -m audio_hw -s
Play audio through the default output while this runs."""
import time

import pytest

from flashback_sampler.core import native
from flashback_sampler.core.buffer import make_ring_buffer
from flashback_sampler.core.native_capture import NativeCaptureSource

pytestmark = pytest.mark.audio_hw


@pytest.fixture(scope="module")
def lib():
    if native.load() is None:
        pytest.skip("flashback_core not built")
    return native.load()


def test_list_devices_has_a_default_loopback(lib):
    devs = native.list_devices()
    assert any(d["kind"] == "loopback" and d["is_default"] for d in devs), devs
    assert all(d["id"] and d["name"] for d in devs)


@pytest.mark.parametrize("kind", ["loopback", "input"])
def test_default_endpoint_captures_two_seconds(lib, kind):
    buf = make_ring_buffer(duration_seconds=10, sample_rate=48_000, channels=2)
    src = NativeCaptureSource(buf, kind=kind)
    src.start()
    time.sleep(2.0)
    running = src.is_running()
    frames = src.frames_written()
    err = src.last_error()
    src.stop()
    src.close()
    buf.close()
    assert running, err
    assert err is None, err
    # 2 s at 48 kHz, minus start-up: comfortably above 1 s of frames.
    assert frames > 48_000, frames
    print(f"{kind}: frames={frames} xruns={src.xrun_count()} mix_rate={src.mix_rate()}")


def test_loopback_at_96k_when_mix_is_48k_reports_mix_rate(lib):
    """AUTOCONVERTPCM: we ask 96 kHz stereo; the engine converts. mix_rate
    tells the truth so the UI can warn (spec: 'honest rate')."""
    buf = make_ring_buffer(duration_seconds=10, sample_rate=96_000, channels=2)
    src = NativeCaptureSource(buf, kind="loopback", sample_rate=96_000)
    src.start()
    time.sleep(1.0)
    ok = src.is_running() and src.last_error() is None
    mix = src.mix_rate()
    src.stop(); src.close(); buf.close()
    assert ok
    assert mix > 0
```

- [ ] **Step 3: Run unit tests, verify green; run hardware tests by hand**

Run: `python -m pytest tests/unit -q -m "not audio_hw and not perf"`
Expected: green. Report the count: Task 0's baseline, minus the 9 deleted here, plus the 8 from Task 6 and the 6 added here (the three rewrites — the two loopback tests and `test_apply_rate_probe_rebuilds_preset` — are count-neutral).

Run (Windows box, audio playing): `python -m pytest tests/hw -m audio_hw -s -q`
Expected: all pass. **If `test_default_endpoint_captures_two_seconds[loopback]` fails with `open failed: FormatRejected`**, the AUTOCONVERTPCM-on-loopback risk fired: comment the finding on `<A>` with the HRESULT, and switch `open()` to request the mix format when `kind != .input` (call `GetMixFormat`, pass that `WAVEFORMATEX` to `Initialize` without the two AUTOCONVERT flags, and set `src_fmt` from it — `WAVEFORMATEXTENSIBLE` float has `wFormatTag == 0xFFFE` and `wBitsPerSample == 32`; treat as `.f32`). `convert.packet` already handles the channel conform. Record which path shipped in the PR body.

- [ ] **Step 4: Full app smoke**

Run the app (`python -m flashback_sampler` or the project's `run` skill), add a loopback source and a mic source, arm, play audio, confirm the waveform moves and the status bar shows no error. Stop both. Check Task Manager: no lingering CPU after stop.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/audio_devices.py flashback_sampler/core/__init__.py flashback_sampler/platform/capabilities.py PLATFORM.md tests/unit/test_audio_devices.py tests/unit/test_capture_source.py tests/hw
git add -u flashback_sampler tests
git commit -m "feat: capture runs on the Zig backend -- audio_devices enumerates/opens via native, Python capture backends sequestered"
```

---

### Task 8: PR a — measure, document, hand off

**Files:**
- Modify: `README.md` (dependency list, "how capture works" line), `docs/superpowers/specs/2026-08-16-zig-core-phase2-design.md` (record the deviations listed in the plan header under a "Deviations" heading)

- [ ] **Step 1: The "after" soak and the #26 number**

First port `soak_test.py` (untracked, repo root) to the native path — Task 7 sequestered the `LoopbackCapture` it imports:

- `from flashback_sampler.core.native_capture import NativeCaptureSource` replaces the `loopback_capture` import.
- `cap = NativeCaptureSource(buf, kind="loopback")` replaces the `LoopbackCapture(...)` construction (there is no `on_level` callback any more).
- `cap._dropped_callbacks` → `cap.xrun_count()` (both print sites); label the column `xruns`. The `warnings`-based discontinuity counter counted warnings the Python capture thread emitted — that thread is gone, so drop the `warnings` plumbing and report `cap.xrun_count()` as the discontinuity signal too.
- The `peaks` signal check: each 5 s tick, append `float(np.max(np.abs(buf.get_latest(0.5))))` — the "blocks with signal" line at the end keeps its meaning.

Then run `python soak_test.py` with the same duration and audio playing as Task 0 Step 3. Post the table on epic #17 as "Phase 2 — after PR a". Then measure write-tail latency the way #26's original measurement did (see the issue for the script; it timed `NativeAudioCircularBuffer.write` from Python) — that path no longer exists on the capture path, so instead report `xruns` and `frames_written` vs wall clock from `fb_capture_stats` over a 5-minute run, and the app's idle-armed RSS/CPU from Task Manager beside Task 0's numbers. Comment the comparison on #26 and on `<A>`.

- [ ] **Step 2: Docs**

`README.md`: dependency section drops the mention of `soundcard` for capture (it is still installed until PR f; say "capture runs in the Zig core; `soundcard`/`sounddevice` remain only for output-device listing and preview playback until phase 2 PR e"). Spec: the "Deviations" section already exists (it landed with this plan's commit) — verify it still matches; do not add a duplicate. Record any NEW deviation this PR produced.

- [ ] **Step 3: Local gates + PR**

Run: `zig fmt --check core/build.zig core/src`; `python -m pytest tests/unit -q -m "not audio_hw and not perf"`; `zig build --build-file core/build.zig test --summary all` (record the count; it must be Task 0's + 28: 7 convert + 3 fake + 6 capture + 5 guards + 4 wasapi + 3 abi = 72 from a 44 baseline — adjust if a task changed its count and say so); `zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu` and `-Dtarget=aarch64-macos` (the cross-compile legs, locally — no CI runs on this PR).

Push `feat/zig-capture`; open the PR against `dev` with:
- Title: `feat(core): capture runs on the Zig backend -- WASAPI bindings, Capture thread, device enumeration`
- Body: what/why (3 bullets), `Closes #<A>`, `Closes #21`, `Closes #28`, the soak/RSS comparison table, the AUTOCONVERTPCM finding, the `_ToRemove/` contents list (deletion approval), **"Zig concepts in this PR"** (COM vtables as `extern struct` + `callconv(.winapi)`; `*anyopaque` + vtable interfaces; `std.Thread.spawn` + atomics for a producer thread; `builtin.os.tag` comptime gating; `errdefer` chains in `open`), and an explicit "findings open/closed" statement.
- No CI fires on this PR (budget) — state in the body that the local gates above all passed, with the counts.

Hand the link to the owner. Do not merge.

---

### Task 9: Process loopback on the Zig backend (PR b)

**Files:**
- Modify: `core/src/wasapi.zig` (activation params, completion handler, `ActivateAudioInterfaceAsync` resolved at call time via `LoadLibraryW`/`GetProcAddress`, Toolhelp32), `core/src/WasapiBackend.zig` (`activate` gains the `.process` arm; `enumerateProcesses`), `core/src/abi.zig`, `core/include/flashback_core.h`, `flashback_sampler/core/native.py`
- Branch: `git checkout -b feat/zig-process-loopback dev` after PR a merges.

**Interfaces:**
- Produces:
  ```c
  typedef struct FbProcess { uint32_t pid; uint32_t ppid; char name[128]; } FbProcess;
  size_t fb_processes_list(FbProcess *out, size_t max);  /* every running process, Toolhelp32; 0 on non-Windows */
  ```
  `ppid` is in the struct because the port's `resolve_audio_root_pid` (`win32_process_loopback.py:521-549`) needs the parent chain — see Task 10, which keeps that behaviour.
  Python, all derived from one private `native._process_entries() -> list[tuple[int, int, str]]` (pid, ppid, name — one `fb_processes_list` call):
  - `native.list_processes() -> list[tuple[int, str]]` sorted by `(name.lower(), pid)` — the shape `enumerate_audio_processes` returns today (`win32_process_loopback.py:552`); the picker keeps unpacking `(pid, name)`.
  - `native.resolve_root_pid(pid: int) -> int` — port of `resolve_audio_root_pid` (`win32_process_loopback.py:521-549`): walk up same-named ancestors (Spotify/Discord/Chrome children share an exe; only the root's audio session covers the tree); return `pid` unchanged when it is absent from the snapshot or the parent chain breaks. Same cycle guard (visited set).
- `Backend.Spec.kind == .process` + `pid` opens the `VAD\Process_Loopback` virtual device including the target process tree.

- [ ] **Step 1: Failing tests**

Append to `core/src/wasapi.zig`:

```zig
test "AUDIOCLIENT_ACTIVATION_PARAMS is 12 bytes and PROCESS params sit at offset 4" {
    try std.testing.expectEqual(@as(usize, 12), @sizeOf(AUDIOCLIENT_ACTIVATION_PARAMS));
    try std.testing.expectEqual(@as(usize, 4), @offsetOf(AUDIOCLIENT_ACTIVATION_PARAMS, "params"));
}

test "PROCESSENTRY32W layout: szExeFile at 44, size 568" {
    try std.testing.expectEqual(@as(usize, 44), @offsetOf(PROCESSENTRY32W, "szExeFile"));
    try std.testing.expectEqual(@as(usize, 568), @sizeOf(PROCESSENTRY32W));
}
```

Append to `core/src/abi.zig`:

```zig
test "fb_processes_list with max 0 returns 0; on Windows a real list contains this process" {
    try std.testing.expectEqual(@as(usize, 0), fb_processes_list(undefined, 0));
    // Comptime-known branch: the Windows arm is not analyzed on other
    // targets, so the wasapi import inside it never reaches the Linux leg.
    if (builtin.os.tag == .windows) {
        var out: [1024]FbProcess = undefined;
        const n = fb_processes_list(&out, out.len);
        try std.testing.expect(n > 0);
        const me = @import("wasapi.zig").GetCurrentProcessId();
        var found = false;
        for (out[0..n]) |p| {
            if (p.pid == me) found = true;
        }
        try std.testing.expect(found);
    } else return error.SkipZigTest;
}
```

Run: `zig build --build-file core/build.zig test` → compile error.

- [ ] **Step 2: Declarations in `wasapi.zig`**

```zig
pub const IID_IActivateAudioInterfaceCompletionHandler = guid("{41D949AB-9862-444A-80F6-C261334DA5EB}");
pub const IID_IAgileObject = guid("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}");
pub const VT_BLOB: u16 = 0x41;
pub const AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK: u32 = 1;
pub const PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE: u32 = 0;
pub const VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = std.unicode.utf8ToUtf16LeStringLiteral("VAD\\Process_Loopback");

pub const AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS = extern struct { TargetProcessId: u32, ProcessLoopbackMode: u32 };
pub const AUDIOCLIENT_ACTIVATION_PARAMS = extern struct {
    ActivationType: u32,
    params: extern union { ProcessLoopbackParams: AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS },
};

pub const IActivateAudioInterfaceAsyncOperation = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        GetActivateResult: *const fn (*IActivateAudioInterfaceAsyncOperation, *HRESULT, *?*anyopaque) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IActivateAudioInterfaceAsyncOperation) void {
        _ = self.vtbl.base.Release(self);
    }
};

/// A COM object WE implement: the completion handler. COM only needs the
/// vtable pointer first; the fields after it are ours. `done` is set from
/// COM's thread; the opener spins on it. Also claims IAgileObject so
/// ActivateAudioInterfaceAsync accepts a handler from an MTA thread.
/// `done` is a plain u32 accessed with @atomicStore/@atomicLoad —
/// std.atomic.Value is not an extern-compatible type, and this struct
/// must be extern for COM to read `vtbl` at offset 0.
pub const CompletionHandler = extern struct {
    vtbl: *const VTable = &vtable,
    refs: u32 = 1,
    done: u32 = 0,
    op: ?*IActivateAudioInterfaceAsyncOperation = null,

    pub const VTable = extern struct {
        base: IUnknownVTable,
        ActivateCompleted: *const fn (*CompletionHandler, *IActivateAudioInterfaceAsyncOperation) callconv(.winapi) HRESULT,
    };
    const vtable = VTable{
        .base = .{ .QueryInterface = qi, .AddRef = addRef, .Release = release },
        .ActivateCompleted = activateCompleted,
    };
    fn qi(this: *anyopaque, riid: *const GUID, out: *?*anyopaque) callconv(.winapi) HRESULT {
        if (std.meta.eql(riid.*, IID_IUnknown) or std.meta.eql(riid.*, IID_IActivateAudioInterfaceCompletionHandler) or std.meta.eql(riid.*, IID_IAgileObject)) {
            out.* = this;
            _ = addRef(this);
            return 0;
        }
        out.* = null;
        return @bitCast(@as(u32, 0x80004002)); // E_NOINTERFACE
    }
    fn addRef(this: *anyopaque) callconv(.winapi) u32 {
        const self: *CompletionHandler = @ptrCast(@alignCast(this));
        self.refs += 1;
        return self.refs;
    }
    fn release(this: *anyopaque) callconv(.winapi) u32 {
        const self: *CompletionHandler = @ptrCast(@alignCast(this));
        self.refs -= 1; // stack-owned by the opener; never freed here
        return self.refs;
    }
    fn activateCompleted(self: *CompletionHandler, op: *IActivateAudioInterfaceAsyncOperation) callconv(.winapi) HRESULT {
        _ = op.vtbl.base.AddRef(op);
        self.op = op;
        @atomicStore(u32, &self.done, 1, .release);
        return 0;
    }
};

pub const ActivateAudioInterfaceAsyncFn = *const fn ([*:0]const u16, *const GUID, ?*PROPVARIANT, *CompletionHandler, *?*IActivateAudioInterfaceAsyncOperation) callconv(.winapi) HRESULT;

pub extern "kernel32" fn LoadLibraryW(name: [*:0]const u16) callconv(.winapi) ?*anyopaque;
pub extern "kernel32" fn GetProcAddress(module: *anyopaque, name: [*:0]const u8) callconv(.winapi) ?*anyopaque;
pub extern "kernel32" fn GetCurrentProcessId() callconv(.winapi) u32;

/// Mmdevapi.dll is resolved at call time, not linked: the export exists
/// only on Windows 10 2004+, and a missing export must be a clean error,
/// not a failed process start. The module handle is deliberately never
/// freed — the function pointer must outlive this call.
pub fn activateAudioInterfaceAsync() ?ActivateAudioInterfaceAsyncFn {
    const module = LoadLibraryW(std.unicode.utf8ToUtf16LeStringLiteral("Mmdevapi.dll")) orelse return null;
    const p = GetProcAddress(module, "ActivateAudioInterfaceAsync") orelse return null;
    return @ptrCast(p);
}

// ── Toolhelp32 (process list) ────────────────────────────────────────
pub const TH32CS_SNAPPROCESS: u32 = 2;
pub const INVALID_HANDLE_VALUE: usize = std.math.maxInt(usize);
pub const PROCESSENTRY32W = extern struct {
    dwSize: u32,
    cntUsage: u32,
    th32ProcessID: u32,
    th32DefaultHeapID: usize,
    th32ModuleID: u32,
    cntThreads: u32,
    th32ParentProcessID: u32,
    pcPriClassBase: i32,
    dwFlags: u32,
    szExeFile: [260]u16,
};
pub extern "kernel32" fn CreateToolhelp32Snapshot(flags: u32, pid: u32) callconv(.winapi) ?HANDLE;
pub extern "kernel32" fn Process32FirstW(snap: HANDLE, entry: *PROCESSENTRY32W) callconv(.winapi) i32;
pub extern "kernel32" fn Process32NextW(snap: HANDLE, entry: *PROCESSENTRY32W) callconv(.winapi) i32;
```

If `@sizeOf(PROCESSENTRY32W)` is not 568 on x64, an `extern struct` field is mis-typed — the layout test is the gate; fix the field, not the test.

- [ ] **Step 3: `WasapiBackend.zig` — the `.process` arm and process enumeration**

In `activate`, first line:

```zig
    if (spec.kind == .process) return activateProcess(spec.pid);
```

Add:

```zig
/// Mirrors win32_process_loopback.py:855-935: activation params in a
/// VT_BLOB PROPVARIANT, our CompletionHandler, then spin (bounded) until
/// ActivateCompleted fires, then GetActivateResult → IAudioClient.
/// Apartment: this call HARD-REQUIRES a WinRT apartment — the port
/// measured E_ILLEGAL_METHOD_CALL under plain CoInitializeEx
/// (win32_process_loopback.py:816-819). open() already ran
/// RoInitialize(RO_INIT_MULTITHREADED) on this thread (Task 5), so no
/// per-kind branch is needed here.
fn activateProcess(pid: u32) Backend.Error!*w.IAudioClient {
    const activate_fn = w.activateAudioInterfaceAsync() orelse return error.Unsupported;
    var params = w.AUDIOCLIENT_ACTIVATION_PARAMS{
        .ActivationType = w.AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        .params = .{ .ProcessLoopbackParams = .{ .TargetProcessId = pid, .ProcessLoopbackMode = w.PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE } },
    };
    var pv = w.PROPVARIANT{ .vt = w.VT_BLOB, .data = .{ .blob = .{ .cbSize = @sizeOf(w.AUDIOCLIENT_ACTIVATION_PARAMS), .pBlobData = &params } } };
    var handler = w.CompletionHandler{};
    var op: ?*w.IActivateAudioInterfaceAsyncOperation = null;
    if (w.failed(activate_fn(w.VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, &w.IID_IAudioClient, &pv, &handler, &op))) return error.ActivationFailed;
    defer if (op) |o| o.release();
    var waited: u32 = 0;
    while (@atomicLoad(u32, &handler.done, .acquire) == 0) : (waited += 10) {
        if (waited >= 5_000) return error.ActivationFailed;
        w.Sleep(10);
    }
    const done_op = handler.op orelse return error.ActivationFailed;
    defer done_op.release();
    var hr_act: w.HRESULT = 0;
    var raw: ?*anyopaque = null;
    if (w.failed(done_op.vtbl.GetActivateResult(done_op, &hr_act, &raw))) return error.ActivationFailed;
    if (w.failed(hr_act) or raw == null) return error.ActivationFailed;
    return @ptrCast(@alignCast(raw.?));
}

pub const Process = extern struct { pid: u32, ppid: u32, name: [128]u8 };

pub fn enumerateProcesses(out: []Process) usize {
    const snap = w.CreateToolhelp32Snapshot(w.TH32CS_SNAPPROCESS, 0) orelse return 0;
    if (@intFromPtr(snap) == w.INVALID_HANDLE_VALUE) return 0;
    defer _ = w.CloseHandle(snap);
    var entry: w.PROCESSENTRY32W = undefined;
    entry.dwSize = @sizeOf(w.PROCESSENTRY32W);
    if (w.Process32FirstW(snap, &entry) == 0) return 0;
    var n: usize = 0;
    while (n < out.len) {
        if (entry.th32ProcessID != 0) {
            out[n].pid = entry.th32ProcessID;
            out[n].ppid = entry.th32ParentProcessID;
            const z: [*:0]const u16 = @ptrCast(&entry.szExeFile);
            _ = w.wtf16ToUtf8Z(&out[n].name, z);
            n += 1;
        }
        if (w.Process32NextW(snap, &entry) == 0) break;
    }
    return n;
}
```

`abi.zig`:

```zig
pub const FbProcess = if (builtin.os.tag == .windows) @import("WasapiBackend.zig").Process else extern struct { pid: u32, ppid: u32, name: [128]u8 };

export fn fb_processes_list(out: [*]FbProcess, max: usize) usize {
    if (max == 0) return 0;
    if (builtin.os.tag == .windows) return @import("WasapiBackend.zig").enumerateProcesses(out[0..max]);
    return 0;
}
```

Header: add `FbProcess` + `fb_processes_list`. `native.py`: `class FbProcess(C.Structure): _fields_ = [("pid", C.c_uint32), ("ppid", C.c_uint32), ("name", C.c_char * 128)]`, `_declare` lines, and:

```python
def _process_entries(max_processes: int = 4096) -> list[tuple[int, int, str]]:
    """One Toolhelp32 snapshot via the core: (pid, ppid, exe_name) rows.
    Both public views below derive from this."""
    lib = load()
    if lib is None:
        return []
    arr = (FbProcess * max_processes)()
    n = int(lib.fb_processes_list(arr, max_processes))
    return [(int(p.pid), int(p.ppid), p.name.decode("utf-8", "replace"))
            for p in arr[:n] if p.pid > 0 and p.name]


def list_processes(max_processes: int = 4096) -> list[tuple[int, str]]:
    rows = [(pid, name) for pid, _ppid, name in _process_entries(max_processes)]
    rows.sort(key=lambda t: (t[1].lower(), t[0]))
    return rows


def resolve_root_pid(pid: int) -> int:
    """Walk up from `pid` to the highest ancestor sharing the same exe
    name (port of win32_process_loopback.resolve_audio_root_pid — apps
    like Spotify/Chrome play audio from the ROOT of a same-exe tree).
    Returns `pid` unchanged if it is absent or the chain breaks."""
    procs = {p: (pp, name) for p, pp, name in _process_entries()}
    if pid not in procs:
        return pid
    name_lc = procs[pid][1].lower()
    current, visited = pid, set()
    while current not in visited:
        visited.add(current)
        parent, _ = procs.get(current, (0, ""))
        if parent <= 0 or parent not in procs or procs[parent][1].lower() != name_lc:
            break
        current = parent
    return current
```

- [ ] **Step 4: Run, verify green, count +3; cross-compile leg**

Run: `zig build --build-file core/build.zig test --summary all`; `zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu`; `zig fmt --check core/src`.

- [ ] **Step 5: Commit**

```bash
git add core/src/wasapi.zig core/src/WasapiBackend.zig core/src/abi.zig core/include/flashback_core.h flashback_sampler/core/native.py
git commit -m "feat(core): per-process loopback via ActivateAudioInterfaceAsync + Toolhelp32 process list"
```

---

### Task 10: Python switches process loopback to native; sequester the COM port

**Files:**
- Modify: `flashback_sampler/app/audio_devices.py` (`process_loopback` branch), `flashback_sampler/app/process_picker_dialog.py` (imports), `flashback_sampler/core/native_capture.py` (`is_process_loopback_supported`), `flashback_sampler/io/__init__.py` (docstring names the moved module), `flashback_sampler.spec` (`hiddenimports` — a hard PyInstaller failure if left), `packaging/README.md`, `README.md`, `tests/unit/test_process_loopback.py`, `tests/hw/test_native_capture_hw.py`, `pyproject.toml` (`[tool.coverage.run] omit`), `flashback_sampler/platform/capabilities.py`, `PLATFORM.md`
- Move to `_ToRemove/`: `flashback_sampler/io/win32_process_loopback.py`

**Interfaces:**
- Produces: `native_capture.is_process_loopback_supported() -> bool` (`sys.platform == "win32"` and `sys.getwindowsversion().build >= 19041` — the SAME floor the port enforces, `win32_process_loopback.py:76-88`; do not raise it) replaces `win32_process_loopback.is_supported`. `native.list_processes()` replaces `enumerate_audio_processes()`; `native.resolve_root_pid()` (Task 9) replaces `resolve_audio_root_pid()`.

- [ ] **Step 1: Failing tests**

Rewrite `tests/unit/test_process_loopback.py`: delete the GUID/struct-layout tests (they tested the ctypes port; the Zig side now owns those layouts and tests them). The rewritten file opens with its own imports and fake buffer — the old file's per-test `FakeBuffer` classes go with it:

```python
import pytest

import flashback_sampler.app.audio_devices as audio_devices
from flashback_sampler.core import native


class _FakeBuffer:  # mirrors tests/unit/test_audio_devices.py
    _h = 0xB0B
    channels = 2
    sample_rate = 48_000
```

Keep and retarget:

```python
def test_is_supported_matches_platform(monkeypatch):
    from flashback_sampler.core import native_capture as nc
    monkeypatch.setattr(nc.sys, "platform", "linux")
    assert nc.is_process_loopback_supported() is False


def test_list_processes_empty_without_library(monkeypatch):
    from flashback_sampler.core import native
    monkeypatch.setattr(native, "_lib", None)
    monkeypatch.setattr(native, "_lib_tried", True)
    assert native.list_processes() == []


def test_build_capture_source_routes_process_loopback(monkeypatch):
    seen = {}

    class _Src:
        def __init__(self, buffer, kind, device_id="", pid=0, sample_rate=48_000, channels=2):
            seen.update(kind=kind, pid=pid, sample_rate=sample_rate, channels=channels)

    monkeypatch.setattr(audio_devices, "NativeCaptureSource", _Src)
    dev = audio_devices.CaptureDevice(kind="process_loopback", name="game.exe", id="4242")
    audio_devices.build_capture_source(dev, _FakeBuffer(), 48_000, 2)
    assert seen == dict(kind="process", pid=4242, sample_rate=48_000, channels=2)


def test_build_capture_source_rejects_non_integer_pid():
    dev = audio_devices.CaptureDevice(kind="process_loopback", name="x", id="nope")
    with pytest.raises(ValueError):
        audio_devices.build_capture_source(dev, _FakeBuffer(), 48_000, 2)


def test_resolve_root_pid_walks_same_named_chain(monkeypatch):
    # spotify 300 -> 200 -> 100 (all same exe); 100's parent is explorer.
    entries = [(1, 0, "explorer.exe"), (100, 1, "spotify.exe"),
               (200, 100, "spotify.exe"), (300, 200, "spotify.exe")]
    monkeypatch.setattr(native, "_process_entries", lambda *a: entries)
    assert native.resolve_root_pid(300) == 100
    assert native.resolve_root_pid(100) == 100


def test_resolve_root_pid_unknown_or_broken_chain_is_identity(monkeypatch):
    entries = [(200, 999, "game.exe")]  # parent absent from snapshot
    monkeypatch.setattr(native, "_process_entries", lambda *a: entries)
    assert native.resolve_root_pid(200) == 200
    assert native.resolve_root_pid(555) == 555
```

Add to `tests/hw/test_native_capture_hw.py`:

```python
def test_process_loopback_of_this_python_process_opens(lib):
    """Opens the process-loopback client for our own PID. It has no render
    stream so frames stay 0 — the assertion is that activation SUCCEEDS."""
    import os
    buf = make_ring_buffer(duration_seconds=5, sample_rate=48_000, channels=2)
    src = NativeCaptureSource(buf, kind="process", pid=os.getpid())
    src.start()
    time.sleep(1.5)
    running, err = src.is_running(), src.last_error()
    src.stop(); src.close(); buf.close()
    assert running and err is None, err
```

Run: `python -m pytest tests/unit/test_process_loopback.py -q` → FAIL.

- [ ] **Step 2: Implement**

`native_capture.py`: add

```python
import sys

def is_process_loopback_supported() -> bool:
    """Per-process WASAPI loopback needs Windows 10 build 19041 (20H1,
    May 2020) or newer — the same floor the ctypes port enforced."""
    if sys.platform != "win32":
        return False
    try:
        return sys.getwindowsversion().build >= 19041
    except Exception:
        return False
```

`audio_devices.py`: `process_loopback` branch → `return NativeCaptureSource(buffer=buffer, kind="process", pid=native.resolve_root_pid(pid), sample_rate=sample_rate, channels=channels)` (keep the `int(device.id)` ValueError; the resolve keeps the port's same-exe-ancestor behaviour at the same seam — construction time). `process_picker_dialog.py`: import `native.list_processes` and `native_capture.is_process_loopback_supported`; `self._all_rows = native.list_processes()`; `if not is_process_loopback_supported():`. `pyproject.toml`: drop `flashback_sampler/io/win32_process_loopback.py` and `flashback_sampler/core/loopback_capture.py` from `omit`. `flashback_sampler.spec`: delete the `"flashback_sampler.io.win32_process_loopback"` entry from `hiddenimports` (keep `"flashback_sampler.io"`) and its comment — PyInstaller hard-fails on a hiddenimport that no longer resolves. `flashback_sampler/io/__init__.py`: rewrite the docstring paragraph naming `win32_process_loopback`. `packaging/README.md` + `README.md`: fix the lines naming the module.

Sequester (same gitignored-`_ToRemove/` recipe as Task 7):

```bash
mkdir -p _ToRemove/flashback_sampler/io
mv flashback_sampler/io/win32_process_loopback.py _ToRemove/flashback_sampler/io/
git add -u flashback_sampler
```

Grep for stragglers: `grep -rn "win32_process_loopback\|enumerate_audio_processes\|ProcessLoopbackCapture\|resolve_audio_root_pid" flashback_sampler tests docs packaging soak_test.py flashback_sampler.spec *.md` — fix every live reference (historical docs — the spec, `PHASE2-HANDOFF.md` — stay as they are).

- [ ] **Step 3: Verify**

Run: `python -m pytest tests/unit -q -m "not audio_hw and not perf"` → green.
Hardware: `python -m pytest tests/hw -m audio_hw -s -q` → the process test passes. (`open()` already inits with `RoInitialize` — the apartment requirement is handled since Task 5.) If activation still fails, surface the `HRESULT` in `last_error`, comment it on `<B>`, and check it against the port's HRESULT constants (`win32_process_loopback.py:92` onward) before changing anything. Then the app: pick a playing process in the picker, arm, confirm frames.

- [ ] **Step 4: Commit + PR b**

```bash
git add flashback_sampler/app/audio_devices.py flashback_sampler/app/process_picker_dialog.py flashback_sampler/core/native_capture.py flashback_sampler/io/__init__.py flashback_sampler/platform/capabilities.py flashback_sampler.spec packaging/README.md README.md PLATFORM.md pyproject.toml tests/unit/test_process_loopback.py tests/hw
git add -u flashback_sampler
git commit -m "feat: per-process loopback and process list run on the Zig backend; COM port sequestered"
```

Push, open PR against `dev`: title `feat: per-process loopback on the Zig backend`, `Closes #<B>`, the `_ToRemove/` contents list, "Zig concepts in this PR" (implementing a COM object in Zig — a vtable we own; `LoadLibraryW`/`GetProcAddress` for an export that only exists on newer Windows; `extern union`; `@offsetOf`/`@sizeOf` layout tests as the ABI gate). Local gates green (state counts in the body; no CI fires). Hand over.

---

### Task 11: Flush cannot be undone by the writer (#20)

**Files:**
- Modify: `core/src/Ring.zig`, `core/src/Capture.zig`
- Branch: `git checkout -b feat/zig-flush-summary dev` after PR b merges.

**Interfaces:**
- Produces: `Ring` gains `writer_active: std.atomic.Value(bool)` and `flush_pending: std.atomic.Value(bool)`. `Ring.flush()` (control thread) becomes: if `writer_active` → set `flush_pending` and return; else the old immediate flush. `Ring.write()` checks `flush_pending` at entry; if set, performs the immediate flush then clears it, then writes. `Capture` stores `writer_active = true` before its loop and `false` after. `fb_ring_flush` unchanged; Python unchanged.

- [ ] **Step 1: Failing test**

Append to `core/src/Ring.zig`:

```zig
test "flush while a writer is active is deferred to the writer and cannot be undone" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 2.0 }); // capacity 16
    defer ring.deinit();
    ring.writer_active.store(true, .release);
    ring.write(&[_]f32{ 1, 1, 1, 1 });
    ring.flush(); // deferred: total_written must NOT drop yet
    try std.testing.expectEqual(@as(u64, 4), ring.total_written.load(.acquire));
    try std.testing.expect(ring.flush_pending.load(.acquire));
    ring.write(&[_]f32{ 2, 2 }); // the writer executes the flush, then writes
    try std.testing.expectEqual(@as(u64, 2), ring.total_written.load(.acquire));
    try std.testing.expect(!ring.flush_pending.load(.acquire));
    var out: [2]f32 = undefined;
    try ring.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 2, 2 }, &out);
}

test "flush with no active writer is immediate (unchanged behaviour)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 2.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 1 });
    ring.flush();
    try std.testing.expectEqual(@as(u64, 0), ring.total_written.load(.acquire));
    try std.testing.expect(!ring.flush_pending.load(.acquire));
}
```

Append to `core/src/Capture.zig`:

```zig
test "capture marks the ring's writer active for the life of the loop, and stop drains a pending flush" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{&[_]f32{ 1, 1 }});
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .input, .device_id = "", .rate = 48_000, .channels = 2 });
    try std.testing.expect(!ring.writer_active.load(.acquire));
    try cap.start();
    try waitUntil(&cap, struct { fn f(c: *Capture) bool { return c.frames_written.load(.acquire) == 1; } }.f);
    try std.testing.expect(ring.writer_active.load(.acquire));
    try std.testing.expectEqual(@as(u64, 1), ring.total_written.load(.acquire));
    // A flush that arrives while the loop is winding down must not be lost:
    // stop() drains it after the join, when the writer is inactive.
    ring.flush_pending.store(true, .release);
    cap.stop();
    try std.testing.expect(!ring.writer_active.load(.acquire));
    try std.testing.expectEqual(@as(u64, 0), ring.total_written.load(.acquire));
    try std.testing.expect(!ring.flush_pending.load(.acquire));
}
```

Run → compile error (fields missing).

- [ ] **Step 2: Implement**

`Ring.zig` fields (after `gain`): `writer_active: std.atomic.Value(bool), flush_pending: std.atomic.Value(bool),` initialised to `false` in `init`. Split flush:

```zig
/// Discard all buffered audio. If a writer is active (Capture sets
/// `writer_active`), the flush is handed to the writer, which performs
/// it before its next write — so a writer that already loaded
/// `total_written` can never republish over the reset (issue #20). With
/// no writer, it happens here, immediately.
pub fn flush(self: *Ring) void {
    if (self.writer_active.load(.acquire)) {
        self.flush_pending.store(true, .release);
        return;
    }
    self.flushNow();
}

fn flushNow(self: *Ring) void {
    self.summary.poison();
    self.total_written.store(0, .release);
    @memset(self.frames, 0);
}
```

At the top of `write`, before the gain load:

```zig
    if (self.flush_pending.load(.acquire)) {
        self.flushNow();
        self.flush_pending.store(false, .release);
    }
```

(The memset is ~50 ms for a 345 MB ring; the WASAPI buffer is 200 ms, so a flush mid-capture costs no frames — state this in the comment. `write` stays lock-free and allocation-free.)

Prose sweep — this task replaces a mechanism, so grep the prose that names the old one: the doc comment on `flush` loses its "known race … #20" paragraph, AND the inline comment INSIDE `flush` (`Ring.zig:124-130`) whose closing line says "same family as the … race **described above**, not fixed here" — its referent is the paragraph being deleted and its ordering rationale changes now that `flushNow` can run on the writer; rewrite both together. `Capture.run`: after `self.running.store(true, .release)` add `self.ring.writer_active.store(true, .release);` and a `defer self.ring.writer_active.store(false, .release);` placed so it runs before `running` flips false (declare it after the `running` defer). `Capture.stop`: after `t.join()`, drain a flush that arrived while the loop was winding down — `if (self.ring.flush_pending.load(.acquire)) { self.ring.flush_pending.store(false, .release); self.ring.flush(); }` (the writer is inactive now, so `flush` runs immediately; the Step 1 Capture test pins this drain).

- [ ] **Step 3: Run, verify green, count +3; run the stress tests 3×**

Run: `zig build --build-file core/build.zig test --summary all` ×3 (the existing "flush racing a concurrent writer" tests still pass — they never set `writer_active`, so they exercise the immediate path).

- [ ] **Step 4: Commit**

```bash
git add core/src/Ring.zig core/src/Capture.zig
git commit -m "fix(core): flush is executed by the active writer, never undone by it (#20)"
```

---

### Task 12: `Summary` seqlock (#23)

**Files:**
- Modify: `core/src/Summary.zig`

**Interfaces:**
- Produces: `Summary` gains `gen: std.atomic.Value(u64)`. `update` and `poison` do `gen += 1` (release) before mutating and `gen += 1` (release) after — odd = mid-write. `rmsBins` snapshots `gen` (acquire), computes into its stack scratch, re-reads `gen`; if odd or changed, retries up to 3 times, then returns the last computation (bounded — a reader never blocks the writer or itself). No locks.

- [ ] **Step 1: Failing test**

Append to `core/src/Summary.zig`:

```zig
test "update and poison bump the generation twice; rmsBins re-reads until it sees a stable even generation" {
    var s = try Summary.init(std.testing.allocator, 4096 * 4, 4096, 1);
    defer s.deinit();
    // init itself calls poison() (Summary.zig:50), so a fresh Summary
    // already sits at generation 2 — not 0.
    try std.testing.expectEqual(@as(u64, 2), s.gen.load(.acquire));
    const block = [_]f32{0.5} ** 4096;
    s.update(&block, 1.0, 0);
    try std.testing.expectEqual(@as(u64, 4), s.gen.load(.acquire));
    s.poison();
    try std.testing.expectEqual(@as(u64, 6), s.gen.load(.acquire));
    // A torn snapshot: force gen odd, rmsBins must not spin forever and must return.
    s.gen.store(7, .release);
    var out: [1]f32 = undefined;
    s.rmsBins(4096, 0, 0, &out);
    s.gen.store(8, .release);
}

test "rmsBins result equals the value read after the writer finished (no torn read across a full slot rewrite)" {
    var s = try Summary.init(std.testing.allocator, 4096 * 2, 4096, 1);
    defer s.deinit();
    const a = [_]f32{0.5} ** 4096;
    s.update(&a, 1.0, 0);
    var out: [1]f32 = undefined;
    s.rmsBins(4096, 4096, 4096, &out);
    try std.testing.expectApproxEqAbs(@as(f32, 0.5), out[0], 1e-4);
}
```

Run → compile error (`gen` missing).

- [ ] **Step 2: Implement**

Add the field `gen: std.atomic.Value(u64)` (init 0). Wrap the bodies:

```zig
pub fn update(self: *Summary, interleaved: []const f32, gain: f32, start_abs: u64) void {
    // Seqlock: odd gen = "being written". Readers (rmsBins, on the UI
    // thread) snapshot gen before and after; a mismatch or an odd value
    // means retry. The writer never waits — same discipline as Ring.
    _ = self.gen.fetchAdd(1, .release);
    defer _ = self.gen.fetchAdd(1, .release);
    ... existing body ...
}

pub fn poison(self: *Summary) void {
    _ = self.gen.fetchAdd(1, .release);
    defer _ = self.gen.fetchAdd(1, .release);
    @memset(self.slot_abs, -1);
}

pub fn rmsBins(self: *const Summary, total_written: u64, n_samples_req: u64, bin_span_frames: u64, out: []f32) void {
    var attempt: u8 = 0;
    while (true) : (attempt += 1) {
        const g0 = self.gen.load(.acquire);
        self.rmsBinsOnce(total_written, n_samples_req, bin_span_frames, out);
        const g1 = self.gen.load(.acquire);
        if ((g0 & 1) == 0 and g0 == g1) return;
        if (attempt >= 3) return; // bounded: hand back the best effort
    }
}

fn rmsBinsOnce(... the existing rmsBins body, renamed ...) void
```

Prose sweep: the "no synchronization" paragraph lives in the **`rmsBins` function doc comment** (`Summary.zig:127-136` — "…this pair has no synchronization at all … See issue #23"), not the module comment; rewrite that THREADING paragraph to describe the seqlock and drop the #23 reference. The module doc comment (`Summary.zig:1-7`) stays. Then grep `core/src` for any other "no synchronization"/"#23" prose (the phase-1 "replacing a mechanism means grepping the prose" rule).

- [ ] **Step 3: Run, verify green, count +2; the existing rmsBins tests still pass**

Run: `zig build --build-file core/build.zig test --summary all`.

- [ ] **Step 4: Commit**

```bash
git add core/src/Summary.zig
git commit -m "fix(core): Summary is a seqlock — rmsBins never reads a half-written slot (#23)"
```

---

### Task 13: PR c hand-off

- [ ] **Step 1: Verify everything**

Run: `zig fmt --check core/src`; `zig build --build-file core/build.zig test --summary all` (count = post-PR-b count + 5); `zig build --build-file core/build.zig -Doptimize=ReleaseSafe`; `python -m pytest tests/unit -q -m "not audio_hw and not perf"`. Run the app: arm a loopback slot, press flush while audio plays, confirm the waveform clears and then refills, no error.

- [ ] **Step 2: Docs**

`ZIG-101.md` is untracked owner notes — leave it, but comment on `<C>` that its flush/summary write-ups are now stale in at least three places (§3.1 flush, §3.5, and the §4 "Accepted compromises" entries for #20 and #23) and list the headings you actually find — grep it for `#20` and `#23` rather than trusting this count. `README.md`: nothing.

- [ ] **Step 3: PR**

Push `feat/zig-flush-summary`; PR against `dev`, title `fix(core): flush executes on the writer; Summary seqlock`, body: `Closes #<C>`, `Closes #20`, `Closes #23`, "Zig concepts in this PR" (`defer` for paired atomics; seqlock generations; why the memset can live on the writer thread here and not in a callback), findings statement. Local gates green (state counts; no CI fires). Hand over. Tick a, b, c on epic #17 as they merge.

---

## Verification (whole part)

- `zig build test` count rose by 28 (PR a: 7+3+6+5+4+3) + 3 (PR b) + 5 (PR c) over Task 0's baseline (44 → 80) — report the actual numbers in each PR body.
- Python: 524, minus the 9 deleted in Task 7 and the ctypes-layout tests deleted in Task 10, plus 8 (Task 6) + 6 (Task 7) + 6 (Task 10) + 1 (capture_source); report the actual.
- Soak: Task 0 "before" and Task 8 "after" tables on epic #17; #26 commented with the comparison.
- Hardware: `tests/hw` green on the owner's Windows box before each merge (loopback, input, 96k-honesty, process).
- Cross-compile builds (`x86_64-linux-gnu`, `aarch64-macos`) green locally before every push — no CI runs until the owner promotes `dev` → `main`.
- `_ToRemove/` contents listed in PR a and PR b bodies for the owner's one-shot deletion approval: `core/capture.py`, `core/loopback_capture.py`, `tests/test_loopback_soundcard.py`, `io/win32_process_loopback.py`. (`_ToRemove/` is gitignored — the PR diffs show plain deletions; the local copies are the review artifact.)
- The next plan (PRs d–f: Mixer, Playback + output enumeration, delete Python buffer + deps + FLAC) is written only after PR a is merged and `Backend.zig` is on `dev`.
