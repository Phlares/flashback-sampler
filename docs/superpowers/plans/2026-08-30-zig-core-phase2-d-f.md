# Zig Core Phase 2 — Part 2 (mixer, playback, Python buffer removal) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After PR f, Zig owns every audio thread and every per-sample loop; Python creates handles, starts and stops them, and reads numbers. Covers spec PRs **d** (`Mixer.zig`, control-thread-owned `writer_active`, #41 rider), **e** (`Playback.zig`, render backend, output enumeration), **f** (peak bins in Zig, Python buffer + `sounddevice`/`soundcard`/`soundfile` + FLAC deleted, final soak).

**Architecture:** `Mixer.zig` runs N `Capture`s into N staging `Ring`s and one mixer thread that sums into the target `Ring`. `Backend.zig` gains a `RenderStream` vtable (event-driven `wait`/`available`/`write`) that `Playback.zig` drives from its own thread; `WasapiBackend.openRender` implements it over `IAudioRenderClient`. `Ring.peakBins` joins `summaryBins` as the second display downsampler. `native.py` shrinks to ctypes declarations and one-line calls.

**Tech Stack:** Zig 0.16.0 (pinned; zero external deps; Windows COM via `extern` + vtable structs), ctypes, numpy, pytest, GitHub Actions (main-only).

**Spec:** `docs/superpowers/specs/2026-08-30-zig-core-phase2-d-f-design.md` — read it first. Parent: `2026-08-16-zig-core-phase2-design.md`. The part-2 spec wins where they differ.

## Global Constraints

All constraints of the part-1 plan (`docs/superpowers/plans/2026-08-16-zig-core-phase2-capture.md`, "Global Constraints") apply verbatim, including its Windows constants table. Restated and extended:

- **Zero external Zig dependencies.** `core/build.zig.zon` never gains a `.dependencies` entry.
- **Zig 0.16.0 pinned** (`core/build.zig.zon`, all CI `mlugg/setup-zig` `version:` fields). Pre-1.0 std drift is expected: if a std call does not resolve, fix the call site to the pinned API and keep the design. Known: no `std.Thread.sleep`; `refAllDecls` is one level deep.
- **Python will disappear.** Every line of logic lives in Zig. `native.py` keeps ctypes declarations and one-line calls or unit conversions. No maths, loops, or fallbacks in Python.
- **RT-safety invariant:** `Ring.write`, the capture loop, the mixer loop, and the render loop never lock, allocate, or fail. Allocation only at `init`/`bind` on the control thread.
- **Control-thread ownership:** the scope that calls `Thread.spawn` calls `join` and owns `ring.writer_active` across both. Worker threads never write it.
- **Portability:** `Mixer` and `Playback` import `Backend.zig` only, never `wasapi.zig`. `wasapi.zig`/`WasapiBackend.zig` stay behind `builtin.os.tag == .windows`; the cross-compile legs (`x86_64-windows`, `aarch64-macos`, `x86_64-linux-gnu`) must build.
- **Idiomatic Zig:** file-as-struct, caller-supplied allocators, error sets internally / status codes at the ABI, `*anyopaque` + vtable interfaces, fixed arrays on the audio path, no speculative comptime. Instructional comments where a Zig concept first load-bears; each PR carries a "Zig concepts in this PR" section.
- **TDD + mutation-check:** every test seen red before green; compound guards get one mutation per clause; verify by edit-then-revert on the real source. **The Zig gate is "the count rose"** (`zig build --build-file core/build.zig test --summary all`; 83 at the start of this plan).
- **Shipped optimize mode is ReleaseSafe.** Zig tests run in Debug.
- **PRs → `dev`**, one per PR group; the app works at every merge; owner merges. **No CI on feature branches, `dev`, or PRs.** Local gate before every push: `python -m pytest tests/unit -q -m "not audio_hw and not perf"` + `zig build --build-file core/build.zig test --summary all` + the three cross-compile builds + `zig fmt --check core/src`.
- **Deletion policy:** sequester to `_ToRemove/` (gitignored — moves stage nothing; stage the deletion explicitly), never `rm -rf`; one approval prompt at the end of each PR.
- **Execute in the primary checkout, not a worktree** (`soak_test.py`, `ZIG-101.md` untracked; `CLAUDE.md` gitignored — restate load-bearing rules in dispatches).
- **Shell on this machine:** no `cd` compounds, no `$( )`, no `&&`. Always `--build-file core/build.zig`.
- **No new pip dependencies.** Three are removed in PR f.
- **Issues are status truth.** Sub-issue per PR under epic #17; comment when something material is learned; `Closes #NN` in the PR body; tick the epic box on merge.
- **Hardware tests** (`tests/hw`, `audio_hw` marker) need the owner at the machine with audio playing; the loopback test reads 0 frames on a silent source.

**Task → PR map:** see the three PR sections below. PR **d** `feat/zig-mixer` · PR **e** `feat/zig-playback` · PR **f** `feat/zig-buffer-only`. Each PR's tasks assume the previous PR merged into `dev`.

---
## PR d — `Mixer.zig`, `writer_active` ownership, #41 rider

**Branch:** `feat/zig-mixer` from `dev`. **Target:** `dev`. **Spec section:** "PR d" in `docs/superpowers/specs/2026-08-30-zig-core-phase2-d-f-design.md:63-152`. The Global Constraints of `docs/superpowers/plans/2026-08-16-zig-core-phase2-capture.md:17-29` apply verbatim (Zig 0.16 pinned, zero deps, RT-safety, `--build-file` on every zig call, no `cd`/`&&`/`$( )`, sequester never delete, PRs → `dev`, local gates only).

**Baseline verified 2026-08-30 on `docs/phase2-d-f-spec` (= `dev` + the spec commit):** `zig build --build-file core/build.zig test --summary all` reports `83/83 tests passed`. Every "count" below is relative to 83.

**Plan choices recorded up front** (the spec is silent or the code disagrees; each is restated at its task):

| # | Choice | Why |
|---|---|---|
| P1 | `stop()` clears `writer_active` BEFORE draining the pending flush (spec text says drain, then clear). | After the join no writer exists. Clearing first means a flush that lands between the two steps takes `Ring.flush`'s immediate path (`Ring.zig:134-140`). Drain-then-clear leaves a window where a flush is deferred with no writer left to drain it. Both calls come from the control thread; `drainPendingFlush` is `pub` (`Ring.zig:179`). Record as a one-line spec edit in the hand-off. |
| P2 | The start-window test parks `FakeBackend` in `open()`, not the first `next()`. | The old scheme stored `writer_active` right after `open` (`Capture.zig:127`), so a park inside `next` never exercised the window. `open` is the earliest point a worker can be held. |
| P3 | `Mixer.init` is in-place (`init(self: *Mixer, ...)`), never returns `Mixer` by value. | Each `Capture` stores `*Ring` to its staging ring inside `Mixer.sources` (`Capture.zig:15`, `Capture.zig:38-56`). A by-value return would move the rings out from under those pointers. |
| P4 | Tick sleep = `std.Io.sleep(io, .fromMilliseconds(tick_ms), .awake)` on `std.Io.Threaded.global_single_threaded.io()`. | There is no `std.Thread.sleep` in 0.16 (Global Constraints). `WasapiBackend.zig:249,309` sleep with kernel32 `Sleep` from `wasapi.zig:91`, which `Mixer` may not import (portability rule, spec "Standing rules"). `abi.zig:57` already holds this Io singleton. Verified in the pinned std: `Io.sleep(io, Duration, Clock)` at `lib/std/Io.zig:2397`, `Duration.fromMilliseconds` at `Io.zig:982`, `Clock.awake` at `Io.zig:~756`, the Threaded vtable's `.sleep` at `lib/std/Io/Threaded.zig:1911` (a real OS wait, `Threaded.zig:11575-11582`). |
| P5 | Per-source packet scripts in tests come from a `FakeBackend` router (`children`), so `Mixer.init` keeps the spec's single-`backend` signature. | The fake's stream state (`delivered`, `stopped`) is per instance (`FakeBackend.zig:15-17`); two captures on one fake would share a packet cursor. |
| P6 | `NativeMixedSource` lives in `flashback_sampler/core/native_capture.py`, sharing a `_NativeSource` base with `NativeCaptureSource`; `core/mixed_capture.py` is sequestered whole. | The two wrappers differ only by the `fb_capture_`/`fb_mixer_` prefix. One mechanism. |
| P7 | The multi-spec build goes through a new `audio_devices.build_mixed_capture_source(devices, buffer, sample_rate, channels)`; both builders share `_spec_kwargs(device)`. | `CaptureDevice` → spec mapping (`audio_devices.py:181-213`) must not be duplicated, and `core/` must not import `app/`. |
| P8 | `MemoryError` text carries `int(seconds * rate) * channels * 4` bytes (the readable window's payload). | Python does not know `storage_frames` before the create succeeds. |
| P9 | Rider fix in Task 1: `Capture.start` resets `err_len` (`Capture.zig:67`) but not `err_buf[0]`, so `lastError()`'s `err_buf[0..0 :0]` (`Capture.zig:98`) trips the sentinel safety check on a restart after a recorded error (Debug and ReleaseSafe). | Found while designing `Mixer.lastError`; the mixer must not copy it. One line + one test. |

**Task → count map:** T1 +3 = 86 · T2 +4 = 90 · T3 +5 = 95 · T4 +5 = 100. The hand-off states the final number.

---

### Task 0: Branch, sub-issue

- [ ] **Step 1: Branch**

```bash
git checkout dev
git pull
git checkout -b feat/zig-mixer
```

- [ ] **Step 2: Open the sub-issue (write-at-the-moment rule)**

```bash
gh issue create --title "d — Mixer.zig, control-thread writer_active, #41 engine rider" --body "Sub-issue of #17 (PR d). Spec: docs/superpowers/specs/2026-08-30-zig-core-phase2-d-f-design.md, section 'PR d'.

- Mixer.zig: N Capture -> N staging Ring -> one mixer thread -> target Ring. Allocation in init only.
- writer_active becomes control-thread-owned (start stores true before spawn, stop stores false after join) for Capture and Mixer. Closes the start-window race.
- ABI: fb_mixer_*; FbStatus.out_of_memory = 5; fb_ring_create gains a nullable status out-param (engine half of #41; the arm-time UI message stays with #16).
- Python: NativeMixedSource handle wrapper; core/mixed_capture.py sequestered; build_capture_for_slot passes specs, not factories.
- Refs #41."
```

Record the number as `#D` — every later step that says `#D` means this issue. Then link it from the epic:

```bash
gh issue view 17 --json body -q .body > C:/Users/Ryon/AppData/Local/Temp/claude/epic17.md
```

Edit that file: `- [ ] d — Mixer` → `- [ ] d — #D Mixer.zig + control-thread writer_active + #41 rider`. Then `gh issue edit 17 --body-file C:/Users/Ryon/AppData/Local/Temp/claude/epic17.md`.

---

### Task 1: `writer_active` is owned by the control thread (Capture), FakeBackend `hold` knob, #P9 rider

**Files:**
- Modify: `core/src/FakeBackend.zig`, `core/src/Capture.zig`, `core/src/Ring.zig`

**Interfaces:**
- Produces (FakeBackend):
  ```zig
  pub const Hold = union(enum) { none, open, packet: usize };
  hold: Hold = .none,                       // where the worker parks until `release`
  release: std.atomic.Value(bool),          // test stores true to let it go
  ```
  `open()` parks while `hold == .open`; `next()` parks before delivering packet `k` while `hold == .{ .packet = k }`. Both waits are bounded (1_000_000 yields, like `FakeBackend.zig:56-57`).
- Produces (Capture): `start()` stores `ring.writer_active = true` before `Thread.spawn` and clears it on a spawn failure; `stop()` after `join` stores `false` then calls `ring.drainPendingFlush()`; `run` no longer touches the flag. `start()` also zeroes `err_buf[0]` (P9).
- Consumes: `Ring.writer_active`, `Ring.flush_pending`, `Ring.drainPendingFlush` (`Ring.zig:34-35,179-184`).

- [ ] **Step 1: Failing tests**

Append to `core/src/FakeBackend.zig`:

```zig
test "hold = .open parks open() until release" {
    var fake = FakeBackend.init(&.{});
    fake.hold = .open;
    const t = try std.Thread.spawn(.{}, struct {
        fn f(fb: *FakeBackend) void {
            _ = fb.backend().open(.{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 }) catch {};
        }
    }.f, .{&fake});
    // A wide window: the spawned thread has had every chance to open.
    var spins: u32 = 0;
    while (spins < 1000) : (spins += 1) std.Thread.yield() catch {};
    try std.testing.expect(!fake.opened.load(.acquire));
    fake.release.store(true, .release);
    t.join();
    try std.testing.expect(fake.opened.load(.acquire));
}
```

Append to `core/src/Capture.zig`:

```zig
test "a flush between start() and the worker's first stream call is deferred, not executed" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 9, 9 }); // audio the flush must not drop while the worker is still parked
    var fake = FakeBackend.init(&.{&[_]f32{ 1, 1 }});
    fake.hold = .open; // the worker parks inside backend.open until released
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    // Probe from the control thread while the worker has no stream yet.
    try std.testing.expect(ring.writer_active.load(.acquire));
    ring.flush();
    try std.testing.expect(ring.flush_pending.load(.acquire)); // deferred to the writer...
    try std.testing.expectEqual(@as(u64, 1), ring.total_written.load(.acquire)); // ...so nothing was reset yet
    fake.release.store(true, .release);
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.frames_written.load(.acquire) == 1;
        }
    }.f);
    cap.stop();
    // The worker drained the flush at its loop top, then wrote its packet.
    try std.testing.expect(!ring.flush_pending.load(.acquire));
    try std.testing.expectEqual(@as(u64, 1), ring.total_written.load(.acquire));
    var out: [2]f32 = undefined;
    try ring.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 1 }, &out);
    try std.testing.expect(!ring.writer_active.load(.acquire));
}

test "restart after a recorded error: lastError is empty and does not trap on the sentinel" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{});
    fake.open_error = error.DeviceNotFound;
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .input, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.err_len.load(.acquire) > 0;
        }
    }.f);
    cap.stop();
    fake.open_error = null;
    try cap.start();
    defer cap.stop();
    // Before the fix this line panics: err_len is 0 but err_buf[0] is 'o'.
    try std.testing.expectEqualStrings("", cap.lastError());
}
```

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: compile error in FakeBackend.zig (`hold`/`release` missing). Comment the FakeBackend test out for a moment and rerun: the start-window test fails at `expect(ring.flush_pending...)` (the flush was immediate; `writer_active` is not yet set when the worker is parked in `open`) and the restart test panics with a sentinel mismatch. Restore the test.

- [ ] **Step 3: Implement the FakeBackend knob**

In `core/src/FakeBackend.zig`, after `last_spec: ?Backend.Spec = null,` (line 18) add:

```zig
/// Where the worker parks until `release` is stored true. Lets a test
/// hold the capture thread at a known point — before its stream exists
/// (`.open`) or before a given packet (`.packet = k`) — and probe the
/// control-thread state from outside. Set before start(); the spawn
/// publishes it to the worker.
pub const Hold = union(enum) { none, open, packet: usize };
hold: Hold = .none,
release: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),

fn waitRelease(self: *FakeBackend) void {
    // Bounded, like the exhausted-wait in next(): a test that forgets
    // `release` fails instead of hanging.
    var spins: u32 = 0;
    while (!self.release.load(.acquire) and spins < 1_000_000) : (spins += 1) std.Thread.yield() catch {};
}
```

In `open` (line 38-44), after the `open_error` check and before `self.last_spec = spec;`:

```zig
    if (self.hold == .open) self.waitRelease();
```

In `next` (line 46-59), after `const i = self.delivered.load(.acquire);`:

```zig
    switch (self.hold) {
        .packet => |k| if (k == i) self.waitRelease(),
        else => {},
    }
```

- [ ] **Step 4: Implement the Capture ownership change**

Replace `start` and `stop` (`Capture.zig:64-85`):

```zig
pub fn start(self: *Capture) !void {
    if (self.thread != null) return error.AlreadyRunning;
    self.stop_flag.store(false, .monotonic);
    // Clear the TEXT too, not only the length: lastError() slices
    // err_buf[0..len :0], and the sentinel check needs err_buf[len] == 0.
    self.err_buf[0] = 0;
    self.err_len.store(0, .monotonic);
    // `writer_active` is owned by THIS thread, not the worker: the scope
    // that spawns is the scope that joins, so it holds the flag across
    // both. Stored BEFORE the spawn so a flush that lands while the
    // worker is still opening its stream is deferred to the loop top
    // (Ring.flush), never executed under a writer about to appear.
    self.ring.writer_active.store(true, .release);
    errdefer self.ring.writer_active.store(false, .release);
    // std.Thread.spawn takes the function and a tuple of its arguments.
    self.thread = try std.Thread.spawn(.{}, run, .{self});
}

pub fn stop(self: *Capture) void {
    const t = self.thread orelse return;
    self.stop_flag.store(true, .release);
    t.join();
    self.thread = null;
    // Joined: no writer exists. Clear the flag FIRST so a flush landing
    // from here on takes Ring.flush's immediate path, then drain the
    // one that may have been deferred while the loop wound down — on
    // this thread, now the only one touching the ring (issue #20).
    self.ring.writer_active.store(false, .release);
    self.ring.drainPendingFlush();
}
```

In `run` (`Capture.zig:107-147`) delete lines 115-128: the comment block that starts `// Tells Ring.flush() to defer to us`, the line `self.ring.writer_active.store(true, .release);`, and `defer self.ring.writer_active.store(false, .release);`. Keep `defer self.running.store(false, .release);`, `defer stream.deinit();`, `defer stream.stop();` in that order. The loop body's `drainPendingFlush` comment (`Capture.zig:132-136`) stays true.

- [ ] **Step 5: Rewrite the stale prose in `Ring.zig`**

Field comment (`Ring.zig:34`):

```zig
writer_active: std.atomic.Value(bool), // owned by the control thread that starts/stops the writer thread; tells flush() whether to defer
```

Replace the doc comment on `flush` (`Ring.zig:117-133`) with:

```zig
/// Discard all buffered audio. If a writer is registered
/// (`writer_active`), the flush is handed to the writer thread, which
/// performs it before its next write (`drainPendingFlush`) — so a
/// writer that already loaded `total_written` can never republish over
/// the reset (issue #20). With no writer, it happens here, immediately.
/// Called from a control thread, never the audio thread.
///
/// OWNERSHIP: `writer_active` belongs to the CONTROL thread that owns
/// the writer thread — `Capture.start`/`stop`, and `Mixer.start`/`stop`
/// for the mixed-slot target. Stored true BEFORE the spawn, false AFTER
/// the join; the writer thread never touches it. Storing before the
/// spawn closes the start window: a flush that lands while the worker
/// is still opening its stream is deferred to the loop top, not
/// executed under a writer about to appear. Every writer of a ring
/// registers this way; a host that wrote through `fb_ring_write` without
/// it would race both this reset and `Summary.gen` (issue #23) — no such
/// host remains once the mixer runs in Zig.
```

(`Mixer.zig` lands in Task 2 of this PR; the comment is true at the PR's merge.)

- [ ] **Step 6: Run, verify green, count +3; three times**

Run: `zig build --build-file core/build.zig test --summary all` ×3.
Expected: `86/86 tests passed` each time. `zig fmt --check core/src` clean.

- [ ] **Step 7: Mutation check**

1. Delete `self.ring.writer_active.store(true, .release);` in `start` → the start-window test goes red at `expect(ring.flush_pending...)`. Revert.
2. Delete `self.ring.drainPendingFlush();` in `stop` → "capture marks the ring's writer active…" (`Capture.zig:239`) goes red (total_written stays 1). Revert.
3. Delete `self.err_buf[0] = 0;` → the restart test panics. Revert.
4. Delete `if (self.hold == .open) self.waitRelease();` → the FakeBackend hold test goes red at `expect(!fake.opened...)`. Revert.

- [ ] **Step 8: Commit**

```bash
git add core/src/FakeBackend.zig core/src/Capture.zig core/src/Ring.zig
git commit -m "fix(core): writer_active is owned by the control thread; FakeBackend hold knob; lastError sentinel on restart"
```

---

### Task 2: `Mixer.zig` — init, common span, sum and clip; FakeBackend router; `root.zig`

**Files:**
- Create: `core/src/Mixer.zig`
- Modify: `core/src/FakeBackend.zig`, `core/src/root.zig`

**Interfaces:**
- Produces (`Mixer.zig`):
  ```zig
  pub const Mixer = @This();
  pub const max_sources = 8;
  pub const stage_seconds = 2.0;
  pub const tick_ms = 10;
  pub const max_error = Capture.max_error;
  pub fn init(self: *Mixer, allocator: std.mem.Allocator, backend: Backend.Backend, target: *Ring, specs: []const Backend.Spec) !void
      // error.InvalidArgument for specs.len == 0 or > max_sources; Ring.init errors (OutOfMemory)
  pub fn deinit(self: *Mixer) void      // stops first; frees the staging rings
  pub fn start(self: *Mixer) !void      // error.AlreadyRunning; capture start errors; spawn errors
  pub fn stop(self: *Mixer) void        // idempotent; joins; stops every capture
  pub fn stats(self: *const Mixer) Capture.Stats
  pub fn lastError(self: *const Mixer) [:0]const u8
  ```
  Fields a test may read: `sources[i].capture`, `sources[i].stage`, `sources[i].cursor`, `n_sources`, `thread`, `running`, `frames_written`, `xruns`.
- Produces (FakeBackend): `children: []const *FakeBackend = &.{}` + `opens: std.atomic.Value(usize)`; when `children` is non-empty every `open()` is forwarded to `children[opens++]`.
- Consumes: `Ring.init/read/write/drainPendingFlush/max_write_frames/capacity/total_written/sample_rate/channels` (`Ring.zig:60,62,179,194,291`), `Capture.init/start/stop/stats/lastError/max_error` (`Capture.zig:11-13,38,64,72,87,96`), `Backend.Spec` (`Backend.zig:15`).

- [ ] **Step 1: Failing tests**

Append to `core/src/FakeBackend.zig`:

```zig
test "children route each open() to the next child in order" {
    var a = FakeBackend.init(&.{&[_]f32{ 1, 1 }});
    var b = FakeBackend.init(&.{&[_]f32{ 2, 2 }});
    var router = FakeBackend.init(&.{});
    router.children = &.{ &a, &b };
    const spec = Backend.Spec{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 };
    const s1 = try router.backend().open(spec);
    const s2 = try router.backend().open(spec);
    try std.testing.expect(a.opened.load(.acquire));
    try std.testing.expect(b.opened.load(.acquire));
    try std.testing.expect(!router.opened.load(.acquire));
    const p1 = (try s1.next(10)) orelse return error.Expected;
    const p2 = (try s2.next(10)) orelse return error.Expected;
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 1 }, p1.frames);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 2, 2 }, p2.frames);
    try std.testing.expectError(error.DeviceNotFound, router.backend().open(spec)); // a third open has no child
}
```

Create `core/src/Mixer.zig` with the header, imports, constants, and struct fields from Step 3 below, no function bodies, and these tests at the bottom:

```zig
fn waitUntil(m: *Mixer, comptime pred: fn (*Mixer) bool) !void {
    var spins: u32 = 0;
    while (!pred(m) and spins < 5_000_000) : (spins += 1) std.Thread.yield() catch {};
    if (!pred(m)) return error.Timeout;
}

const test_spec = Backend.Spec{ .kind = .loopback, .device_id = "", .rate = 100, .channels = 1 };

test "init rejects zero sources and more than max_sources; builds a 2 s stage per spec at the target's format" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var fake = FakeBackend.init(&.{});
    var m: Mixer = undefined;
    try std.testing.expectError(error.InvalidArgument, m.init(std.testing.allocator, fake.backend(), &target, &.{}));
    const too_many = [_]Backend.Spec{test_spec} ** (max_sources + 1);
    try std.testing.expectError(error.InvalidArgument, m.init(std.testing.allocator, fake.backend(), &target, &too_many));
    try m.init(std.testing.allocator, fake.backend(), &target, &.{ test_spec, test_spec });
    defer m.deinit();
    try std.testing.expectEqual(@as(u8, 2), m.n_sources);
    try std.testing.expectEqual(@as(u64, 200), m.sources[0].stage.capacity); // 2 s at 100 Hz
    try std.testing.expectEqual(@as(u16, 1), m.sources[1].stage.channels);
    try std.testing.expectEqual(@as(u64, 0), m.sources[1].cursor);
    try std.testing.expectEqual(@as(u8, 0), m.stats().running);
}

test "skewed sources: the target gets the common span, summed" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    // Dyadic values: every sum below is exact in f32.
    var a = FakeBackend.init(&.{ &[_]f32{ 0.125, 0.25, 0.375, 0.5 }, &[_]f32{ 0.625, 0.75, 0.875, 1.0 } }); // 8 frames
    var b = FakeBackend.init(&.{&[_]f32{ 0.0625, 0.0625, 0.0625, 0.0625, 0.0625 }}); // 5 frames
    var router = FakeBackend.init(&.{});
    router.children = &.{ &a, &b };
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, router.backend(), &target, &.{ test_spec, test_spec });
    defer m.deinit();
    try m.start();
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 5;
        }
    }.f);
    m.stop();
    // 5 is the common span: b never delivers a 6th frame, so a's last 3 stay unmixed.
    try std.testing.expectEqual(@as(u64, 5), target.total_written.load(.acquire));
    var out: [5]f32 = undefined;
    try target.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0.1875, 0.3125, 0.4375, 0.5625, 0.6875 }, &out);
    try std.testing.expectEqual(@as(u64, 5), m.sources[0].cursor);
    try std.testing.expectEqual(@as(u64, 5), m.sources[1].cursor);
}

test "the sum is clipped to [-1, 1]" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 2, .seconds = 1.0 });
    defer target.deinit();
    var a = FakeBackend.init(&.{&[_]f32{ 0.75, -0.75 }}); // one stereo frame
    var b = FakeBackend.init(&.{&[_]f32{ 0.5, -0.5 }});
    var router = FakeBackend.init(&.{});
    router.children = &.{ &a, &b };
    const stereo = Backend.Spec{ .kind = .loopback, .device_id = "", .rate = 100, .channels = 2 };
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, router.backend(), &target, &.{ stereo, stereo });
    defer m.deinit();
    try m.start();
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 1;
        }
    }.f);
    m.stop();
    var out: [2]f32 = undefined;
    try target.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1.0, -1.0 }, &out);
}
```

Add to `core/src/root.zig` after line 14 (`pub const Capture = ...`):

```zig
pub const Mixer = @import("Mixer.zig");
```

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: compile error (`children` missing; `Mixer.init`/`start`/… missing).

- [ ] **Step 3: Implement the router**

In `core/src/FakeBackend.zig`, after the `release` field add:

```zig
/// Per-source doubles for a multi-source owner (Mixer): when set, each
/// open() is forwarded to the next child in order of ARRIVAL, so every
/// capture thread gets its own packet script. Arrival order is whichever
/// thread opens first — tests using this must be symmetric under source
/// order (they are: a sum does not care which addend came first).
children: []const *FakeBackend = &.{},
opens: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),
```

At the top of `open` (`FakeBackend.zig:38`), right after the `self` cast:

```zig
    if (self.children.len > 0) {
        const k = self.opens.fetchAdd(1, .monotonic);
        if (k >= self.children.len) return error.DeviceNotFound;
        return open(self.children[k], spec);
    }
```

- [ ] **Step 4: Implement `Mixer.zig`**

The whole file (tests from Step 1 go below it):

```zig
//! N capture sources summed into one target Ring on a Zig-owned mixer
//! thread. Each source is a Capture writing its own small staging Ring;
//! every tick the mixer reads the span ALL stages have in common, sums
//! it, clips to [-1, 1], and writes the target. Python never sees a
//! frame or a staging ring: it creates, starts, stops, and polls stats().
//!
//! Allocation happens in init() only (the staging rings). The loop works
//! in the fixed `scratch`/`sum` arrays: no lock, no allocation, no error
//! path — the same RT rule Ring.write and Capture.run follow.
//!
//! SELF-REFERENTIAL: each Capture holds a pointer to its staging ring
//! inside `sources`, so a Mixer is initialised IN PLACE (`init` takes
//! `*Mixer`) and must never be moved afterwards. Hosts allocate the
//! struct first (the ABI: allocator.create; tests: a stack variable).
const std = @import("std");
const Ring = @import("Ring.zig");
const Backend = @import("Backend.zig");
const Capture = @import("Capture.zig");
const FakeBackend = @import("FakeBackend.zig");
const Mixer = @This();

pub const max_sources = 8;
pub const stage_seconds = 2.0;
pub const tick_ms = 10;
pub const max_error = Capture.max_error;

const Source = struct { capture: Capture, stage: Ring, cursor: u64 };

// The tick's sleep. Zig 0.16 has no std.Thread.sleep; blocking waits
// live under std.Io. This is the same single-threaded Io singleton
// abi.zig's wav mutex holds; its `sleep` is a real OS wait (see
// std/Io/Threaded.zig's vtable), not a spin. Mixer talks to Backend.zig
// only and never imports wasapi.zig, so kernel32 Sleep is not an option.
const io = std.Io.Threaded.global_single_threaded.io();

allocator: std.mem.Allocator,
target: *Ring, // host-owned; never freed here
sources: [max_sources]Source,
n_sources: u8,
// One source's read for one tick, and the running sum. Sized for the
// largest single publish Ring.write makes (max_write_frames) at the
// widest channel count Ring.init accepts (2).
scratch: [Ring.max_write_frames * 2]f32,
sum: [Ring.max_write_frames * 2]f32,
thread: ?std.Thread,
stop_flag: std.atomic.Value(bool),
running: std.atomic.Value(bool),
frames_written: std.atomic.Value(u64),
xruns: std.atomic.Value(u32),
err_buf: [max_error]u8,
err_len: std.atomic.Value(usize),

pub fn init(self: *Mixer, allocator: std.mem.Allocator, backend: Backend.Backend, target: *Ring, specs: []const Backend.Spec) !void {
    if (specs.len == 0 or specs.len > max_sources) return error.InvalidArgument;
    self.* = .{
        .allocator = allocator,
        .target = target,
        .sources = undefined,
        .n_sources = @intCast(specs.len),
        .scratch = undefined,
        .sum = undefined,
        .thread = null,
        .stop_flag = std.atomic.Value(bool).init(false),
        .running = std.atomic.Value(bool).init(false),
        .frames_written = std.atomic.Value(u64).init(0),
        .xruns = std.atomic.Value(u32).init(0),
        .err_buf = [_]u8{0} ** max_error,
        .err_len = std.atomic.Value(usize).init(0),
    };
    // errdefer with a progress counter: if Ring.init fails on source k,
    // only stages 0..k-1 exist and only those are freed.
    var built: usize = 0;
    errdefer for (self.sources[0..built]) |*s| s.stage.deinit();
    for (specs, 0..) |spec, i| {
        const s = &self.sources[i];
        s.stage = try Ring.init(allocator, .{ .sample_rate = target.sample_rate, .channels = target.channels, .seconds = stage_seconds });
        built += 1;
        // Capture copies spec.device_id into its own buffer; the caller's
        // slice may die after this call.
        s.capture = Capture.init(&s.stage, backend, spec);
        s.cursor = 0;
    }
}

pub fn deinit(self: *Mixer) void {
    self.stop();
    for (self.sources[0..self.n_sources]) |*s| s.stage.deinit();
    self.* = undefined; // poison, like Ring.deinit
}

pub fn start(self: *Mixer) !void {
    if (self.thread != null) return error.AlreadyRunning;
    self.stop_flag.store(false, .monotonic);
    self.err_buf[0] = 0;
    self.err_len.store(0, .monotonic);
    // Control-thread ownership of the target's writer flag (Ring.flush):
    // registered before any thread that could write the target exists,
    // cleared by the errdefer if anything below fails.
    self.target.writer_active.store(true, .release);
    errdefer self.target.writer_active.store(false, .release);
    // Same progress-counter unwind as init. errdefers run LIFO, so a
    // failure stops the started captures FIRST, then clears the flag.
    var started: usize = 0;
    errdefer for (self.sources[0..started]) |*s| s.capture.stop();
    for (self.sources[0..self.n_sources]) |*s| {
        s.capture.start() catch |e| {
            self.setError("source start failed: {s}", .{@errorName(e)});
            return e;
        };
        started += 1;
    }
    self.thread = std.Thread.spawn(.{}, run, .{self}) catch |e| {
        self.setError("spawn failed: {s}", .{@errorName(e)});
        return e;
    };
}

pub fn stop(self: *Mixer) void {
    const t = self.thread orelse return;
    self.stop_flag.store(true, .release);
    t.join();
    self.thread = null;
    for (self.sources[0..self.n_sources]) |*s| s.capture.stop();
    // Joined: no target writer exists. Clear first, then drain — same
    // order and reason as Capture.stop.
    self.target.writer_active.store(false, .release);
    self.target.drainPendingFlush();
}

pub fn stats(self: *const Mixer) Capture.Stats {
    var xruns = self.xruns.load(.acquire);
    for (self.sources[0..self.n_sources]) |*s| xruns += s.capture.stats().xruns;
    return .{
        .running = @intFromBool(self.running.load(.acquire)),
        .frames_written = self.frames_written.load(.acquire),
        .xruns = xruns,
        .mix_rate = self.sources[0].capture.stats().mix_rate,
    };
}

pub fn lastError(self: *const Mixer) [:0]const u8 {
    const n = self.err_len.load(.acquire);
    if (n > 0) return self.err_buf[0..n :0];
    for (self.sources[0..self.n_sources]) |*s| {
        const e = s.capture.lastError();
        if (e.len > 0) return e;
    }
    return self.err_buf[0..0 :0];
}

fn setError(self: *Mixer, comptime fmt: []const u8, args: anytype) void {
    const s = std.fmt.bufPrintZ(self.err_buf[0..], fmt, args) catch self.err_buf[0 .. max_error - 1 :0];
    self.err_len.store(s.len, .release);
}

fn run(self: *Mixer) void {
    self.running.store(true, .release);
    defer self.running.store(false, .release);
    const ch: u64 = self.target.channels;
    while (!self.stop_flag.load(.acquire)) {
        // The mixer is the target's registered writer, so a control-thread
        // flush is deferred to us; drain it before sleeping so a flush
        // never waits on the sources to produce (same rule as Capture.run).
        self.target.drainPendingFlush();
        std.Io.sleep(io, .fromMilliseconds(tick_ms), .awake) catch {};
        // Common span: the frames EVERY stage has that we have not consumed,
        // capped at one Ring.write publish per tick.
        var n: u64 = Ring.max_write_frames;
        for (self.sources[0..self.n_sources]) |*s| {
            const tw = s.stage.total_written.load(.acquire);
            var avail = tw - s.cursor; // stages are never flushed: tw only grows
            if (avail > s.stage.capacity) {
                // The stage lapped our cursor: we fell more than stage_seconds
                // behind. Resume at the oldest frame still readable.
                s.cursor = tw - s.stage.capacity;
                avail = s.stage.capacity;
                _ = self.xruns.fetchAdd(1, .monotonic);
            }
            n = @min(n, avail);
        }
        if (n == 0) continue;
        const floats: usize = @intCast(n * ch);
        @memset(self.sum[0..floats], 0);
        var complete = true;
        for (self.sources[0..self.n_sources]) |*s| {
            // Seqlock read: never blocks the capture thread. A failure means
            // the stage lapped us between the check above and this copy;
            // give up the tick — the next one re-derives every cursor.
            s.stage.read(s.cursor, self.scratch[0..floats]) catch {
                complete = false;
                break;
            };
            for (self.sum[0..floats], self.scratch[0..floats]) |*acc, x| acc.* += x;
        }
        if (!complete) continue;
        for (self.sum[0..floats]) |*x| x.* = std.math.clamp(x.*, -1.0, 1.0);
        self.target.write(self.sum[0..floats]);
        for (self.sources[0..self.n_sources]) |*s| s.cursor += n;
        _ = self.frames_written.fetchAdd(n, .release);
    }
}
```

Verify: (1) `[Ring.max_write_frames * 2]f32` as an array length — `max_write_frames` is a comptime-known `u64` (`Ring.zig:60`); `Ring.zig:409` already uses a `u64` expression as an array length, so this compiles. If it does not, write `[@as(usize, Ring.max_write_frames) * 2]f32`. (2) `std.math.clamp(x.*, -1.0, 1.0)` returns `@TypeOf(val, lower, upper)` (`lib/std/math.zig:520`), f32 with comptime floats; if peer resolution complains, use `@min(@max(x.*, -1.0), 1.0)`. (3) `.fromMilliseconds(tick_ms)` is a decl literal for `Io.Duration` (`Io.zig:982`); if the pinned std spells the parameter type differently, write `std.Io.Duration.fromMilliseconds(tick_ms)`. (4) `std.Io.sleep` on a thread other than the one that created the singleton: the Threaded `sleep` (`Threaded.zig:11575`) only reads the timeout and calls the OS; if it asserts on thread identity, replace with a bounded yield loop timed with `std.time.Timer` and record it here.

- [ ] **Step 5: Run, verify green, count +4; three times**

Run: `zig build --build-file core/build.zig test --summary all` ×3.
Expected: `90/90 tests passed` each time (83 + 3 from Task 1 + 1 router + 3 Mixer). If the count is 86, `root.zig` is missing the re-export — `refAllDecls` is one level deep (`root.zig:33-40`). `zig fmt --check core/src` clean.

- [ ] **Step 6: Mutation check**

1. `n = @min(n, avail)` → `n = @max(n, avail)`: the skew test times out (the read on `b` fails every tick). Revert.
2. Delete the `std.math.clamp` line: the clip test reads `1.25`. Revert.
3. `stage_seconds = 2.0` → `1.0`: the init test's `capacity == 200` goes red. Revert.
4. Delete `if (k >= self.children.len) return error.DeviceNotFound;`: the router test's third `open` panics on an out-of-bounds index in Debug instead of erroring. Revert.

- [ ] **Step 7: Commit**

```bash
git add core/src/Mixer.zig core/src/FakeBackend.zig core/src/root.zig
git commit -m "feat(core): Mixer.zig — N captures summed into one ring on a Zig mixer thread"
```

---

### Task 3: `Mixer.zig` — lapped cursor, flush during mixing, start unwind, `writer_active` window, stats

**Files:**
- Modify: `core/src/Mixer.zig` (tests only — Task 2's implementation already carries the behaviour; this task pins it)

**Interfaces:** none new. Consumes `FakeBackend.hold/release/children/discontinuity_at/mix_rate`.

- [ ] **Step 1: Failing tests**

Append to `core/src/Mixer.zig`:

```zig
test "a stage that laps the cursor counts one xrun and resumes at the oldest readable frame" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 10.0 });
    defer target.deinit();
    // 300 frames in ONE packet against a 200-frame stage (2 s at 100 Hz):
    // frames 0..99 are gone before the mixer's first tick can read them.
    var big: [300]f32 = undefined;
    for (&big, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i + 1)) / 1000.0;
    var src = FakeBackend.init(&.{&big});
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, src.backend(), &target, &.{test_spec});
    defer m.deinit();
    try m.start();
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 200;
        }
    }.f);
    m.stop();
    try std.testing.expectEqual(@as(u32, 1), m.stats().xruns);
    try std.testing.expectEqual(@as(u64, 300), m.sources[0].cursor); // 100 (oldest valid) + 200 read
    try std.testing.expectEqual(@as(u64, 200), target.total_written.load(.acquire));
    var out: [1]f32 = undefined;
    try target.read(0, &out);
    try std.testing.expectEqual(big[100], out[0]); // the target starts at stage frame 100, not 0
}

test "a flush during mixing is drained by the mixer even while the sources are idle; only post-flush frames remain" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var a = FakeBackend.init(&.{ &[_]f32{ 0.25, 0.25 }, &[_]f32{ 0.5, 0.5 } });
    var b = FakeBackend.init(&.{ &[_]f32{ 0.25, 0.25 }, &[_]f32{ 0.5, 0.5 } });
    a.hold = .{ .packet = 1 }; // both sources park before their second packet
    b.hold = .{ .packet = 1 };
    var router = FakeBackend.init(&.{});
    router.children = &.{ &a, &b };
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, router.backend(), &target, &.{ test_spec, test_spec });
    defer m.deinit();
    try m.start();
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 2;
        }
    }.f);
    try std.testing.expectEqual(@as(u64, 2), target.total_written.load(.acquire));
    target.flush(); // control thread: the mixer is the registered writer, so this is deferred to it
    // Drained at the loop top while no source delivers: a flush must never wait on audio.
    var spins: u32 = 0;
    while (target.flush_pending.load(.acquire) and spins < 5_000_000) : (spins += 1) std.Thread.yield() catch {};
    try std.testing.expect(!target.flush_pending.load(.acquire));
    try std.testing.expectEqual(@as(u64, 0), target.total_written.load(.acquire));
    a.release.store(true, .release);
    b.release.store(true, .release);
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 4;
        }
    }.f);
    m.stop();
    try std.testing.expectEqual(@as(u64, 2), target.total_written.load(.acquire)); // post-flush frames only
    var out: [2]f32 = undefined;
    try target.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1.0, 1.0 }, &out); // 0.5 + 0.5, the SECOND packets
}

test "start failure on source 2 unwinds source 1 and clears writer_active" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var fake = FakeBackend.init(&.{});
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, fake.backend(), &target, &.{ test_spec, test_spec });
    defer m.deinit();
    // Make source 2 already running: the mixer's own start hits AlreadyRunning
    // on it — a real failure through the real path, no fake needed.
    try m.sources[1].capture.start();
    defer m.sources[1].capture.stop(); // runs before m.deinit (defers are LIFO)
    try std.testing.expectError(error.AlreadyRunning, m.start());
    try std.testing.expect(m.sources[0].capture.thread == null); // stopped and joined by the unwind
    try std.testing.expect(m.thread == null);
    try std.testing.expect(!target.writer_active.load(.acquire));
    try std.testing.expectEqualStrings("source start failed: AlreadyRunning", m.lastError());
}

test "writer_active is true from start() through stop(), false after" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var fake = FakeBackend.init(&.{});
    fake.hold = .open;
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, fake.backend(), &target, &.{test_spec});
    defer m.deinit();
    try std.testing.expect(!target.writer_active.load(.acquire));
    try m.start();
    // Probed while the capture is still parked in open(): before any frame can exist.
    try std.testing.expect(target.writer_active.load(.acquire));
    fake.release.store(true, .release);
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.running.load(.acquire);
        }
    }.f);
    try std.testing.expect(target.writer_active.load(.acquire));
    m.stop();
    try std.testing.expect(!target.writer_active.load(.acquire));
    try std.testing.expectEqual(@as(u8, 0), m.stats().running);
    m.stop(); // idempotent
}

test "stats: xruns sums the captures' discontinuities, mix_rate is the first source's, lastError is the first non-empty" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var a = FakeBackend.init(&.{ &[_]f32{0}, &[_]f32{0} });
    a.discontinuity_at = 1;
    a.mix_rate = 44_100;
    var b = FakeBackend.init(&.{});
    b.open_error = error.FormatRejected;
    var router = FakeBackend.init(&.{});
    router.children = &.{ &a, &b };
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, router.backend(), &target, &.{ test_spec, test_spec });
    defer m.deinit();
    try m.start();
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.sources[0].capture.stats().xruns + x.sources[1].capture.stats().xruns == 1 and
                (x.sources[0].capture.lastError().len > 0 or x.sources[1].capture.lastError().len > 0);
        }
    }.f);
    m.stop();
    const st = m.stats();
    try std.testing.expectEqual(@as(u32, 1), st.xruns);
    // The router hands a/b out in ARRIVAL order, so which capture got
    // 44_100 is not fixed. Pin "the first source's" against source 0
    // itself, and that the value is one of the two scripted rates.
    try std.testing.expectEqual(m.sources[0].capture.stats().mix_rate, st.mix_rate);
    try std.testing.expect(st.mix_rate == 44_100 or st.mix_rate == 48_000);
    try std.testing.expectEqualStrings("open failed: FormatRejected", m.lastError());
}
```

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: all five compile and pass on the first run (the mechanism exists since Task 2). That is fine for this task: its value is the mutation pins in Step 3, each of which must go red.

- [ ] **Step 3: Mutation check (each must go red, then revert)**

1. `s.cursor = tw - s.stage.capacity;` → `s.cursor = tw;` — lapped test: target stays at 0 frames, timeout.
2. Delete `_ = self.xruns.fetchAdd(1, .monotonic);` — lapped test: `xruns == 0`.
3. Delete `self.target.drainPendingFlush();` at the loop top — flush test: `flush_pending` stays true while the sources are parked; red at `expect(!target.flush_pending...)`.
4. Delete `errdefer for (self.sources[0..started]) |*s| s.capture.stop();` — unwind test: `sources[0].capture.thread != null`.
5. Delete `errdefer self.target.writer_active.store(false, .release);` in `start` — unwind test: `writer_active` still true.
6. Delete `self.target.writer_active.store(true, .release);` in `start` — window test red at the first `expect(target.writer_active...)`; the flush test also changes shape (the flush is immediate).
7. In `stats`, drop the `for` that adds capture xruns — stats test: `xruns == 0`.
8. In `lastError`, return `self.err_buf[0..0 :0]` without consulting the captures — stats test: `""`.
9. In `stats`, read `self.sources[self.n_sources - 1]` instead of `sources[0]` for `mix_rate` — stats test red on the runs where the 44_100 fake landed on source 0 (run ×3).

- [ ] **Step 4: Run, verify green, count +5; three times**

Run: `zig build --build-file core/build.zig test --summary all` ×3.
Expected: `95/95 tests passed`.

- [ ] **Step 5: Commit**

```bash
git add core/src/Mixer.zig
git commit -m "test(core): Mixer — lapped cursor, flush while idle, start unwind, writer_active window, stats"
```

---

### Task 4: ABI — `fb_mixer_*`, `FbStatus.out_of_memory`, `fb_ring_create` status out-param; header; `native.py`

**Files:**
- Modify: `core/src/abi.zig`, `core/include/flashback_core.h`, `flashback_sampler/core/native.py`, `tests/unit/test_native_capture.py`

**Interfaces:**
- Produces (C ABI, mirrored in `native.py._declare`):
  ```c
  typedef struct FbMixer FbMixer; /* opaque */
  /* FbStatus gains */ FB_OUT_OF_MEMORY = 5
  FbRing  *fb_ring_create(uint32_t rate, uint16_t channels, double seconds, FbStatus *status /* nullable */);
  FbMixer *fb_mixer_create(FbRing *target, const FbCaptureSpec *specs, size_t n); /* NULL: n outside 1..8, a bad spec, no backend on this OS, or OOM */
  FbStatus fb_mixer_start(FbMixer *);        /* FB_INVALID_ARG if already running, FB_IO_ERROR otherwise */
  void     fb_mixer_stop(FbMixer *);
  void     fb_mixer_destroy(FbMixer *);      /* stops first */
  void     fb_mixer_stats(const FbMixer *, FbCaptureStats *out);
  const char *fb_mixer_last_error(const FbMixer *);
  ```
- Produces (Python): `native._OUT_OF_MEMORY = 5`, `native.MAX_MIXER_SOURCES = 8`, the seven declarations; `NativeAudioCircularBuffer.__init__` raises `MemoryError` (with the byte count, P8) on `out_of_memory` and `ValueError` on `invalid_arg`.
- Consumes: `Mixer` (Task 2), `Capture.Stats` (`Capture.zig:11`), `nativeBackend()` (`abi.zig:326-329`), `Ring.init`'s error set = `{InvalidArgument, OutOfMemory}` (`Ring.zig:66-67,81` and `Summary.init`'s five `try allocator.alloc` at `Summary.zig:29-37`).

- [ ] **Step 1: Failing Zig tests**

Append to `core/src/abi.zig`:

```zig
test "fb_ring_create: status is ok on success and invalid_arg on a rejected config" {
    var st: FbStatus = .io_error; // any value the call must overwrite
    const ring = fb_ring_create(8, 1, 1.0, &st) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    try std.testing.expectEqual(FbStatus.ok, st);
    st = .io_error;
    try std.testing.expectEqual(@as(?*Ring, null), fb_ring_create(8, 3, 1.0, &st));
    try std.testing.expectEqual(FbStatus.invalid_arg, st);
}

test "fb_ring_create: status is out_of_memory when the allocator fails (issue #41)" {
    // std.testing.failing_allocator fails its FIRST allocation (fail_index = 0).
    var st: FbStatus = .ok;
    try std.testing.expectEqual(@as(?*Ring, null), ringCreate(std.testing.failing_allocator, 48_000, 2, 1.0, &st));
    try std.testing.expectEqual(FbStatus.out_of_memory, st);
}

test "fb_ring_create: a null status pointer is accepted" {
    const ring = fb_ring_create(8, 1, 1.0, null) orelse return error.CreateFailed;
    fb_ring_destroy(ring);
}

test "fb_mixer_create rejects n == 0, n > max_sources, and a bad spec" {
    const ring = fb_ring_create(48_000, 2, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    const good = FbCaptureSpec{ .kind = 0, .pid = 0, .rate = 48_000, .channels = 2, .device_id = "" };
    const bad = FbCaptureSpec{ .kind = 9, .pid = 0, .rate = 48_000, .channels = 2, .device_id = "" };
    const nine = [_]FbCaptureSpec{good} ** (Mixer.max_sources + 1);
    try std.testing.expectEqual(@as(?*Mixer, null), fb_mixer_create(ring, &nine, 0));
    try std.testing.expectEqual(@as(?*Mixer, null), fb_mixer_create(ring, &nine, nine.len));
    const one_bad = [_]FbCaptureSpec{ good, bad };
    try std.testing.expectEqual(@as(?*Mixer, null), fb_mixer_create(ring, &one_bad, one_bad.len));
}

test "fb_mixer stats/last_error on a never-started mixer are zero/empty (Windows only)" {
    if (builtin.os.tag != .windows) return error.SkipZigTest;
    const ring = fb_ring_create(48_000, 2, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    const specs = [_]FbCaptureSpec{ .{ .kind = 0, .pid = 0, .rate = 48_000, .channels = 2, .device_id = "" }, .{ .kind = 1, .pid = 0, .rate = 48_000, .channels = 2, .device_id = "" } };
    const m = fb_mixer_create(ring, &specs, specs.len) orelse return error.CreateFailed;
    defer fb_mixer_destroy(m);
    var st: Capture.Stats = undefined;
    fb_mixer_stats(m, &st);
    try std.testing.expectEqual(@as(u8, 0), st.running);
    try std.testing.expectEqual(@as(u64, 0), st.frames_written);
    try std.testing.expectEqual(@as(u32, 0), st.xruns);
    try std.testing.expectEqualStrings("", std.mem.span(fb_mixer_last_error(m)));
}
```

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: compile error (`fb_ring_create` takes 3 args; `ringCreate`, `Mixer`, `fb_mixer_*` missing).

- [ ] **Step 3: Implement the Zig side**

Imports (`abi.zig:6-12`): add `const Mixer = @import("Mixer.zig");`.

`FbStatus` (`abi.zig:22-28`): add `out_of_memory = 5,` after `invalid_arg = 4,`.

Replace `fb_ring_create` (`abi.zig:173-189`) with:

```zig
/// The export's body, with the allocator as a parameter so a test can
/// hand in std.testing.failing_allocator. `status` is nullable: hosts
/// that only need the pointer pass NULL.
fn ringCreate(alloc: std.mem.Allocator, rate: u32, channels: u16, seconds: f64, status: ?*FbStatus) ?*Ring {
    const ring = alloc.create(Ring) catch {
        if (status) |s| s.* = .out_of_memory;
        return null;
    };
    ring.* = Ring.init(alloc, .{
        .sample_rate = rate,
        .channels = channels,
        .seconds = seconds,
    }) catch |e| {
        alloc.destroy(ring);
        // Ring.init's inferred error set is exactly these two: its own
        // InvalidArgument guard, and OutOfMemory from its allocations and
        // Summary.init's. A third member is a compile error here — on
        // purpose, so a new failure mode gets a status, not a guess.
        if (status) |s| s.* = switch (e) {
            error.InvalidArgument => .invalid_arg,
            error.OutOfMemory => .out_of_memory,
        };
        return null;
    };
    if (status) |s| s.* = .ok;
    return ring;
}

// The five-clause config guard lives in Ring.init (issue #21); this is
// a pass-through that also reports WHY a create failed (issue #41):
// invalid_arg for a rejected config, out_of_memory when the reservation
// cannot be made. A 345 MB ring that fails to allocate used to look
// exactly like channels == 3.
export fn fb_ring_create(rate: u32, channels: u16, seconds: f64, status: ?*FbStatus) ?*Ring {
    return ringCreate(allocator, rate, channels, seconds, status);
}
```

Update every existing `fb_ring_create(` call in `abi.zig`'s tests to pass `null` as the fourth argument. Thirteen sites at the pre-edit line numbers 61, 76, 83, 90, 99, 125, 129, 133, 137, 153, 157, 283, 291. Lines 153 and 157 contain a nested `std.math.nan(f64)` / `std.math.inf(f64)` — edit those by hand; for the rest this works:

```bash
sed -i '/export fn\|std.math.nan\|std.math.inf/! s/fb_ring_create(\([^)]*\))/fb_ring_create(\1, null)/' core/src/abi.zig
```

Then: `grep -n "fb_ring_create(" core/src/abi.zig` — every test call shows four arguments.

Replace `fb_capture_create` (`abi.zig:345-357`) and add the mixer exports after `fb_capture_last_error`:

```zig
/// One validation for both create paths. null = rejected spec.
fn specFromAbi(s: FbCaptureSpec) ?Backend.Spec {
    if (s.kind > 2 or s.channels == 0 or s.channels > 2 or s.rate == 0) return null;
    return .{
        .kind = @enumFromInt(s.kind),
        .device_id = std.mem.span(s.device_id),
        .pid = s.pid,
        .rate = s.rate,
        .channels = s.channels,
    };
}

export fn fb_capture_create(ring: *Ring, spec: *const FbCaptureSpec) ?*Capture {
    const zspec = specFromAbi(spec.*) orelse return null;
    const be = nativeBackend() orelse return null;
    const cap = allocator.create(Capture) catch return null;
    cap.* = Capture.init(ring, be, zspec);
    return cap;
}
```

```zig
export fn fb_mixer_create(target: *Ring, specs: [*]const FbCaptureSpec, n: usize) ?*Mixer {
    if (n == 0 or n > Mixer.max_sources) return null;
    // Converted on the stack: Mixer.init copies what it keeps (Capture
    // owns its device_id bytes), so these slices need not outlive the call.
    var zspecs: [Mixer.max_sources]Backend.Spec = undefined;
    for (specs[0..n], 0..) |s, i| zspecs[i] = specFromAbi(s) orelse return null;
    const be = nativeBackend() orelse return null;
    // Allocated BEFORE init and never moved: Mixer is self-referential
    // (see Mixer.zig's header).
    const m = allocator.create(Mixer) catch return null;
    m.init(allocator, be, target, zspecs[0..n]) catch {
        allocator.destroy(m);
        return null;
    };
    return m;
}

export fn fb_mixer_start(m: *Mixer) FbStatus {
    m.start() catch |e| return switch (e) {
        error.AlreadyRunning => .invalid_arg,
        else => .io_error,
    };
    return .ok;
}

export fn fb_mixer_stop(m: *Mixer) void {
    m.stop();
}

export fn fb_mixer_destroy(m: *Mixer) void {
    m.deinit(); // stops first
    allocator.destroy(m);
}

export fn fb_mixer_stats(m: *const Mixer, out: *Capture.Stats) void {
    out.* = m.stats();
}

export fn fb_mixer_last_error(m: *const Mixer) [*:0]const u8 {
    return m.lastError().ptr;
}
```

- [ ] **Step 4: Run Zig, verify green, count +5; rebuild the DLL**

Run: `zig build --build-file core/build.zig test --summary all` — `100/100 tests passed`. Then `zig build --build-file core/build.zig -Doptimize=ReleaseSafe` (the DLL Python loads, `native.py:50`).

Mutation check: in `ringCreate` swap the two switch arms → the `invalid_arg` test and the `out_of_memory` test both go red. Revert. In `fb_mixer_create` delete `if (n == 0 or n > Mixer.max_sources) return null;` → the rejects test goes red on `n == 0` (Windows: `Mixer.init` rejects, but the `n > 8` case indexes past `zspecs` — a Debug panic, which is the red). Revert.

- [ ] **Step 5: Header mirror**

`core/include/flashback_core.h`: in `FbStatus` (line 11-17) add `FB_OUT_OF_MEMORY = 5`; after `typedef struct FbCapture FbCapture;` (line 21) add `typedef struct FbMixer FbMixer; /* opaque */`; replace line 27 with

```c
/* status is nullable. FB_INVALID_ARG: rejected config. FB_OUT_OF_MEMORY:
 * the reservation could not be made (issue #41). */
FbRing *fb_ring_create(uint32_t rate, uint16_t channels, double seconds, FbStatus *status);
```

and after the `fb_capture_last_error` line (67) add:

```c
/* N sources (1..8) summed into `target` by a Zig mixer thread. Staging
 * rings live inside the mixer. NULL: n outside 1..8, a bad spec, no
 * backend on this OS, or out of memory. */
FbMixer   *fb_mixer_create(FbRing *target, const FbCaptureSpec *specs, size_t n);
FbStatus   fb_mixer_start(FbMixer *);                          /* FB_INVALID_ARG if already running, FB_IO_ERROR otherwise */
void       fb_mixer_stop(FbMixer *);
void       fb_mixer_destroy(FbMixer *);                        /* stops first */
void       fb_mixer_stats(const FbMixer *, FbCaptureStats *out);
const char*fb_mixer_last_error(const FbMixer *);               /* own message, else the first source's; "" when none */
```

- [ ] **Step 6: Failing Python tests**

In `tests/unit/test_native_capture.py`, extend `_FakeLib.__init__` (line 14-20) with `self.ring_status = 0` and add to `__getattr__._fn` before `return None`:

```python
            if name == "fb_ring_create":
                a[3]._obj.value = self.ring_status  # byref(status) -> the c_int
                return 0 if self.ring_status else 0xA11
```

Append:

```python
def test_ring_create_out_of_memory_raises_memory_error_with_the_byte_count(lib):
    lib.ring_status = native._OUT_OF_MEMORY
    with pytest.raises(MemoryError) as e:
        native.NativeAudioCircularBuffer(duration_seconds=2.0, sample_rate=1000, channels=2)
    assert "16,000 bytes" in str(e.value)  # 2 s * 1000 Hz * 2 ch * 4 B


def test_ring_create_invalid_arg_raises_value_error(lib):
    lib.ring_status = native._INVALID_ARG
    with pytest.raises(ValueError):
        native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=3)


def test_ring_create_passes_a_status_out_param(lib):
    lib.ring_status = native._OUT_OF_MEMORY
    with pytest.raises(MemoryError):
        native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    name, args = next(c for c in lib.calls if c[0] == "fb_ring_create")
    assert len(args) == 4 and hasattr(args[3], "_obj")
```

Run: `python -m pytest tests/unit/test_native_capture.py -q`
Expected: the three new tests fail (`fb_ring_create` is called with 3 arguments; `IndexError` on `a[3]`).

- [ ] **Step 7: Implement the Python side**

`native.py:31`:

```python
_OK, _OVERWRITTEN, _OUT_OF_RANGE, _IO_ERROR, _INVALID_ARG, _OUT_OF_MEMORY = range(6)
```

After `KIND_INTS` (`native.py:82-83`):

```python
MAX_MIXER_SOURCES = 8  # Mixer.max_sources
```

In `_declare` replace line 111 and add the mixer block after `fb_capture_last_error` (line 167):

```python
    lib.fb_ring_create.argtypes = [C.c_uint32, C.c_uint16, C.c_double, C.POINTER(C.c_int)]
```

```python
    lib.fb_mixer_create.argtypes = [C.c_void_p, C.POINTER(FbCaptureSpec), C.c_size_t]
    lib.fb_mixer_create.restype = C.c_void_p
    lib.fb_mixer_start.argtypes = [C.c_void_p]
    lib.fb_mixer_start.restype = C.c_int
    lib.fb_mixer_stop.argtypes = [C.c_void_p]
    lib.fb_mixer_stop.restype = None
    lib.fb_mixer_destroy.argtypes = [C.c_void_p]
    lib.fb_mixer_destroy.restype = None
    lib.fb_mixer_stats.argtypes = [C.c_void_p, C.POINTER(FbCaptureStats)]
    lib.fb_mixer_stats.restype = None
    lib.fb_mixer_last_error.argtypes = [C.c_void_p]
    lib.fb_mixer_last_error.restype = C.c_char_p
```

`NativeAudioCircularBuffer.__init__` (`native.py:224-226`):

```python
        status = C.c_int(_OK)
        self._h = lib.fb_ring_create(sample_rate, channels, duration_seconds, C.byref(status))
        if not self._h:
            if status.value == _OUT_OF_MEMORY:
                # The readable window's payload; the guard band and the
                # summary ring add a little on top (see Ring.init).
                requested = int(duration_seconds * sample_rate) * channels * 4
                raise MemoryError(
                    f"fb_ring_create: could not reserve {requested:,} bytes "
                    f"for a {duration_seconds:g} s ring at {sample_rate} Hz x {channels} ch"
                )
            raise ValueError(
                f"fb_ring_create rejected rate={sample_rate} channels={channels} seconds={duration_seconds}"
            )
```

(`self._h` is falsy before the raise, so `__del__` → `close()` is a no-op.)

- [ ] **Step 8: Run Python, verify green**

Run: `python -m pytest tests/unit/test_native_capture.py tests/unit/test_native_smoke.py -q` then `python -m pytest tests/unit -q -m "not audio_hw and not perf"`.
Expected: green. Mutation: in `__init__` swap `MemoryError` and `ValueError` → both new tests red. Revert.

- [ ] **Step 9: Commit**

```bash
git add core/src/abi.zig core/include/flashback_core.h flashback_sampler/core/native.py tests/unit/test_native_capture.py
git commit -m "feat(core): fb_mixer_* ABI; FbStatus.out_of_memory; fb_ring_create status out-param (#41 engine half)"
```

---

### Task 5: Python — `NativeMixedSource`, `build_mixed_capture_source`, `build_capture_for_slot` passes specs; sequester `mixed_capture.py`

**Files:**
- Modify: `flashback_sampler/core/native_capture.py`, `flashback_sampler/app/audio_devices.py`, `flashback_sampler/app/state.py`, `tests/unit/test_native_capture.py`, `tests/unit/test_audio_devices.py`, `tests/unit/test_app_state.py`
- Sequester: `flashback_sampler/core/mixed_capture.py` → `_ToRemove/flashback_sampler/core/`
- Prose sweep: `core/src/Summary.zig`, `flashback_sampler/core/native.py`, `flashback_sampler/core/capture_slot.py`, `tests/unit/test_native_smoke.py`, `tests/unit/test_buffer.py`, `README.md`, `PHASE2-HANDOFF.md`

**Interfaces:**
- Produces:
  ```python
  # native_capture.py
  class _NativeSource:            # shared lifecycle; subclasses set _api = "fb_capture" | "fb_mixer" and create _h
      def start(self) -> None     # RuntimeError if closed; idempotent; RuntimeError(status) on non-ok
      def stop(self) -> None      # no-op if closed or not started
      def is_running(self) -> bool; def xrun_count(self) -> int; def last_error(self) -> str | None
      def frames_written(self) -> int; def mix_rate(self) -> int
      def close(self) -> None     # destroys once; every method above is inert afterwards
  class NativeCaptureSource(_NativeSource)   # unchanged surface (test_native_capture.py pins it)
  class NativeMixedSource(_NativeSource):
      def __init__(self, buffer, specs: list[dict], sample_rate: int = 48_000, channels: int = 2)
      # specs: NativeCaptureSource keyword dicts — {"kind", "device_id"?, "pid"?}
      sample_rate: int; channels: int; specs: list[dict]
  # audio_devices.py
  def _spec_kwargs(device: CaptureDevice) -> dict
  def build_capture_source(device, buffer, sample_rate, channels)               # unchanged signature
  def build_mixed_capture_source(devices, buffer, sample_rate, channels)        # NEW
  ```
- Consumes: `native.FbCaptureSpec`, `native.KIND_INTS`, `native.fb_mixer_*` (Task 4).

- [ ] **Step 1: Failing tests**

Append to `tests/unit/test_native_capture.py` (extend `_FakeLib._fn`: `fb_mixer_create` returns `0xA1CE`; `fb_mixer_start/stop/stats/last_error` behave exactly like their `fb_capture_` twins — implement by rewriting the four `if name == "fb_capture_x"` checks as `if name in ("fb_capture_x", "fb_mixer_x")`):

```python
from flashback_sampler.core.native_capture import NativeMixedSource


def test_mixed_conforms_to_capture_source(lib):
    from flashback_sampler.core.capture_source import CaptureSource
    src = NativeMixedSource(_FakeBuffer(), specs=[{"kind": "loopback"}, {"kind": "input", "device_id": "{mic}"}])
    assert isinstance(src, CaptureSource)
    assert src.sample_rate == 48_000 and src.channels == 2


def test_mixed_rejects_non_native_buffer(lib):
    with pytest.raises(TypeError):
        NativeMixedSource(object(), specs=[{"kind": "loopback"}])


def test_mixed_rejects_unknown_kind(lib):
    with pytest.raises(ValueError):
        NativeMixedSource(_FakeBuffer(), specs=[{"kind": "telepathy"}])


def test_mixed_create_passes_every_spec(lib):
    NativeMixedSource(
        _FakeBuffer(),
        specs=[{"kind": "loopback", "device_id": "{spk}"}, {"kind": "process", "pid": 77}],
        sample_rate=44_100, channels=1,
    )
    name, args = next(c for c in lib.calls if c[0] == "fb_mixer_create")
    ring, arr, n = args
    assert ring == _FakeBuffer._h and n == 2
    assert (arr[0].kind, arr[0].pid, arr[0].rate, arr[0].channels, arr[0].device_id) == (0, 0, 44_100, 1, b"{spk}")
    assert (arr[1].kind, arr[1].pid, arr[1].rate, arr[1].channels, arr[1].device_id) == (2, 77, 44_100, 1, b"")


def test_mixed_create_failure_raises(lib):
    lib.mixer_create_fails = True
    with pytest.raises(RuntimeError):
        NativeMixedSource(_FakeBuffer(), specs=[{"kind": "loopback"}])


def test_mixed_start_stop_stats_and_close_are_inert_after_close(lib):
    src = NativeMixedSource(_FakeBuffer(), specs=[{"kind": "loopback"}, {"kind": "input"}])
    assert not src.is_running()
    src.start(); src.start()
    assert src.is_running()
    assert sum(1 for c in lib.calls if c[0] == "fb_mixer_start") == 1
    lib.stats = (1, 999, 4, 44_100)
    assert src.frames_written() == 999 and src.xrun_count() == 4 and src.mix_rate() == 44_100
    lib.err = b"source start failed: AlreadyRunning"
    assert src.last_error() == "source start failed: AlreadyRunning"
    src.stop(); src.stop()
    assert sum(1 for c in lib.calls if c[0] == "fb_mixer_stop") == 1
    src.close(); src.close()
    assert sum(1 for c in lib.calls if c[0] == "fb_mixer_destroy") == 1
    # Inert after close: no ABI call reaches a NULL handle.
    before = len(lib.calls)
    assert src.is_running() is False and src.xrun_count() == 0 and src.last_error() is None
    src.stop()
    with pytest.raises(RuntimeError):
        src.start()
    assert len(lib.calls) == before
```

Add to `_FakeLib.__init__`: `self.mixer_create_fails = False`; in `_fn`: `if name == "fb_mixer_create": return 0 if self.mixer_create_fails else 0xA1CE`.

Append to `tests/unit/test_audio_devices.py`:

```python
def test_build_mixed_capture_source_maps_every_device(monkeypatch):
    """Two devices become two spec dicts through the SAME mapping the
    single builder uses; the process id goes through resolve_root_pid."""
    import flashback_sampler.app.audio_devices as ad
    from flashback_sampler.app.audio_devices import CaptureDevice, build_mixed_capture_source

    seen = {}

    class _Mixed:
        def __init__(self, buffer, specs, sample_rate=48_000, channels=2):
            seen.update(specs=specs, sample_rate=sample_rate, channels=channels)

    monkeypatch.setattr(ad, "NativeMixedSource", _Mixed)
    monkeypatch.setattr(ad.native, "resolve_root_pid", lambda pid: pid + 1)
    spk = CaptureDevice(kind="loopback", name="Spk", id="{spk}")
    proc = CaptureDevice(kind="process_loopback", name="P", id="1234")
    build_mixed_capture_source([spk, proc], buffer=_FakeBuffer(), sample_rate=44_100, channels=1)
    assert seen["specs"] == [{"kind": "loopback", "device_id": "{spk}"}, {"kind": "process", "pid": 1235}]
    assert (seen["sample_rate"], seen["channels"]) == (44_100, 1)
```

(`_FakeBuffer` already exists in that file — it is used at `test_audio_devices.py:64`.)

Append to `tests/unit/test_app_state.py`:

```python
def test_build_capture_for_slot_routes_two_specs_to_the_mixer():
    """Two or more capture_specs go to build_mixed_capture_source with the
    devices themselves — no factories, no staging buffers in Python."""
    from flashback_sampler.app.audio_devices import CaptureDevice
    from flashback_sampler.core.quality_presets import preset_by_name

    import flashback_sampler.app.state as state_mod
    seen = {}

    def fake_mixed(devices, buffer, sample_rate, channels):
        seen.update(devices=list(devices), buffer=buffer, sample_rate=sample_rate, channels=channels)
        return object()

    def fake_single(device, buffer, sample_rate, channels):
        raise AssertionError("single-source builder must not run for two specs")

    real_mixed, real_single = state_mod.build_mixed_capture_source, state_mod.build_capture_source
    state_mod.build_mixed_capture_source, state_mod.build_capture_source = fake_mixed, fake_single
    try:
        st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
        slot = st.add_slot(preset_by_name("SCRATCH"))
        d1 = CaptureDevice(kind="loopback", name="A", id="a")
        d2 = CaptureDevice(kind="input", name="B", id="b")
        slot.capture_specs = [d1, d2]
        st.build_capture_for_slot(slot)
        assert seen["devices"] == [d1, d2]
        assert seen["buffer"] is slot.buffer
        assert (seen["sample_rate"], seen["channels"]) == (slot.sample_rate, slot.channels)
    finally:
        state_mod.build_mixed_capture_source, state_mod.build_capture_source = real_mixed, real_single
```

Run: `python -m pytest tests/unit/test_native_capture.py tests/unit/test_audio_devices.py tests/unit/test_app_state.py -q`
Expected: FAIL (`NativeMixedSource`, `build_mixed_capture_source` missing).

- [ ] **Step 2: Rewrite `flashback_sampler/core/native_capture.py`**

Keep the module docstring and `is_process_loopback_supported` (lines 1-24). Replace the class with:

```python
class _NativeSource:
    """Shared handle lifecycle for the fb_capture_* and fb_mixer_*
    families. The two ABIs have the same start/stop/stats/last_error/
    destroy shape; a subclass names its prefix in `_api` and creates
    `_h`. Every call goes through `_call`, so a closed instance (`_h` is
    None) can be made inert in ONE place: the Zig exports take a
    non-optional pointer, and NULL through them is undefined behaviour in
    the DLL, not a Python exception."""

    _api: str
    sample_rate: int
    channels: int

    def _call(self, name: str):
        return getattr(self._lib, f"{self._api}_{name}")

    # -- CaptureSource protocol ----------------------------------------
    def start(self) -> None:
        if self._h is None:
            raise RuntimeError(f"{type(self).__name__} is closed")
        if self._started:
            return
        status = self._call("start")(self._h)
        if status != native._OK:
            raise RuntimeError(f"{self._api}_start failed with status {status}")
        self._started = True

    def stop(self) -> None:
        if self._h is None or not self._started:
            return
        self._call("stop")(self._h)
        self._started = False

    def is_running(self) -> bool:
        return bool(self._stats().running)

    def xrun_count(self) -> int:
        return int(self._stats().xruns)

    def last_error(self) -> str | None:
        if self._h is None:
            return None
        raw = self._call("last_error")(self._h)
        return raw.decode("utf-8", "replace") if raw else None

    # -- extras -------------------------------------------------------
    def frames_written(self) -> int:
        return int(self._stats().frames_written)

    def mix_rate(self) -> int:
        return int(self._stats().mix_rate)

    def close(self) -> None:
        if self._h:
            self._call("destroy")(self._h)
            self._h = None
        self._started = False

    def _stats(self) -> native.FbCaptureStats:
        st = native.FbCaptureStats()
        if self._h is not None:
            self._call("stats")(self._h, C.byref(st))
        return st

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class NativeCaptureSource(_NativeSource):
    """One source. The kind ("loopback", "input", "process") is a field of
    the spec the Zig side receives, not a Python class."""

    _api = "fb_capture"

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


class NativeMixedSource(_NativeSource):
    """N sources summed into one ring by the Zig mixer thread. `specs` is
    a list of NativeCaptureSource keyword dicts ({"kind", "device_id",
    "pid"}); the staging rings live inside the Zig Mixer and never reach
    Python. Zig validates the count (1..native.MAX_MIXER_SOURCES) and each
    spec; a rejection surfaces as fb_mixer_create returning NULL.

    No level compensation: each source ring and the target apply their
    own Ring.gain, and 1/N pre-mix gain stays the caller's job."""

    _api = "fb_mixer"

    def __init__(self, buffer, specs: list[dict], sample_rate: int = 48_000, channels: int = 2):
        h = getattr(buffer, "_h", None)
        if not h:
            raise TypeError("NativeMixedSource needs a NativeAudioCircularBuffer (no native ring handle)")
        lib = native.load()
        if lib is None:
            raise RuntimeError("flashback_core library not available")
        self._lib = lib
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.specs = [dict(s) for s in specs]
        # Encoded ids stay referenced for the duration of fb_mixer_create
        # (Zig copies them out) — one bytes object per spec.
        self._id_bytes = [str(s.get("device_id", "")).encode("utf-8") for s in self.specs]
        arr = (native.FbCaptureSpec * max(len(self.specs), 1))()
        for i, (s, raw) in enumerate(zip(self.specs, self._id_bytes)):
            kind = s["kind"]
            if kind not in native.KIND_INTS:
                raise ValueError(f"unknown capture kind {kind!r}; expected one of {sorted(native.KIND_INTS)}")
            arr[i] = native.FbCaptureSpec(native.KIND_INTS[kind], int(s.get("pid", 0)), self.sample_rate, self.channels, raw)
        self._h = lib.fb_mixer_create(h, arr, len(self.specs))
        if not self._h:
            raise RuntimeError(
                f"fb_mixer_create failed ({len(self.specs)} specs; needs 1..{native.MAX_MIXER_SOURCES} "
                "valid specs and a capture backend on this OS)"
            )
        self._started = False
```

- [ ] **Step 3: `audio_devices.py` — one mapping, two builders**

Replace `build_capture_source` (`audio_devices.py:181-213`) with:

```python
def _spec_kwargs(device: CaptureDevice) -> dict:
    """CaptureDevice -> the keyword fields a native spec carries. Shared
    by the single and the mixed builder so a kind is mapped in one place."""
    if device.kind in ("loopback", "input"):
        return {
            "kind": device.kind,
            # follow_default → "" → the Zig side follows the live OS
            # default endpoint at start; otherwise pin to device.id.
            "device_id": "" if device.follow_default else device.id,
        }
    if device.kind == "process_loopback":
        try:
            pid = int(device.id)
        except ValueError as e:
            raise ValueError(
                f"process_loopback device id must be an integer PID; "
                f"got {device.id!r}"
            ) from e
        return {"kind": "process", "pid": native.resolve_root_pid(pid)}
    raise ValueError(f"unknown CaptureDevice.kind: {device.kind!r}")


def build_capture_source(device: CaptureDevice, buffer, sample_rate: int, channels: int):
    """Instantiate the capture source for ONE CaptureDevice.
    `buffer` is the NativeAudioCircularBuffer the source writes into."""
    return NativeCaptureSource(buffer=buffer, sample_rate=sample_rate, channels=channels, **_spec_kwargs(device))


def build_mixed_capture_source(devices, buffer, sample_rate: int, channels: int):
    """Instantiate the mixed source for two or more CaptureDevices: every
    device becomes a spec of the same Zig mixer, which sums them into
    `buffer`. Nothing per-source is created on the Python side."""
    return NativeMixedSource(
        buffer=buffer,
        specs=[_spec_kwargs(d) for d in devices],
        sample_rate=sample_rate,
        channels=channels,
    )
```

Import line (`audio_devices.py:20`): `from flashback_sampler.core.native_capture import NativeCaptureSource, NativeMixedSource`.

- [ ] **Step 4: `state.py` — specs through, no factories**

Add `build_mixed_capture_source,` to the `audio_devices` import block (`state.py:17-23`, after `build_capture_source,`). Replace the body of `build_capture_for_slot` from `if len(specs) == 1:` to the end of the method (`state.py:333-360`) with:

```python
        if len(specs) == 1:
            return build_capture_source(
                device=specs[0],
                buffer=slot.buffer,
                sample_rate=slot.sample_rate,
                channels=slot.channels,
            )
        # Two or more: one Zig mixer owns a capture and a staging ring per
        # device and sums them into slot.buffer. Python passes the devices.
        return build_mixed_capture_source(
            devices=specs,
            buffer=slot.buffer,
            sample_rate=slot.sample_rate,
            channels=slot.channels,
        )
```

Docstring (`state.py:316-321`): `MixedCaptureSource` → `NativeMixedSource`.

- [ ] **Step 5: Run Python, verify green**

Run: `python -m pytest tests/unit -q -m "not audio_hw and not perf"`
Expected: green, including the pre-existing `NativeCaptureSource` tests (the base-class refactor must not change one message or call count).

Mutation check: (1) in `NativeMixedSource.__init__` pass `len(self.specs) - 1` to `fb_mixer_create` → `test_mixed_create_passes_every_spec` red. (2) In `_NativeSource.stop` delete the `self._h is None or` clause AND in `close` delete `self._started = False` → the inert-after-close test red (a `fb_mixer_stop` reaches the fake after close). Revert both; each clause alone is covered by the other, which is why the pin needs both removed. (3) In `_NativeSource._stats` delete the `if self._h is not None` guard → the inert-after-close test red (`fb_mixer_stats` reaches the fake). Revert. (4) In `build_mixed_capture_source` map with `{"kind": d.kind}` only → the audio_devices test red. (5) In `state.build_capture_for_slot` call `build_capture_source(specs[0], ...)` for the multi case → the app_state test red.

- [ ] **Step 6: Sequester `mixed_capture.py` and sweep the prose**

`_ToRemove/` is gitignored (`.gitignore:2`), so `git mv` into it stages nothing. The recipe (same as the part-1 plan's Task 7):

```bash
mkdir -p _ToRemove/flashback_sampler/core
mv flashback_sampler/core/mixed_capture.py _ToRemove/flashback_sampler/core/
git add -u flashback_sampler
```

`git status` shows `deleted: flashback_sampler/core/mixed_capture.py`; the bytes survive locally for the owner's one-shot approval (listed in the PR body).

Sweep every mention of the old mechanism (the part-1 lesson: replacing a mechanism means grepping for PROSE that names the old one):

```bash
grep -rn "MixedCaptureSource\|mixed_capture\|sub_factories\|sub-factory" flashback_sampler tests core README.md PHASE2-HANDOFF.md PLATFORM.md soak_test.py flashback_sampler.spec packaging docs/superpowers/specs/2026-08-30-zig-core-phase2-d-f-design.md
```

Fix each hit (verified sites on the baseline):
- `core/src/Summary.zig:182-190`: the "but ONLY for a `Capture` writer … PR d closes it" sentences → "`Ring.writer_active` is what enforces that between `update` and `flushNow`'s `poison`: every writer thread's owner registers it (`Capture`, `Mixer` — see `Ring.flush`'s OWNERSHIP note), so no unregistered writer of a ring remains."
- `flashback_sampler/core/native.py:341-346` (`copy_abs_range` docstring): drop "and mixed_capture.py (the mixer thread, polling a live sub-source ring every 10ms)"; the remaining caller is `checkout.py`.
- `flashback_sampler/core/capture_slot.py:69-72`: "should be built as a MixedCaptureSource" → "are passed to one Zig mixer (`NativeMixedSource`) that sums them".
- `tests/unit/test_native_smoke.py:88-96` docstring: drop the MixedCaptureSource clause.
- `tests/unit/test_buffer.py:228` comment: drop `mixed_capture.py`.
- `README.md:56`: `mixed_capture.py     # sum multiple inputs into one slot` → remove the line; if the tree listing has `native_capture.py`, extend its comment with "+ NativeMixedSource (Zig mixer handle)".
- `PHASE2-HANDOFF.md:42`: the `core/mixed_capture.py` row → `core/native_capture.py` (`NativeMixedSource`) | Zig `Mixer.zig` | staging rings in Zig.
- `flashback_sampler/app/state.py`: done in Step 4.
- The spec is left alone here; the hand-off records deviations.

Rerun the grep: zero hits outside `docs/superpowers/plans/` and `_ToRemove/`.

- [ ] **Step 7: Gate and commit**

Run: `python -m pytest tests/unit -q -m "not audio_hw and not perf"`; `zig build --build-file core/build.zig test --summary all` (100, unchanged by this task); `zig fmt --check core/src`.

```bash
git add flashback_sampler/core/native_capture.py flashback_sampler/app/audio_devices.py flashback_sampler/app/state.py flashback_sampler/core/capture_slot.py flashback_sampler/core/native.py core/src/Summary.zig tests/unit/test_native_capture.py tests/unit/test_audio_devices.py tests/unit/test_app_state.py tests/unit/test_native_smoke.py tests/unit/test_buffer.py README.md PHASE2-HANDOFF.md
git commit -m "feat: NativeMixedSource replaces the Python mixer; build_capture_for_slot passes specs to the Zig mixer"
```

---

### Task 6: Hardware test, whole-branch gates

**Files:**
- Modify: `tests/hw/test_native_capture_hw.py`

- [ ] **Step 1: Add the 2-source hardware test**

Append to `tests/hw/test_native_capture_hw.py` (imports: add `from flashback_sampler.core.native_capture import NativeMixedSource` next to line 10):

```python
def test_two_source_mix_records_frames_on_both(lib):
    """Default loopback + default input through one Zig mixer for 2 s.
    frames_written counts the COMMON span, so > 1 s of frames proves
    both sources delivered at least that much."""
    buf = make_ring_buffer(duration_seconds=10, sample_rate=48_000, channels=2)
    src = NativeMixedSource(buf, specs=[{"kind": "loopback"}, {"kind": "input"}])
    src.start()
    time.sleep(2.0)
    running, err, frames, xruns, mix = src.is_running(), src.last_error(), src.frames_written(), src.xrun_count(), src.mix_rate()
    src.stop(); src.close(); buf.close()
    assert running, err
    assert err is None, err
    assert frames > 48_000, frames
    print(f"mixed(loopback+input): frames={frames} xruns={xruns} mix_rate={mix}")
```

Run (Windows, audio playing): `python -m pytest tests/hw -m audio_hw -s -q`
Expected: 5 passed. If the mixed test reports `frames == 0` with `running == True`, the input endpoint is silent-gated by the OS (some mics deliver no packets until unmuted): retry with the mic unmuted before treating it as a mixer defect.

- [ ] **Step 2: Whole-branch gates (no CI runs on this branch)**

```
zig fmt --check core/build.zig core/src
zig build --build-file core/build.zig test --summary all
zig build --build-file core/build.zig -Doptimize=ReleaseSafe
zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu
zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=aarch64-macos
python -m pytest tests/unit -q -m "not audio_hw and not perf"
```

Expected: `100/100 tests passed` (83 + 3 + 4 + 5 + 5); both cross-compile legs build (`Mixer.zig` imports `Backend.zig`, `Capture.zig`, `Ring.zig` only — no OS gate needed); pytest green. Record the numbers for the PR body.

- [ ] **Step 3: App smoke (manual, 2 minutes)**

Launch the app, add a slot, give it two inputs via the slot's right-click menu (default loopback + a mic), START CAPTURE for 10 s, FLUSH mid-capture, STOP, check out a clip. Expected: waveform shows both sources; flush empties the view and capture continues; no console output from the mixer (the old `[MixedCapture]` print is gone).

- [ ] **Step 4: Commit**

```bash
git add tests/hw/test_native_capture_hw.py
git commit -m "test(hw): 2-source mixed capture through the Zig mixer"
```

---

### Task 7: `/simplify` + `/code-review`, PR d hand-off

- [ ] **Step 1: Review passes (owner's rules: inline, one combined pass each)**

Run `/simplify` as one combined pass over `git diff dev...feat/zig-mixer`, then `/code-review` at **medium** (feature PR; threads + ABI + a shared base class). Address findings; rerun Task 6 Step 2's gates after any change. Specific things the reviewer should check because a per-task view cannot: the `_NativeSource` refactor kept every `NativeCaptureSource` message and call count; `Capture.start`'s `errdefer` runs on spawn failure only (no double clear on `AlreadyRunning`, which returns before the store); no remaining reader of `Ring.writer_active` assumes the worker sets it.

- [ ] **Step 2: Push, open the PR**

```bash
git push -u origin feat/zig-mixer
gh pr create --base dev --title "feat: Mixer.zig — mixed capture on a Zig thread; control-thread writer_active; #41 engine rider" --body-file C:/Users/Ryon/AppData/Local/Temp/claude/pr-d-body.md
```

`pr-d-body.md` carries, in this order:

1. What / why (3 bullets): the Python mixer thread is gone (`fb_ring_write`'s last production caller); `writer_active` has one owner rule for `Capture` and `Mixer`, closing the start window; `fb_ring_create` tells OOM from a bad config.
2. `Closes #D`. **`Refs #41`** — not `Closes`: #41's UI half (the arm-time message) stays open for #16.
3. Deviations to record in the spec (small edits the owner approves with the merge): P1 (clear-then-drain order), P2 (park in `open`), P3 (in-place `init`), P6 (class lives in `native_capture.py`), P7 (`build_mixed_capture_source` + `_spec_kwargs`), P9 (the `err_buf[0]` rider — not in the spec at all).
4. Gate results: Zig `100/100` (from 83), pytest count, both cross-compile legs, hw run's `frames=` line.
5. `_ToRemove/` contents for the owner's one-shot deletion approval: `flashback_sampler/core/mixed_capture.py`.
6. **Zig concepts in this PR:**
   - *Control-thread ownership of an atomic.* `writer_active` is stored by the thread that spawns and joins the writer, never by the writer. The rule is about scope, not speed: the scope that outlives the thread on both ends can hold a flag across both ends; the thread itself cannot (it does not exist before spawn or after join). `Capture.zig` `start`/`stop`, `Mixer.zig` `start`/`stop`.
   - *Fixed arrays vs an allocator on the audio path.* `Mixer` sums in `[max_write_frames * 2]f32` arrays sized at compile time from `Ring`'s own publish bound; the only allocation is the staging rings in `init`. Contrast `Capture.id_buf` (same idea, smaller).
   - *`errdefer` with a progress counter.* `init` and `start` each build N things in a loop; on failure at k only 0..k-1 exist. A counter incremented after each success and an `errdefer for (items[0..count])` at function scope unwind exactly those — and `errdefer`s run LIFO, so the captures stop before the flag clears.
   - *Self-referential structs need in-place `init`.* A `Capture` inside `Mixer.sources` points at a `Ring` beside it; returning `Mixer` by value would move the ring away from the pointer. `init(self: *Mixer, …)` and `allocator.create(Mixer)` before init.
   - *Exhaustive `switch` on an inferred error set.* `ringCreate` maps `Ring.init`'s two errors to two statuses with no `else`, so a third error becomes a compile error, not a wrong status.
   - *`std.Io.sleep` for a timed wait.* 0.16 has no `std.Thread.sleep`; blocking waits are `Io` operations on an `Io` instance — here the same singleton `abi.zig` already holds.
   - *Sentinel slices are checked.* `err_buf[0..n :0]` asserts `err_buf[n] == 0` in Debug and ReleaseSafe; resetting a length without the byte under it is a latent trap (the P9 rider).
7. Findings: the P9 sentinel trap (fixed here); the `FakeBackend` router hands children out in arrival order (documented; tests are order-symmetric).

- [ ] **Step 3: Tracker updates (write-at-the-moment)**

Comment on #41:

```bash
gh issue comment 41 --body "Engine half landed in PR <url> (PR d): fb_ring_create now takes a nullable FbStatus out-param and reports FB_OUT_OF_MEMORY (5) distinctly from FB_INVALID_ARG; NativeAudioCircularBuffer raises MemoryError with the requested byte count (ValueError for a rejected config). Still open here: the arm-time UI message and removal of the 4 GB app-level stop (AppState.add_slot) — that half belongs to #16."
```

Comment on #D with the gate numbers and the hw `frames=` line. After the owner merges: tick `d` on #17 (`gh issue view 17 --json body -q .body > epic17.md`, flip `- [ ] d — #D …` to `- [x] d — #D …`, `gh issue edit 17 --body-file epic17.md`) — `Closes #D` in the body fires on merge into `dev` (the default branch), so #D closes itself.

- [ ] **Step 4: Hand over**

State in the final message: branch, PR URL, the counts, the `_ToRemove/` list awaiting approval, the six spec deviations to record, and that PR e's plan should start from `Mixer.zig`'s `start`/`stop` shape (the render thread follows the same ownership rule for nothing — `Playback` writes no ring — but reuses the progress-counter unwind and the `Io.sleep`-free event wait).
## PR e — render backend, `Playback.zig`, output enumeration

**Branch:** `feat/zig-playback` → `dev`. **Tasks 1–9.** Every Global Constraint from the part-1 plan (lines 15–32) applies verbatim, including the Windows constants table (lines 36–62): `AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000`, `AUDCLNT_BUFFERFLAGS_SILENT = 0x2`, `WAIT_OBJECT_0 = 0`, `WAIT_TIMEOUT = 0x102`.

**Assumes PR d merged:** `Capture.start()` owns `ring.writer_active`; `Mixer.zig` exists; `FbStatus.out_of_memory = 5`; `fb_ring_create(rate, channels, seconds, status)`. Nothing in PR e touches those.

**Test-count baseline:** 83 today on the Windows host before PR d (`zig build --build-file core/build.zig test --summary all` → `83/83 tests passed`). PR d's end count is unknown when this plan is written; every count below is "PR d's end count + N". The Linux cross-compile leg runs fewer tests (wasapi.zig / WasapiBackend.zig are OS-gated, `root.zig:22-23`); the gate is the Windows host count.

**Verified facts this plan leans on (file:line):**
- `Backend.Kind` is `enum(u8) { loopback = 0, input = 1, process = 2 }` (`Backend.zig:8`); `Backend.VTable` has exactly `enumerate` + `open` (`Backend.zig:50-55`). Adding `openRender` to the vtable breaks every `Backend.VTable{...}` literal: `FakeBackend.zig:28`, `WasapiBackend.zig:19`, and PR d's Mixer tests if they build one.
- `Capture.zig` shape to mirror: fields `thread/stop_flag/running/err_buf/err_len` (`Capture.zig:23-30`), `waitUntil` test helper (`:32-36`), `init` copies the id into a fixed buffer (`:38-56`), `start` (`:64-70`), `stop` joins (`:72-76`), `lastError` (`:96-99`), `setError` via `bufPrintZ` (`:101-105`), the `defer` order in `run` (`:112-130`).
- `WasapiBackend.open` does `RoInitialize` on the audio thread and pairs it with `CoUninitialize` in `deinit` (`WasapiBackend.zig:153-159`, `:320-327`). Playback's thread does not init COM itself; the backend's `openRender`/`deinit` pair does, the same way.
- Device lookup by id or default: `activate(spec)` (`WasapiBackend.zig:204-225`). Its flow selection is `if (spec.kind == .input) eCapture else eRender` (`:210`) and its process arm is `if (spec.kind == .process)` (`:205`), so a `.render` spec already resolves an eRender endpoint. `openRender` reuses it unchanged.
- Fixed `Stream` pool, no allocator: `Stream` struct (`WasapiBackend.zig:45-58`), `streams: [max_streams]Stream` (`:60`), `acquireSlot` (`:283-291`). The render pool copies this shape.
- Format builder: `w.waveFormat(tag, bits, rate, channels)` (`wasapi.zig:182-185`). There is no WAVEFORMATEXTENSIBLE builder in the repo; ≤ 2 channels use the plain struct.
- kernel32 already declared in `wasapi.zig`: `Sleep` (`:91`), `CloseHandle` (`:92`), `LoadLibraryW`/`GetProcAddress` (`:93-94`). NOT declared: `CreateEventW`, `WaitForSingleObject`, `AUDCLNT_STREAMFLAGS_EVENTCALLBACK`, `IAudioRenderClient`, `WAIT_*`. `IAudioClient.SetEventHandle`, `GetBufferSize`, `GetCurrentPadding`, `GetService`, `Start`, `Stop` exist (`wasapi.zig:257-268`).
- `IID_IAudioRenderClient = {F294ACFC-3146-4483-A7BF-ADDCA7C260E2}` verified against the shipped `soundcard` package (`.venv/Lib/site-packages/soundcard/mediafoundation.py:597`). Verify: also against `audioclient.h` (`DEFINE_GUID(IID_IAudioRenderClient, 0xF294ACFC, 0x3146, 0x4483, ...)`) if an SDK is on the box.
- `abi.zig`: allocator is `std.heap.smp_allocator` (`:20`); `nativeBackend()` (`:326-329`); `fb_capture_create` guard `spec.kind > 2` (`:346`) stays — render is not a capture kind; Windows-only ABI test pattern (`:289-290`).
- `native.py`: `KIND_INTS` / `_KIND_NAMES` (`:82-83`); `_KIND_NAMES.get(d.kind, "input")` in `list_devices` (`:183`) — kind 3 must be in the dict or it reports `"input"`; `_declare` (`:105-170`); `_as_f32p` (`:190-191`); `wav_write` reshapes 1-D to `[N, 1]` (`:201-202`) — `bind` mirrors that.
- Player call sites: `turntable_window.py:907-921` (`is_playing`, `pause`, `bind(audio)`, `open()`, `play()`), `:933` (`is_playing`), `:1698-1737` (`is_playing`, `cursor_samples`, `play`), `:1755` (`pause`), `state.py:85-88` (constructor `sample_rate`, `channels`), `:95` and `:255` (`set_device(spec.id)`), `:435` (`close`). Nothing calls `stop`, `seek`, `seek_samples`, `cursor_seconds`, `source_length_samples` in the app today; the spec keeps them on the wrapper.
- The checkout's rate: `Checkout.sample_rate` (`checkout.py:50`), `channels` (`:51`), `audio` (`:52`), `trimmed_audio()` (`:68-76`). The bind call site is `turntable_window.py:920` with `co` in scope from `:906`.
- `OutputDevice` (`audio_devices.py:74-80`, `id: int`); `list_output_devices` imports `sounddevice` (`:117-153`); `default_output_device` (`:168-173`); `_list_native_devices` filters `("loopback", "input")` (`:100`) — unchanged by this PR.
- `AppState.output_spec` (`state.py:93`), `set_output_spec` (`:253-255`), `shutdown` (`:427-437`).
- Tests to update: `tests/unit/test_scrub_player.py` (20 `_audio_callback` tests, replaced), `tests/unit/test_audio_devices.py:39-42` (`OutputDevice(id=0, ...)`), `tests/unit/test_app_state.py:16,26,31-32` (imports/asserts `ScrubPlayer`) and `:395-412` (calls `_audio_callback`), `tests/unit/test_native_capture.py` (`_FakeLib` pattern at `:14-50`, reused), `tests/unit/test_turntable_window.py` (`qapp`/`state` fixtures `:10-21`).
- `AUDCLNT_STREAMFLAGS_EVENTCALLBACK` was deliberately NOT used for capture (part-1 plan line 46, `WasapiBackend.zig:1-6`); render uses it (spec "Render delivery").

**Spec deviations recorded by this plan (Plan choice):**
1. `WasapiBackend.enumerate` lists every eRender endpoint TWICE — once as `.loopback` (today) and once as `.render`. "One endpoint, two roles" then costs Python zero logic: `_list_native_devices` keeps its filter and `list_output_devices` filters `kind == "render"`.
2. `NativeScrubPlayer` has no `open()`: the spec's "lazy open on first `play()`" makes it dead, and the one caller (`turntable_window.py:921`) is removed. `close()` stays (it destroys the handle; `state.py:435` calls it).
3. `NativeScrubPlayer.bind(audio, sample_rate)` — channels come from `audio.shape[1]`; the checkout's rate is the second argument.
4. `stop()` on the wrapper = `pause()` + `seek_samples(0)`. Zig has no "unbind"; a bound clip stays bound until the next `bind` or `close`.
5. `bind` while the render thread is mid-copy: `Playback` uses a two-flag handshake (`playing` + `in_copy`, both `seq_cst`) instead of a lock. `bind` clears `playing`, then spins until `in_copy` is false. The thread sets `in_copy` before it reads `playing` and the clip, and clears it after the copy. No lock, no allocation on the thread; `bind` (control thread) is the only side that waits.
6. After an `openRender` failure the thread exits with `running = false`. The next `play()` joins the finished thread (`done` flag) and spawns again, so a fixed device gets retried without a `destroy`.
7. `fb_playback_seek` takes `u64` (spec). Python `seek_samples` passes `max(0, int(pos))` — the single clamp that keeps a negative int off a `c_uint64`.
8. The FakeBackend render sink records writes into a `std.ArrayList(f32)` fed by a test-supplied allocator (`render_allocator: ?std.mem.Allocator`, set to `std.testing.allocator` inside `test` blocks only — `std.testing.allocator` outside a test block is a compile error, and `FakeBackend` is analyzed in the DLL build via `root.zig:13`).

**Task → commit map:** T1 Backend + Fake render sink · T2 wasapi.zig render surface · T3 WasapiBackend.openRender + render enumeration · T4 Playback.zig · T5 ABI + header + native.py · T6 NativeScrubPlayer + tests · T7 audio_devices + AppState · T8 window call sites + window tests + hw test · T9 hand-off.

---

### Task 1: `Backend.zig` render surface + FakeBackend scripted render sink

**Files:**
- Modify: `core/src/Backend.zig`, `core/src/FakeBackend.zig`, `core/src/WasapiBackend.zig` (one line: the vtable literal), PR d's `Mixer.zig` only if it builds a `Backend.VTable` literal (grep `Backend.Backend.VTable{` first).

**Interfaces:**
- Produces:
  ```zig
  // Backend.zig
  pub const Kind = enum(u8) { loopback = 0, input = 1, process = 2, render = 3 };
  pub const RenderStream = struct {
      ptr: *anyopaque, vtable: *const VTable,
      pub const VTable = struct {
          wait: *const fn (*anyopaque, timeout_ms: u32) bool,
          available: *const fn (*anyopaque) Error!u32,
          write: *const fn (*anyopaque, frames: []const f32) Error!void,
          stop: *const fn (*anyopaque) void,
          deinit: *const fn (*anyopaque) void,
          mixRate: *const fn (*anyopaque) u32,
      };
      pub fn wait/available/write/stop/deinit/mixRate  // thin forwarders
  };
  // Backend.VTable gains:
  openRender: *const fn (*anyopaque, Spec) Error!RenderStream,
  pub fn openRender(b: Backend, spec: Spec) Error!RenderStream
  ```
  ```zig
  // FakeBackend.zig — render sink fields
  render_available: u32 = 256,                 // what available() returns, every call
  render_open_error: ?Backend.Error = null,    // openRender() fails with this
  render_allocator: ?std.mem.Allocator = null, // tests set std.testing.allocator; null = writes are counted, not stored
  written: std.ArrayList(f32) = .empty,        // every write(), concatenated
  render_opens: std.atomic.Value(u32), render_waits: std.atomic.Value(usize), render_writes: std.atomic.Value(usize), render_stopped: std.atomic.Value(bool),
  last_render_spec: ?Backend.Spec = null,
  pub fn deinitRender(self: *FakeBackend) void   // frees `written`
  ```
  Fake `wait` yields once, counts, returns `true`. Fake `write` appends to `written` (when an allocator is set), counts. `available` returns `render_available`.

- [ ] **Step 1: Failing tests (render sink round-trip through the fake)**

Append to `core/src/FakeBackend.zig`:

```zig
test "fake render sink: openRender records the spec, available is scripted, writes are recorded in order" {
    var fake = FakeBackend.init(&.{});
    fake.render_allocator = std.testing.allocator;
    defer fake.deinitRender();
    fake.render_available = 3;
    const rs = try fake.backend().openRender(.{ .kind = .render, .device_id = "{out}", .rate = 44_100, .channels = 2 });
    defer rs.deinit();
    try std.testing.expectEqual(@as(u32, 1), fake.render_opens.load(.acquire));
    try std.testing.expectEqualStrings("{out}", fake.last_render_spec.?.device_id);
    try std.testing.expectEqual(@as(u32, 44_100), fake.last_render_spec.?.rate);
    try std.testing.expectEqual(@as(u32, 3), try rs.available());
    try std.testing.expect(rs.wait(100));
    try rs.write(&[_]f32{ 1, 2 });
    try rs.write(&[_]f32{ 3, 4, 5, 6 });
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 2, 3, 4, 5, 6 }, fake.written.items);
    try std.testing.expectEqual(@as(usize, 2), fake.render_writes.load(.acquire));
    try std.testing.expectEqual(@as(usize, 1), fake.render_waits.load(.acquire));
    try std.testing.expectEqual(@as(u32, 48_000), rs.mixRate());
    rs.stop();
    try std.testing.expect(fake.render_stopped.load(.acquire));
}

test "fake render sink: render_open_error propagates and opens are not counted" {
    var fake = FakeBackend.init(&.{});
    fake.render_open_error = error.FormatRejected;
    try std.testing.expectError(error.FormatRejected, fake.backend().openRender(.{ .kind = .render, .device_id = "", .rate = 48_000, .channels = 2 }));
    try std.testing.expectEqual(@as(u32, 0), fake.render_opens.load(.acquire));
}
```

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test`
Expected: compile error (`openRender` not a member of `Backend.Backend`; `render_allocator` unknown field).

- [ ] **Step 3: Implement `Backend.zig`**

Replace `Backend.zig:8` with:

```zig
pub const Kind = enum(u8) { loopback = 0, input = 1, process = 2, render = 3 };
```

Insert after the `Stream` struct (`Backend.zig:44`):

```zig
/// The output side. Event-driven, not polled: `wait` blocks until the
/// engine wants frames (WASAPI signals an event once per period), so the
/// render thread sleeps at zero CPU between fills.
pub const RenderStream = struct {
    ptr: *anyopaque,
    vtable: *const VTable,

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

    pub fn wait(s: RenderStream, timeout_ms: u32) bool {
        return s.vtable.wait(s.ptr, timeout_ms);
    }
    pub fn available(s: RenderStream) Error!u32 {
        return s.vtable.available(s.ptr);
    }
    pub fn write(s: RenderStream, frames: []const f32) Error!void {
        return s.vtable.write(s.ptr, frames);
    }
    pub fn stop(s: RenderStream) void {
        return s.vtable.stop(s.ptr);
    }
    pub fn deinit(s: RenderStream) void {
        return s.vtable.deinit(s.ptr);
    }
    pub fn mixRate(s: RenderStream) u32 {
        return s.vtable.mixRate(s.ptr);
    }
};
```

In `Backend.Backend.VTable` (`Backend.zig:50-55`) add after `open`:

```zig
        /// Opens AND starts a render stream at spec.rate/spec.channels
        /// (float32). The backend resamples to its mix rate; FormatRejected
        /// if it cannot. Called on the render thread.
        openRender: *const fn (*anyopaque, Spec) Error!RenderStream,
```

and add the forwarder after `open` (`Backend.zig:60-62`):

```zig
    pub fn openRender(b: Backend, spec: Spec) Error!RenderStream {
        return b.vtable.openRender(b.ptr, spec);
    }
```

- [ ] **Step 4: Implement the fake render sink**

In `core/src/FakeBackend.zig`, after `last_spec` (`:18`) add the fields:

```zig
// ── Render sink (PR e) ───────────────────────────────────────────────
// Scripted output: `available` is a constant, `wait` returns at once,
// every `write` is appended to `written` so a test can read back exactly
// what the Playback loop produced. `render_allocator` is set by tests
// (std.testing.allocator is a compile error outside a test block, and
// this file is analyzed in the DLL build through root.zig).
render_available: u32 = 256,
render_open_error: ?Backend.Error = null,
render_allocator: ?std.mem.Allocator = null,
written: std.ArrayList(f32) = .empty,
render_opens: std.atomic.Value(u32) = std.atomic.Value(u32).init(0),
render_waits: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),
render_writes: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),
render_stopped: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
last_render_spec: ?Backend.Spec = null,
```

Verify: Zig 0.16 `std.ArrayList(T)` is the unmanaged list (`.empty`, `appendSlice(gpa, items)`, `deinit(gpa)`, `.items`). If the pinned std still names it `std.ArrayListUnmanaged`, use that name; the API is the same.

Replace `FakeBackend.zig:28` with:

```zig
const backend_vtable = Backend.Backend.VTable{ .enumerate = enumerate, .open = open, .openRender = openRender };
const render_vtable = Backend.RenderStream.VTable{ .wait = renderWait, .available = renderAvailable, .write = renderWrite, .stop = renderStop, .deinit = renderDeinit, .mixRate = mixRate };
```

Add after `mixRate` (`FakeBackend.zig:73`):

```zig
pub fn deinitRender(self: *FakeBackend) void {
    if (self.render_allocator) |a| self.written.deinit(a);
    self.written = .empty;
}

fn openRender(ptr: *anyopaque, spec: Backend.Spec) Backend.Error!Backend.RenderStream {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    if (self.render_open_error) |e| return e;
    self.last_render_spec = spec;
    _ = self.render_opens.fetchAdd(1, .release);
    self.render_stopped.store(false, .release);
    return .{ .ptr = self, .vtable = &render_vtable };
}

fn renderWait(ptr: *anyopaque, timeout_ms: u32) bool {
    _ = timeout_ms;
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    // Yield so a Playback loop spinning on this fake does not starve the
    // test thread that is waiting to observe it.
    std.Thread.yield() catch {};
    _ = self.render_waits.fetchAdd(1, .release);
    return true;
}

fn renderAvailable(ptr: *anyopaque) Backend.Error!u32 {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    return self.render_available;
}

fn renderWrite(ptr: *anyopaque, frames: []const f32) Backend.Error!void {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    if (self.render_allocator) |a| self.written.appendSlice(a, frames) catch return error.OutOfMemory;
    _ = self.render_writes.fetchAdd(1, .release);
}

fn renderStop(ptr: *anyopaque) void {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    self.render_stopped.store(true, .release);
}

fn renderDeinit(ptr: *anyopaque) void {
    _ = ptr;
}
```

Replace `WasapiBackend.zig:19` with a literal that names the new field so the Windows build compiles before Task 3 lands the real function:

```zig
const backend_vtable = Backend.Backend.VTable{ .enumerate = enumerate, .open = open, .openRender = openRender };
```

and add a temporary stub after `open` (`WasapiBackend.zig:200`), replaced in Task 3:

```zig
fn openRender(ptr: *anyopaque, spec: Backend.Spec) Backend.Error!Backend.RenderStream {
    _ = ptr;
    _ = spec;
    return error.Unsupported;
}
```

If PR d's `Mixer.zig` builds its own `Backend.Backend.VTable{...}` literal, add `.openRender = ...` there too (grep first).

- [ ] **Step 5: Run, verify green, count +2**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: pass; count = d's end count + 2. `zig fmt --check core/src` clean.
Run: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu` — green (Backend/Fake are OS-neutral).

Mutation check: in `renderWrite`, drop the `appendSlice` line → the first test's `expectEqualSlices` reddens. In `openRender`, drop the `render_open_error` check → the second test reddens. Revert both.

- [ ] **Step 6: Commit**

```bash
git add core/src/Backend.zig core/src/FakeBackend.zig core/src/WasapiBackend.zig
git commit -m "feat(core): Backend.RenderStream + Kind.render; FakeBackend scripted render sink"
```

---

### Task 2: `wasapi.zig` — `IAudioRenderClient`, event imports, `EVENTCALLBACK`

**Files:**
- Modify: `core/src/wasapi.zig`

**Interfaces:**
- Produces (all `pub`):
  ```zig
  pub const AUDCLNT_STREAMFLAGS_EVENTCALLBACK: u32 = 0x00040000;
  pub const WAIT_OBJECT_0: u32 = 0;
  pub const WAIT_TIMEOUT: u32 = 0x102;
  pub const IID_IAudioRenderClient = guid("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}");
  pub extern "kernel32" fn CreateEventW(attrs: ?*anyopaque, manual_reset: i32, initial_state: i32, name: ?[*:0]const u16) callconv(.winapi) ?HANDLE;
  pub extern "kernel32" fn WaitForSingleObject(h: HANDLE, timeout_ms: u32) callconv(.winapi) u32;
  pub const IAudioRenderClient = extern struct { vtbl, VTable { base, GetBuffer, ReleaseBuffer }, release() };
  ```
  `CloseHandle` already exists (`wasapi.zig:92`); reuse it.

- [ ] **Step 1: Failing tests (pure: GUID bytes, vtable layout)**

Append to `core/src/wasapi.zig` (tests run only on the Windows host — this file is OS-gated at `root.zig:22`):

```zig
test "guid parses IID_IAudioRenderClient" {
    const g = IID_IAudioRenderClient;
    try std.testing.expectEqual(@as(u32, 0xF294ACFC), g.d1);
    try std.testing.expectEqual(@as(u16, 0x3146), g.d2);
    try std.testing.expectEqual(@as(u16, 0x4483), g.d3);
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0xA7, 0xBF, 0xAD, 0xDC, 0xA7, 0xC2, 0x60, 0xE2 }, &g.d4);
}

test "IAudioRenderClient vtable: GetBuffer is slot 3, ReleaseBuffer slot 4 (after IUnknown's three)" {
    // Method order IS the binary interface; a swap here would call
    // ReleaseBuffer when we mean GetBuffer and corrupt the engine buffer.
    try std.testing.expectEqual(3 * @sizeOf(usize), @offsetOf(IAudioRenderClient.VTable, "GetBuffer"));
    try std.testing.expectEqual(4 * @sizeOf(usize), @offsetOf(IAudioRenderClient.VTable, "ReleaseBuffer"));
    try std.testing.expectEqual(@as(u32, 0x00040000), AUDCLNT_STREAMFLAGS_EVENTCALLBACK);
}
```

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test`
Expected: compile error (`IID_IAudioRenderClient`, `IAudioRenderClient`, `AUDCLNT_STREAMFLAGS_EVENTCALLBACK` undeclared).

- [ ] **Step 3: Declare the render surface**

After `CloseHandle` (`wasapi.zig:92`) add:

```zig
// Event-driven render: WASAPI signals this event once per engine period
// (SetEventHandle + AUDCLNT_STREAMFLAGS_EVENTCALLBACK). The render thread
// blocks in WaitForSingleObject at zero CPU until then. Auto-reset event
// (manual_reset = 0): one signal wakes one wait, no explicit ResetEvent.
pub extern "kernel32" fn CreateEventW(attrs: ?*anyopaque, manual_reset: i32, initial_state: i32, name: ?[*:0]const u16) callconv(.winapi) ?HANDLE;
pub extern "kernel32" fn WaitForSingleObject(h: HANDLE, timeout_ms: u32) callconv(.winapi) u32;
pub const WAIT_OBJECT_0: u32 = 0;
pub const WAIT_TIMEOUT: u32 = 0x102;
```

After `AUDCLNT_STREAMFLAGS_LOOPBACK` (`wasapi.zig:142`) add:

```zig
pub const AUDCLNT_STREAMFLAGS_EVENTCALLBACK: u32 = 0x00040000;
```

After `IID_IAudioCaptureClient` (`wasapi.zig:155`) add:

```zig
pub const IID_IAudioRenderClient = guid("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}");
```

After the `IAudioCaptureClient` struct (`wasapi.zig:286`) add:

```zig
pub const IAudioRenderClient = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        /// Hands out `n_frames` frames of engine buffer to fill; ReleaseBuffer
        /// with AUDCLNT_BUFFERFLAGS_SILENT tells the engine to ignore the
        /// bytes and play silence.
        GetBuffer: *const fn (*IAudioRenderClient, n_frames: u32, data: *?[*]u8) callconv(.winapi) HRESULT,
        ReleaseBuffer: *const fn (*IAudioRenderClient, n_frames: u32, flags: u32) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IAudioRenderClient) void {
        _ = self.vtbl.base.Release(self);
    }
};
```

- [ ] **Step 4: Run, verify green, count +2**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: pass; count = d's end count + 4. `zig fmt --check core/src` clean.
Run: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu` — green.

Mutation check: change the GUID's `A7BF` to `A7BE` → the first test reddens; swap `GetBuffer`/`ReleaseBuffer` in the VTable → the second reddens. Revert both.

- [ ] **Step 5: Commit**

```bash
git add core/src/wasapi.zig
git commit -m "feat(core): wasapi.zig render surface — IAudioRenderClient, event imports, EVENTCALLBACK"
```

---

### Task 3: `WasapiBackend.openRender` + render endpoints in `enumerate`

**Files:**
- Modify: `core/src/WasapiBackend.zig`

**Interfaces:**
- Consumes: `activate(spec)` (`WasapiBackend.zig:204-225`), `w.waveFormat` (`wasapi.zig:182`), Task 2's declarations.
- Produces: the real `openRender`; `renderFormat(rate, channels) w.WAVEFORMATEX` (pure, tested); `enumerate` appends eRender endpoints a second time tagged `.render`.
- Render pool: `renders: [max_renders]Render`, `max_renders = 4`, `acquireRender()` — the `Stream`/`acquireSlot` shape (`:45-60`, `:283-291`) with no allocator.

- [ ] **Step 1: Failing test (pure format helper)**

Append to `core/src/WasapiBackend.zig`:

```zig
test "renderFormat is float32 at the clip's rate and channels; AUTOCONVERTPCM does the rest" {
    const f = renderFormat(96_000, 2);
    try std.testing.expectEqual(w.WAVE_FORMAT_IEEE_FLOAT, f.wFormatTag);
    try std.testing.expectEqual(@as(u16, 32), f.wBitsPerSample);
    try std.testing.expectEqual(@as(u32, 96_000), f.nSamplesPerSec);
    try std.testing.expectEqual(@as(u16, 8), f.nBlockAlign);
    try std.testing.expectEqual(@as(u32, 96_000 * 8), f.nAvgBytesPerSec);
}
```

Run: `zig build --build-file core/build.zig test` → compile error (`renderFormat` missing).

- [ ] **Step 2: Implement**

Replace the Task 1 stub and add the render pool. After the `Stream` struct and `streams` field (`WasapiBackend.zig:45-60`) add:

```zig
/// One open render stream. Same fixed-pool rule as `Stream`: no allocator
/// on the audio path. The engine owns the sample buffer (GetBuffer hands
/// us a pointer into it), so no scratch is needed here.
const Render = struct {
    in_use: bool = false,
    client: ?*w.IAudioClient = null,
    render: ?*w.IAudioRenderClient = null,
    event: ?w.HANDLE = null,
    buffer_frames: u32 = 0,
    channels: u16 = 2,
    mix_rate: u32 = 0,
};

const max_renders = 4;
renders: [max_renders]Render = [_]Render{.{}} ** max_renders,

const render_vtable = Backend.RenderStream.VTable{ .wait = renderWait, .available = renderAvailable, .write = renderWrite, .stop = renderStop, .deinit = renderDeinit, .mixRate = renderMixRate };

/// The clip's own format. The engine resamples to its mix rate under
/// AUTOCONVERTPCM | SRC_DEFAULT_QUALITY — the same borrowed resampler
/// capture uses in the other direction (open(), above).
pub fn renderFormat(rate: u32, channels: u16) w.WAVEFORMATEX {
    return w.waveFormat(w.WAVE_FORMAT_IEEE_FLOAT, 32, rate, channels);
}

fn acquireRender(self: *WasapiBackend) ?*Render {
    for (&self.renders) |*r| {
        if (!r.in_use) {
            r.in_use = true;
            return r;
        }
    }
    return null;
}

/// Called on the render thread, which lives for the stream's life: the
/// RoInitialize here pairs with CoUninitialize in renderDeinit, exactly
/// as open()/deinit() do for capture (same apartment rule, one mechanism).
fn openRender(ptr: *anyopaque, spec: Backend.Spec) Backend.Error!Backend.RenderStream {
    const self: *WasapiBackend = @ptrCast(@alignCast(ptr));
    if (spec.kind != .render) return error.Unsupported;
    if (spec.channels == 0 or spec.channels > 2 or spec.rate == 0) return error.Unsupported;
    _ = w.RoInitialize(w.RO_INIT_MULTITHREADED);
    errdefer w.CoUninitialize();
    const slot = self.acquireRender() orelse return error.OutOfMemory;
    errdefer slot.* = .{};
    // activate() picks eRender for every kind but .input and resolves
    // "" to the default endpoint — nothing render-specific to add.
    const client = try activate(spec);
    errdefer client.release();
    var mix: ?*w.WAVEFORMATEX = null;
    if (!w.failed(client.vtbl.GetMixFormat(client, &mix))) {
        slot.mix_rate = mix.?.nSamplesPerSec;
        w.CoTaskMemFree(mix);
    } else slot.mix_rate = 0;
    const flags: u32 = w.AUDCLNT_STREAMFLAGS_EVENTCALLBACK | w.AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | w.AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
    const fmt = renderFormat(spec.rate, spec.channels);
    // Duration 0 / period 0: shared-mode event-driven, the engine picks
    // its own period and the smallest buffer that covers it. Measure the
    // resulting GetBufferSize on hardware (spec: "Risks to measure").
    if (w.failed(client.vtbl.Initialize(client, w.AUDCLNT_SHAREMODE_SHARED, flags, 0, 0, &fmt, null))) return error.FormatRejected;
    const event = w.CreateEventW(null, 0, 0, null) orelse return error.ActivationFailed;
    errdefer _ = w.CloseHandle(event);
    if (w.failed(client.vtbl.SetEventHandle(client, event))) return error.ActivationFailed;
    var buf_frames: u32 = 0;
    if (w.failed(client.vtbl.GetBufferSize(client, &buf_frames)) or buf_frames == 0) return error.ActivationFailed;
    var raw: ?*anyopaque = null;
    if (w.failed(client.vtbl.GetService(client, &w.IID_IAudioRenderClient, &raw))) return error.ActivationFailed;
    const rc: *w.IAudioRenderClient = @ptrCast(@alignCast(raw.?));
    errdefer rc.release();
    if (w.failed(client.vtbl.Start(client))) return error.ActivationFailed;
    slot.* = .{
        .in_use = true,
        .client = client,
        .render = rc,
        .event = event,
        .buffer_frames = buf_frames,
        .channels = spec.channels,
        .mix_rate = slot.mix_rate,
    };
    return .{ .ptr = slot, .vtable = &render_vtable };
}

fn renderWait(ptr: *anyopaque, timeout_ms: u32) bool {
    const r: *Render = @ptrCast(@alignCast(ptr));
    return w.WaitForSingleObject(r.event.?, timeout_ms) == w.WAIT_OBJECT_0;
}

fn renderAvailable(ptr: *anyopaque) Backend.Error!u32 {
    const r: *Render = @ptrCast(@alignCast(ptr));
    var padding: u32 = 0;
    if (w.failed(r.client.?.vtbl.GetCurrentPadding(r.client.?, &padding))) return error.ActivationFailed;
    return r.buffer_frames - @min(padding, r.buffer_frames);
}

fn renderWrite(ptr: *anyopaque, frames: []const f32) Backend.Error!void {
    const r: *Render = @ptrCast(@alignCast(ptr));
    const n: u32 = @intCast(frames.len / r.channels);
    if (n == 0) return;
    var data: ?[*]u8 = null;
    if (w.failed(r.render.?.vtbl.GetBuffer(r.render.?, n, &data))) return error.ActivationFailed;
    // Silence flag: the engine skips the mix for this packet. Cheap scan;
    // the paused loop writes zeros every period.
    const silent = std.mem.allEqual(f32, frames, 0);
    if (!silent) @memcpy(data.?[0 .. frames.len * @sizeOf(f32)], std.mem.sliceAsBytes(frames));
    const flags: u32 = if (silent) w.AUDCLNT_BUFFERFLAGS_SILENT else 0;
    if (w.failed(r.render.?.vtbl.ReleaseBuffer(r.render.?, n, flags))) return error.ActivationFailed;
}

fn renderStop(ptr: *anyopaque) void {
    const r: *Render = @ptrCast(@alignCast(ptr));
    if (r.client) |c| _ = c.vtbl.Stop(c);
}

fn renderDeinit(ptr: *anyopaque) void {
    const r: *Render = @ptrCast(@alignCast(ptr));
    if (r.client) |c| _ = c.vtbl.Stop(c);
    if (r.render) |rc| rc.release();
    if (r.client) |c| c.release();
    if (r.event) |e| _ = w.CloseHandle(e);
    r.* = .{};
    w.CoUninitialize();
}

fn renderMixRate(ptr: *anyopaque) u32 {
    const r: *Render = @ptrCast(@alignCast(ptr));
    return r.mix_rate;
}
```

Verify: `std.mem.allEqual(T, slice, scalar)` exists in the pinned std (present since 0.11). If it moved, write the four-line loop instead.

In `enumerate` (`WasapiBackend.zig:74-76`) add a third line so render endpoints appear once as loopback candidates and once as outputs:

```zig
    // Loopback devices are the RENDER endpoints; inputs are the CAPTURE endpoints.
    n += listFlow(en, w.eRender, .loopback, out[n..]);
    n += listFlow(en, w.eCapture, .input, out[n..]);
    // The same render endpoints again, as playback outputs. One endpoint,
    // two roles; two rows keeps the Python filters one-liners.
    n += listFlow(en, w.eRender, .render, out[n..]);
```

Update the file's header comment (`WasapiBackend.zig:1-6`): append one sentence — "Render (PR e) is event-driven: the loopback quirk does not apply to a real output stream."

- [ ] **Step 3: Verify build on both leg shapes, count +1**

Run: `zig build --build-file core/build.zig test --summary all` → count = d's end count + 5.
Run: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe`
Run: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu`
Run: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=aarch64-macos`
Run: `zig fmt --check core/build.zig core/src`
Expected: all green.

Mutation check: change `renderFormat`'s `32` to `16` → the test reddens. Revert.

- [ ] **Step 4: Commit**

```bash
git add core/src/WasapiBackend.zig
git commit -m "feat(core): WasapiBackend.openRender (event-driven shared-mode render) + render endpoints in enumerate"
```

---

### Task 4: `Playback.zig` — the render thread

**Files:**
- Create: `core/src/Playback.zig`
- Modify: `core/src/root.zig` (add `pub const Playback = @import("Playback.zig");` after `Capture`, `root.zig:14`)

**Interfaces:**
- Consumes: `Backend.*` (`openRender`, `RenderStream`), `FakeBackend` (tests only). Never imports `wasapi.zig`.
- Produces:
  ```zig
  pub const Playback = @This();
  pub const State = extern struct { running: u8, playing: u8, cursor: u64, clip_frames: u64, mix_rate: u32 };
  pub const max_device_id = 256;
  pub const max_error = 256;
  pub const max_fill_frames = 8192;                 // one write per wake, capped
  pub fn init(allocator: std.mem.Allocator, backend: Backend.Backend, spec: Backend.Spec) Playback
  pub fn deinit(self: *Playback) void               // stop + free clip
  pub fn bind(self: *Playback, frames: []const f32, rate: u32, channels: u16) !void  // error.InvalidArgument / error.OutOfMemory
  pub fn play(self: *Playback) !void                // spawns the thread on first call; error from Thread.spawn
  pub fn pause(self: *Playback) void
  pub fn seek(self: *Playback, frames: u64) void    // clamps to [0, clip_frames]
  pub fn setDevice(self: *Playback, id: []const u8) void
  pub fn stop(self: *Playback) void                 // joins; idempotent
  pub fn state(self: *const Playback) State
  pub fn lastError(self: *const Playback) [:0]const u8
  ```
  Thread loop (the whole of it):
  1. `stream = backend.openRender(currentSpec())` — on error: `"open failed: {s}"`, `done = true`, return.
  2. `mix_rate.store`, `running = true`.
  3. `while (!stop_flag)`: if `reopen.swap(false)`: stop+deinit the stream, `openRender` again (error → record, break). `if (!stream.wait(100)) continue`. `want = min(stream.available(), max_fill_frames)`; `if (want == 0) continue`. `fill(want)` into `scratch`; `stream.write(scratch[0 .. want * channels])`.
  4. `stream.stop(); stream.deinit(); running = false; done = true`.
  `fill`: `in_copy = true`; if `!playing` → zeros; else `n = min(want, clip_frames - cursor)`; copy `n` frames; zero-pad; `cursor += n`; if `cursor == clip_frames` → `playing = false`; `in_copy = false`.

- [ ] **Step 1: Failing tests**

Create `core/src/Playback.zig` with the header, fields, `waitUntil`, and these tests (no function bodies yet):

```zig
//! One clip player: a Zig-owned render thread feeding a
//! Backend.RenderStream from an owned copy of the clip. Python binds,
//! plays, pauses, seeks, and reads State; it never sees a frame. The
//! thread never locks or allocates — the clip is allocated in bind() on
//! the control thread, and a two-flag handshake (playing + in_copy)
//! keeps bind() from freeing a buffer the thread is copying from.
const std = @import("std");
const Backend = @import("Backend.zig");
const FakeBackend = @import("FakeBackend.zig");
const Playback = @This();

pub const State = extern struct { running: u8, playing: u8, cursor: u64, clip_frames: u64, mix_rate: u32 };
pub const max_device_id = 256;
pub const max_error = 256;
/// Largest single write. WASAPI's shared-mode buffer at Initialize(0, 0)
/// is a few thousand frames; a bigger `available()` is filled over
/// several wakes rather than sized dynamically.
pub const max_fill_frames = 8192;

allocator: std.mem.Allocator,
backend: Backend.Backend,
rate: u32,
channels: u16,
id_buf: [max_device_id]u8,
id_len: usize,
clip: []f32 = &.{},
clip_frames: std.atomic.Value(u64) = std.atomic.Value(u64).init(0),
cursor: std.atomic.Value(u64) = std.atomic.Value(u64).init(0),
playing: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
in_copy: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
reopen: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
thread: ?std.Thread = null,
stop_flag: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
running: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
done: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
mix_rate: std.atomic.Value(u32) = std.atomic.Value(u32).init(0),
scratch: [max_fill_frames * 2]f32 = undefined,
err_buf: [max_error]u8 = [_]u8{0} ** max_error,
err_len: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),

fn waitUntil(pb: *Playback, comptime pred: fn (*Playback) bool) !void {
    var spins: u32 = 0;
    while (!pred(pb) and spins < 5_000_000) : (spins += 1) std.Thread.yield() catch {};
    if (!pred(pb)) return error.Timeout;
}

fn ramp(comptime n: usize) [n * 2]f32 {
    var out: [n * 2]f32 = undefined;
    for (0..n) |i| {
        out[i * 2] = @floatFromInt(i + 1);
        out[i * 2 + 1] = -@as(f32, @floatFromInt(i + 1));
    }
    return out;
}

const test_spec = Backend.Spec{ .kind = .render, .device_id = "", .rate = 48_000, .channels = 2 };

test "partial tail zero-pads the last write and auto-stops with the cursor at clip end" {
    var fake = FakeBackend.init(&.{});
    fake.render_allocator = std.testing.allocator;
    defer fake.deinitRender();
    fake.render_available = 4;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const clip = ramp(6);
    try pb.bind(&clip, 48_000, 2);
    try pb.play();
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            return !p.playing.load(.acquire) and p.cursor.load(.acquire) == 6;
        }
    }.f);
    pb.stop();
    try std.testing.expect(fake.written.items.len >= 16);
    try std.testing.expectEqualSlices(f32, &clip, fake.written.items[0..12]);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0, 0, 0, 0 }, fake.written.items[12..16]);
    const st = pb.state();
    try std.testing.expectEqual(@as(u8, 0), st.playing);
    try std.testing.expectEqual(@as(u64, 6), st.cursor);
    try std.testing.expectEqual(@as(u64, 6), st.clip_frames);
}

test "paused: writes are zeros and the cursor does not move" {
    var fake = FakeBackend.init(&.{});
    fake.render_allocator = std.testing.allocator;
    defer fake.deinitRender();
    fake.render_available = 2;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const clip = ramp(100);
    try pb.bind(&clip, 48_000, 2);
    try pb.play();
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            return p.cursor.load(.acquire) >= 2;
        }
    }.f);
    pb.pause();
    const at = pb.cursor.load(.acquire);
    const writes = fake.render_writes.load(.acquire);
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            _ = p;
            return false;
        }
    }.f) catch {}; // burn ~5M yields: several fake periods pass
    try std.testing.expect(fake.render_writes.load(.acquire) > writes);
    try std.testing.expectEqual(at, pb.cursor.load(.acquire));
    pb.stop();
    const tail = fake.written.items[fake.written.items.len - 4 ..];
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0, 0, 0, 0 }, tail);
}

test "seek past end clamps to clip_frames; play at end rewinds to 0" {
    var fake = FakeBackend.init(&.{});
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const clip = ramp(10);
    try pb.bind(&clip, 48_000, 2);
    pb.seek(500);
    try std.testing.expectEqual(@as(u64, 10), pb.state().cursor);
    pb.seek(3);
    try std.testing.expectEqual(@as(u64, 3), pb.state().cursor);
    pb.seek(10);
    fake.render_available = 0; // thread wakes but never writes; cursor stays observable
    try pb.play();
    try std.testing.expectEqual(@as(u64, 0), pb.state().cursor);
    try std.testing.expectEqual(@as(u8, 1), pb.state().playing);
    pb.stop();
}

test "bind while playing pauses, resets the cursor, and replaces the clip" {
    var fake = FakeBackend.init(&.{});
    fake.render_allocator = std.testing.allocator;
    defer fake.deinitRender();
    fake.render_available = 1;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const a = ramp(1000);
    try pb.bind(&a, 48_000, 2);
    try pb.play();
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            return p.cursor.load(.acquire) >= 5;
        }
    }.f);
    const b = ramp(3);
    try pb.bind(&b, 48_000, 2);
    try std.testing.expectEqual(@as(u8, 0), pb.state().playing);
    try std.testing.expectEqual(@as(u64, 0), pb.state().cursor);
    try std.testing.expectEqual(@as(u64, 3), pb.state().clip_frames);
    try std.testing.expectEqualSlices(f32, &b, pb.clip);
    pb.stop();
}

test "rebind at a new rate reopens the stream on the render thread with the new spec" {
    var fake = FakeBackend.init(&.{});
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const a = ramp(4);
    try pb.bind(&a, 48_000, 2);
    try pb.play();
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            return p.running.load(.acquire);
        }
    }.f);
    try std.testing.expectEqual(@as(u32, 1), fake.render_opens.load(.acquire));
    try pb.bind(&a, 96_000, 2);
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            return !p.reopen.load(.acquire);
        }
    }.f);
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            return p.running.load(.acquire);
        }
    }.f);
    pb.stop();
    try std.testing.expectEqual(@as(u32, 2), fake.render_opens.load(.acquire));
    try std.testing.expectEqual(@as(u32, 96_000), fake.last_render_spec.?.rate);
    // Same rate again: no reopen.
    try pb.bind(&a, 96_000, 2);
    try std.testing.expect(!pb.reopen.load(.acquire));
}

test "setDevice copies the id, sets reopen, and the new id reaches the backend" {
    var fake = FakeBackend.init(&.{});
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    pb.setDevice("{hp}");
    try std.testing.expect(pb.reopen.load(.acquire));
    const a = ramp(4);
    try pb.bind(&a, 48_000, 2);
    try pb.play();
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            return p.running.load(.acquire) and !p.reopen.load(.acquire);
        }
    }.f);
    pb.stop();
    try std.testing.expectEqualStrings("{hp}", fake.last_render_spec.?.device_id);
}

test "available() == 0 does not spin into zero-length writes" {
    var fake = FakeBackend.init(&.{});
    fake.render_available = 0;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const a = ramp(4);
    try pb.bind(&a, 48_000, 2);
    try pb.play();
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            _ = p;
            return false;
        }
    }.f) catch {};
    try std.testing.expect(fake.render_waits.load(.acquire) > 100);
    try std.testing.expectEqual(@as(usize, 0), fake.render_writes.load(.acquire));
    try std.testing.expect(pb.running.load(.acquire));
    pb.stop();
}

test "openRender failure lands in lastError, running stays false, and the next play retries" {
    var fake = FakeBackend.init(&.{});
    fake.render_open_error = error.DeviceNotFound;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const a = ramp(4);
    try pb.bind(&a, 48_000, 2);
    try pb.play();
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            return p.done.load(.acquire);
        }
    }.f);
    try std.testing.expectEqualStrings("open failed: DeviceNotFound", pb.lastError());
    try std.testing.expectEqual(@as(u8, 0), pb.state().running);
    fake.render_open_error = null;
    try pb.play();
    try waitUntil(&pb, struct {
        fn f(p: *Playback) bool {
            return p.running.load(.acquire);
        }
    }.f);
    try std.testing.expectEqualStrings("", pb.lastError());
    pb.stop();
}

test "bind rejects a bad channel count, a zero rate, and a ragged frame slice" {
    var fake = FakeBackend.init(&.{});
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const a = ramp(4);
    try std.testing.expectError(error.InvalidArgument, pb.bind(&a, 48_000, 3));
    try std.testing.expectError(error.InvalidArgument, pb.bind(&a, 0, 2));
    try std.testing.expectError(error.InvalidArgument, pb.bind(a[0..3], 48_000, 2));
    try std.testing.expectEqual(@as(u64, 0), pb.state().clip_frames);
}
```

Add to `core/src/root.zig` after line 14: `pub const Playback = @import("Playback.zig");`

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test`
Expected: compile error (`init`/`bind`/`play`/… missing).

- [ ] **Step 3: Implement**

```zig
pub fn init(allocator: std.mem.Allocator, backend: Backend.Backend, spec: Backend.Spec) Playback {
    var self = Playback{
        .allocator = allocator,
        .backend = backend,
        .rate = spec.rate,
        .channels = spec.channels,
        .id_buf = undefined,
        .id_len = 0,
    };
    // Own the id bytes — the caller's slice is a Python str via ctypes
    // and is gone before the thread reads it. Fixed buffer, no allocator.
    const n = @min(spec.device_id.len, max_device_id - 1);
    @memcpy(self.id_buf[0..n], spec.device_id[0..n]);
    self.id_buf[n] = 0;
    self.id_len = n;
    return self;
}

pub fn deinit(self: *Playback) void {
    self.stop();
    self.allocator.free(self.clip);
    self.clip = &.{};
}

fn currentSpec(self: *const Playback) Backend.Spec {
    return .{ .kind = .render, .device_id = self.id_buf[0..self.id_len], .rate = self.rate, .channels = self.channels };
}

/// Control thread. The ONLY place the clip is allocated or freed.
pub fn bind(self: *Playback, frames: []const f32, rate: u32, channels: u16) !void {
    if (channels == 0 or channels > 2 or rate == 0 or frames.len % channels != 0) return error.InvalidArgument;
    // Handshake with fill(): clear `playing`, then wait for any copy in
    // flight. fill() raises in_copy BEFORE it reads `playing`, so once we
    // observe in_copy == false after storing playing = false, no copy can
    // start on the old clip. seq_cst on both sides makes the two stores
    // and two loads globally ordered (Dekker's pattern).
    self.playing.store(false, .seq_cst);
    while (self.in_copy.load(.seq_cst)) std.Thread.yield() catch {};
    const copy = try self.allocator.dupe(f32, frames);
    self.allocator.free(self.clip);
    self.clip = copy;
    self.cursor.store(0, .release);
    self.clip_frames.store(frames.len / channels, .release);
    if (rate != self.rate or channels != self.channels) {
        self.rate = rate;
        self.channels = channels;
        // The thread reopens the stream at the new format on its next
        // wake; no stream is opened here (bind may run before play).
        self.reopen.store(true, .release);
    }
}

pub fn play(self: *Playback) !void {
    // A thread that exited (open failed, stream error) is joined here so
    // the next play retries the open instead of silently doing nothing.
    if (self.thread) |t| {
        if (self.done.load(.acquire)) {
            t.join();
            self.thread = null;
        }
    }
    const total = self.clip_frames.load(.acquire);
    if (self.cursor.load(.acquire) >= total) self.cursor.store(0, .release);
    self.playing.store(total > 0, .seq_cst);
    if (self.thread == null) {
        self.stop_flag.store(false, .monotonic);
        self.done.store(false, .monotonic);
        self.err_len.store(0, .monotonic);
        self.thread = try std.Thread.spawn(.{}, run, .{self});
    }
}

pub fn pause(self: *Playback) void {
    self.playing.store(false, .seq_cst);
}

pub fn seek(self: *Playback, frames: u64) void {
    self.cursor.store(@min(frames, self.clip_frames.load(.acquire)), .release);
}

pub fn setDevice(self: *Playback, id: []const u8) void {
    const n = @min(id.len, max_device_id - 1);
    @memcpy(self.id_buf[0..n], id[0..n]);
    self.id_buf[n] = 0;
    self.id_len = n;
    self.reopen.store(true, .release);
}

pub fn stop(self: *Playback) void {
    const t = self.thread orelse return;
    self.stop_flag.store(true, .release);
    t.join();
    self.thread = null;
}

pub fn state(self: *const Playback) State {
    return .{
        .running = @intFromBool(self.running.load(.acquire)),
        .playing = @intFromBool(self.playing.load(.acquire)),
        .cursor = self.cursor.load(.acquire),
        .clip_frames = self.clip_frames.load(.acquire),
        .mix_rate = self.mix_rate.load(.acquire),
    };
}

pub fn lastError(self: *const Playback) [:0]const u8 {
    const n = self.err_len.load(.acquire);
    return self.err_buf[0..n :0];
}

fn setError(self: *Playback, comptime fmt: []const u8, args: anytype) void {
    const s = std.fmt.bufPrintZ(self.err_buf[0..], fmt, args) catch self.err_buf[0 .. max_error - 1 :0];
    self.err_len.store(s.len, .release);
}

/// Render thread. Produces `want` frames into scratch. Never allocates.
fn fill(self: *Playback, want: usize) []const f32 {
    const ch: usize = self.channels;
    const out = self.scratch[0 .. want * ch];
    self.in_copy.store(true, .seq_cst);
    defer self.in_copy.store(false, .seq_cst);
    if (!self.playing.load(.seq_cst)) {
        @memset(out, 0);
        return out;
    }
    const total = self.clip_frames.load(.acquire);
    const at = self.cursor.load(.acquire);
    const n = @min(want, total - @min(at, total));
    const src = self.clip[at * ch .. (at + n) * ch];
    @memcpy(out[0 .. n * ch], src);
    @memset(out[n * ch ..], 0);
    self.cursor.store(at + n, .release);
    // Auto-stop, no loop: the UI re-calls play() for LOOP, as today.
    if (at + n >= total) self.playing.store(false, .seq_cst);
    return out;
}

fn run(self: *Playback) void {
    defer self.done.store(true, .release);
    var stream: ?Backend.RenderStream = self.backend.openRender(self.currentSpec()) catch |e| {
        self.setError("open failed: {s}", .{@errorName(e)});
        return;
    };
    // An open consumed the pending flag: a reopen requested before the
    // first open is already satisfied.
    self.reopen.store(false, .release);
    self.mix_rate.store(stream.?.mixRate(), .release);
    self.running.store(true, .release);
    defer self.running.store(false, .release);
    defer if (stream) |s| {
        s.stop();
        s.deinit();
    };
    while (!self.stop_flag.load(.acquire)) {
        if (self.reopen.swap(false, .acq_rel)) {
            // Reopen on THIS thread: the backend's COM apartment belongs
            // to the thread that opened the stream.
            if (stream) |s| {
                s.stop();
                s.deinit();
            }
            stream = null;
            self.running.store(false, .release);
            stream = self.backend.openRender(self.currentSpec()) catch |e| {
                self.setError("open failed: {s}", .{@errorName(e)});
                return;
            };
            self.mix_rate.store(stream.?.mixRate(), .release);
            self.running.store(true, .release);
        }
        const s = stream.?;
        if (!s.wait(100)) continue;
        const avail = s.available() catch |e| {
            self.setError("stream failed: {s}", .{@errorName(e)});
            return;
        };
        const want: usize = @min(avail, max_fill_frames);
        if (want == 0) continue;
        s.write(self.fill(want)) catch |e| {
            self.setError("stream failed: {s}", .{@errorName(e)});
            return;
        };
    }
}
```

Note the `stream` optional: after a failed reopen `stream` is `null`, so the deferred `stop`/`deinit` runs only on a live stream. `done` is stored last (first `defer` declared = last run), after `running` is false, so `play()`'s join-on-done sees a fully torn-down thread.

- [ ] **Step 4: Run, verify green ×3, count +9**

Run: `zig build --build-file core/build.zig test --summary all` three times (threads).
Expected: pass ×3; count = d's end count + 14. `zig fmt --check core/src` clean.
Run: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu` — green (Playback is OS-neutral).

Mutation checks (one per test, edit-then-revert on the real source):
- "partial tail": drop `@memset(out[n * ch ..], 0)` → the `[12..16]` assertion sees stale scratch → red. Drop `if (at + n >= total) playing = false` → `waitUntil` times out → red.
- "paused": in `fill`, replace `if (!self.playing...)` with `if (false)` → cursor moves → red.
- "seek/rewind": in `seek`, remove the `@min` → 500 stays → red. In `play`, remove the rewind → cursor stays 10 → red.
- "bind while playing": remove `self.playing.store(false, .seq_cst)` from `bind` → `playing` reads 1 → red.
- "rebind reopens": remove `self.reopen.store(true, .release)` from `bind` → `render_opens` stays 1 → red. Remove the `rate != self.rate` condition (always reopen) → the "same rate again" assertion → red.
- "setDevice": remove the `@memcpy` in `setDevice` → id mismatch → red.
- "available()==0": delete `if (want == 0) continue;` → writes > 0 → red.
- "openRender failure": in `play`, delete the join-on-done block → the second `play()` never spawns → `waitUntil(running)` times out → red.
- "bind rejects": delete `frames.len % channels != 0` from the guard → the ragged case returns ok → red.

- [ ] **Step 5: Commit**

```bash
git add core/src/Playback.zig core/src/root.zig
git commit -m "feat(core): Playback.zig — Zig-owned render thread over Backend.RenderStream, tested on FakeBackend"
```

---

### Task 5: ABI + header + `native.py` declarations

**Files:**
- Modify: `core/src/abi.zig`, `core/include/flashback_core.h`, `flashback_sampler/core/native.py`, `tests/unit/test_native_capture.py`

**Interfaces:**
- Produces (C ABI, mirrored in `native.py._declare`):
  ```c
  typedef struct FbPlayback FbPlayback; /* opaque */
  typedef struct FbPlaybackState { uint8_t running; uint8_t playing; uint64_t cursor; uint64_t clip_frames; uint32_t mix_rate; } FbPlaybackState;
  FbPlayback *fb_playback_create(const char *device_id, uint32_t rate, uint16_t channels); /* NULL on non-Windows or bad args */
  FbStatus    fb_playback_bind(FbPlayback *, const float *frames, size_t n_frames, uint32_t rate, uint16_t channels); /* FB_INVALID_ARG, FB_OUT_OF_MEMORY */
  FbStatus    fb_playback_play(FbPlayback *);           /* FB_IO_ERROR if the thread could not spawn */
  void        fb_playback_pause(FbPlayback *);
  void        fb_playback_seek(FbPlayback *, uint64_t frames);
  void        fb_playback_set_device(FbPlayback *, const char *device_id);
  void        fb_playback_state(const FbPlayback *, FbPlaybackState *out);
  const char *fb_playback_last_error(const FbPlayback *);  /* "" when none; valid until destroy */
  void        fb_playback_destroy(FbPlayback *);            /* stops first, frees the clip */
  ```
  `fb_devices_list` now also returns `kind == 3` rows (render endpoints). Python: `KIND_INTS["render"] = 3`; `list_devices` reports `"render"`.

- [ ] **Step 1: Failing Zig ABI tests**

Append to `core/src/abi.zig` (add `const Playback = @import("Playback.zig");` to the imports at `:6-12`):

```zig
test "fb_playback_create rejects rate 0 and channels 3" {
    try std.testing.expectEqual(@as(?*Playback, null), fb_playback_create("", 0, 2));
    try std.testing.expectEqual(@as(?*Playback, null), fb_playback_create("", 48_000, 3));
}

test "fb_playback bind/state/last_error on a never-played handle (Windows only)" {
    if (builtin.os.tag != .windows) return error.SkipZigTest;
    const pb = fb_playback_create("", 48_000, 2) orelse return error.CreateFailed;
    defer fb_playback_destroy(pb);
    var st: Playback.State = undefined;
    fb_playback_state(pb, &st);
    try std.testing.expectEqual(@as(u8, 0), st.running);
    try std.testing.expectEqual(@as(u64, 0), st.clip_frames);
    const frames = [_]f32{ 0.1, -0.1, 0.2, -0.2, 0.3, -0.3 };
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_playback_bind(pb, &frames, 3, 48_000, 3));
    try std.testing.expectEqual(FbStatus.ok, fb_playback_bind(pb, &frames, 3, 44_100, 2));
    fb_playback_seek(pb, 99);
    fb_playback_state(pb, &st);
    try std.testing.expectEqual(@as(u64, 3), st.clip_frames);
    try std.testing.expectEqual(@as(u64, 3), st.cursor);
    try std.testing.expectEqualStrings("", std.mem.span(fb_playback_last_error(pb)));
}

test "fb_capture_create rejects kind 3: render is not a capture kind" {
    const ring = fb_ring_create(48_000, 2, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    try std.testing.expectEqual(@as(?*Capture, null), fb_capture_create(ring, &.{ .kind = 3, .pid = 0, .rate = 48_000, .channels = 2, .device_id = "" }));
}
```

(`fb_ring_create` carries PR d's `status` parameter — pass `null`.)

- [ ] **Step 2: Run, verify red**

Run: `zig build --build-file core/build.zig test` → compile error (exports missing).

- [ ] **Step 3: Implement the exports**

Append to `core/src/abi.zig`:

```zig
export fn fb_playback_create(device_id: [*:0]const u8, rate: u32, channels: u16) ?*Playback {
    if (rate == 0 or channels == 0 or channels > 2) return null;
    const be = nativeBackend() orelse return null;
    const pb = allocator.create(Playback) catch return null;
    pb.* = Playback.init(allocator, be, .{ .kind = .render, .device_id = std.mem.span(device_id), .rate = rate, .channels = channels });
    return pb;
}

export fn fb_playback_bind(pb: *Playback, frames: [*]const f32, n_frames: usize, rate: u32, channels: u16) FbStatus {
    if (channels == 0) return .invalid_arg;
    pb.bind(frames[0 .. n_frames * channels], rate, channels) catch |e| return switch (e) {
        error.InvalidArgument => .invalid_arg,
        error.OutOfMemory => .out_of_memory,
    };
    return .ok;
}

export fn fb_playback_play(pb: *Playback) FbStatus {
    pb.play() catch return .io_error;
    return .ok;
}

export fn fb_playback_pause(pb: *Playback) void {
    pb.pause();
}

export fn fb_playback_seek(pb: *Playback, frames: u64) void {
    pb.seek(frames);
}

export fn fb_playback_set_device(pb: *Playback, device_id: [*:0]const u8) void {
    pb.setDevice(std.mem.span(device_id));
}

export fn fb_playback_state(pb: *const Playback, out: *Playback.State) void {
    out.* = pb.state();
}

export fn fb_playback_last_error(pb: *const Playback) [*:0]const u8 {
    return pb.lastError().ptr;
}

export fn fb_playback_destroy(pb: *Playback) void {
    pb.deinit();
    allocator.destroy(pb);
}
```

Verify: `pb.bind`'s error set is exactly `error{ InvalidArgument, OutOfMemory }` (`dupe` returns `Allocator.Error` = `OutOfMemory`). If the compiler reports an unhandled error, add it to the switch — do not add `else`.

Update `core/include/flashback_core.h`: add the two typedefs after `FbProcess` (`:25`) and the nine prototypes after `fb_processes_list` (`:69`), as in the Interfaces block.

- [ ] **Step 4: Run Zig, verify green, count +3; rebuild the DLL**

Run: `zig build --build-file core/build.zig test --summary all` → count = d's end count + 17.
Run: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe`
Run: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu`

Mutation check: change `fb_playback_create`'s guard to `channels > 3` → the first test reddens. Change `fb_capture_create`'s guard (`abi.zig:346`) to `spec.kind > 3` → the kind-3 test reddens. Revert both.

- [ ] **Step 5: Failing Python test (kind 3 in `list_devices`)**

Append to `tests/unit/test_native_capture.py`:

```python
def test_list_devices_maps_render_kind(lib):
    lib.devices = [(3, 1, 48_000, 2, "{spk}", "Speakers")]
    got = native.list_devices()
    assert got == [{"kind": "render", "is_default": True, "mix_rate": 48_000, "mix_channels": 2, "id": "{spk}", "name": "Speakers"}]
```

Run: `python -m pytest tests/unit/test_native_capture.py -q` → FAIL (`kind == "input"` — the `_KIND_NAMES.get(d.kind, "input")` fallback at `native.py:183`).

- [ ] **Step 6: Implement `native.py`**

Replace `native.py:82`:

```python
KIND_INTS = {"loopback": 0, "input": 1, "process": 2, "render": 3}
```

After `FbProcess` (`native.py:101-102`) add:

```python
class FbPlaybackState(C.Structure):
    _fields_ = [("running", C.c_uint8), ("playing", C.c_uint8), ("cursor", C.c_uint64),
                ("clip_frames", C.c_uint64), ("mix_rate", C.c_uint32)]
```

At the end of `_declare` (after `native.py:170`) add:

```python
    lib.fb_playback_create.argtypes = [C.c_char_p, C.c_uint32, C.c_uint16]
    lib.fb_playback_create.restype = C.c_void_p
    lib.fb_playback_bind.argtypes = [C.c_void_p, f32p, C.c_size_t, C.c_uint32, C.c_uint16]
    lib.fb_playback_bind.restype = C.c_int
    lib.fb_playback_play.argtypes = [C.c_void_p]
    lib.fb_playback_play.restype = C.c_int
    lib.fb_playback_pause.argtypes = [C.c_void_p]
    lib.fb_playback_pause.restype = None
    lib.fb_playback_seek.argtypes = [C.c_void_p, C.c_uint64]
    lib.fb_playback_seek.restype = None
    lib.fb_playback_set_device.argtypes = [C.c_void_p, C.c_char_p]
    lib.fb_playback_set_device.restype = None
    lib.fb_playback_state.argtypes = [C.c_void_p, C.POINTER(FbPlaybackState)]
    lib.fb_playback_state.restype = None
    lib.fb_playback_last_error.argtypes = [C.c_void_p]
    lib.fb_playback_last_error.restype = C.c_char_p
    lib.fb_playback_destroy.argtypes = [C.c_void_p]
    lib.fb_playback_destroy.restype = None
```

Add `_OUT_OF_MEMORY = 5` after `native.py:31` if PR d did not already (grep first). Update the `list_devices` docstring (`native.py:174-176`): "render endpoints appear twice: as kind="loopback" (capture candidate) and kind="render" (playback output)".

- [ ] **Step 7: Run Python, verify green**

Run: `python -m pytest tests/unit/test_native_capture.py tests/unit/test_native_smoke.py -q` → pass.

- [ ] **Step 8: Commit**

```bash
git add core/src/abi.zig core/include/flashback_core.h flashback_sampler/core/native.py tests/unit/test_native_capture.py
git commit -m "feat(core): playback ABI, header, ctypes bindings; render kind in list_devices"
```

---

### Task 6: `NativeScrubPlayer` + wrapper tests

**Files:**
- Rewrite: `flashback_sampler/core/scrub_player.py`
- Replace: `tests/unit/test_scrub_player.py`
- Modify: `tests/unit/test_app_state.py` (`:16`, `:26`, `:31-32`, `:395-412`)

**Interfaces:**
- Produces:
  ```python
  class NativeScrubPlayer:
      def __init__(self, sample_rate: int = 48_000, channels: int = 2, device: str = "")
      sample_rate: int; channels: int; device: str   # sample_rate/channels follow the last bind
      def bind(self, audio: np.ndarray, sample_rate: int) -> None   # 1-D → [N, 1]; ValueError / MemoryError from status
      def play(self) -> None; def pause(self) -> None; def stop(self) -> None   # stop = pause + seek_samples(0)
      def seek_samples(self, pos: int) -> None; def seek(self, seconds: float) -> None
      def set_device(self, device: str) -> None
      cursor_samples: int; cursor_seconds: float; is_playing: bool; source_length_samples: int   # properties
      def last_error(self) -> str | None
      def close(self) -> None   # destroys; idempotent
  ```
  No `open()` (Plan choice 2). No `sounddevice` import anywhere in the module.

- [ ] **Step 1: Failing tests**

Replace `tests/unit/test_scrub_player.py` with:

```python
"""NativeScrubPlayer over a FAKE ctypes library. No DLL, no device: every
fb_playback_* symbol is a Python stub that records calls and serves a
scripted state. The fill logic lives in Zig (core/src/Playback.zig) and
is tested there."""
import ctypes as C

import numpy as np
import pytest

from flashback_sampler.core import native
from flashback_sampler.core.scrub_player import NativeScrubPlayer


class _FakePlaybackLib:
    def __init__(self):
        self.calls = []
        self.state = (0, 0, 0, 0, 0)  # running, playing, cursor, clip_frames, mix_rate
        self.bind_status = 0
        self.play_status = 0
        self.err = b""
        self.bound = None  # (frames ndarray copy, n_frames, rate, channels)

    def __getattr__(self, name):
        def _fn(*a):
            self.calls.append((name, a))
            if name == "fb_playback_create":
                return 0xF00D
            if name == "fb_playback_bind":
                _h, ptr, n, rate, ch = a
                arr = np.ctypeslib.as_array(ptr, shape=(n * ch,)).copy() if n else np.zeros(0, np.float32)
                self.bound = (arr, n, rate, ch)
                if self.bind_status == 0:
                    self.state = self.state[:3] + (n, self.state[4])
                return self.bind_status
            if name == "fb_playback_play":
                return self.play_status
            if name == "fb_playback_state":
                st = a[1]._obj if hasattr(a[1], "_obj") else a[1]
                st.running, st.playing, st.cursor, st.clip_frames, st.mix_rate = self.state
            if name == "fb_playback_last_error":
                return self.err
            return None
        return _fn


@pytest.fixture
def lib(monkeypatch):
    fake = _FakePlaybackLib()
    monkeypatch.setattr(native, "_lib", fake)
    monkeypatch.setattr(native, "_lib_tried", True)
    return fake


def _calls(lib, name):
    return [a for n, a in lib.calls if n == name]


def test_create_passes_rate_channels_and_device(lib):
    NativeScrubPlayer(44_100, 1, device="{hp}")
    assert _calls(lib, "fb_playback_create") == [(b"{hp}", 44_100, 1)]


def test_create_without_library_raises(monkeypatch):
    monkeypatch.setattr(native, "_lib", None)
    monkeypatch.setattr(native, "_lib_tried", True)
    with pytest.raises(RuntimeError):
        NativeScrubPlayer()


def test_bind_passes_frames_rate_channels_and_updates_attributes(lib):
    p = NativeScrubPlayer(48_000, 2)
    audio = np.arange(6, dtype=np.float32).reshape(3, 2)
    p.bind(audio, 96_000)
    arr, n, rate, ch = lib.bound
    assert (n, rate, ch) == (3, 96_000, 2)
    np.testing.assert_array_equal(arr, audio.ravel())
    assert p.sample_rate == 96_000 and p.channels == 2
    assert p.source_length_samples == 3


def test_bind_reshapes_mono_1d_to_one_channel(lib):
    p = NativeScrubPlayer(48_000, 2)
    p.bind(np.zeros(4, dtype=np.float32), 48_000)
    assert lib.bound[1:] == (4, 48_000, 1)
    assert p.channels == 1


def test_bind_status_invalid_arg_raises_value_error(lib):
    lib.bind_status = native._INVALID_ARG
    with pytest.raises(ValueError):
        NativeScrubPlayer().bind(np.zeros((2, 3), dtype=np.float32), 48_000)


def test_bind_status_out_of_memory_raises_memory_error(lib):
    lib.bind_status = native._OUT_OF_MEMORY
    with pytest.raises(MemoryError):
        NativeScrubPlayer().bind(np.zeros((2, 2), dtype=np.float32), 48_000)


def test_play_pause_forward_and_play_failure_raises(lib):
    p = NativeScrubPlayer()
    p.play()
    p.pause()
    assert [n for n, _ in lib.calls[-2:]] == ["fb_playback_play", "fb_playback_pause"]
    lib.play_status = native._IO_ERROR
    lib.err = b"open failed: DeviceNotFound"
    with pytest.raises(RuntimeError, match="DeviceNotFound"):
        p.play()


def test_stop_is_pause_then_seek_zero(lib):
    p = NativeScrubPlayer()
    p.stop()
    assert [(n, a[1:]) for n, a in lib.calls[-2:]] == [("fb_playback_pause", ()), ("fb_playback_seek", (0,))]


def test_seek_samples_clamps_negative_to_zero_and_passes_through(lib):
    p = NativeScrubPlayer()
    p.seek_samples(-5)
    p.seek_samples(123)
    assert [a[1] for a in _calls(lib, "fb_playback_seek")] == [0, 123]


def test_seek_seconds_uses_the_bound_rate(lib):
    p = NativeScrubPlayer(48_000, 2)
    p.bind(np.zeros((10, 2), dtype=np.float32), 1_000)
    p.seek(0.25)
    assert _calls(lib, "fb_playback_seek")[-1][1] == 250


def test_state_properties_read_native_state(lib):
    p = NativeScrubPlayer(48_000, 2)
    p.bind(np.zeros((500, 2), dtype=np.float32), 1_000)
    lib.state = (1, 1, 250, 500, 48_000)
    assert p.is_playing is True
    assert p.cursor_samples == 250
    assert p.cursor_seconds == 0.25
    assert p.source_length_samples == 500
    lib.state = (1, 0, 500, 500, 48_000)
    assert p.is_playing is False


def test_set_device_passes_encoded_id(lib):
    p = NativeScrubPlayer()
    p.set_device("{spk}")
    assert _calls(lib, "fb_playback_set_device") == [(0xF00D, b"{spk}")]
    assert p.device == "{spk}"


def test_last_error_none_when_empty(lib):
    p = NativeScrubPlayer()
    assert p.last_error() is None
    lib.err = b"stream failed: ActivationFailed"
    assert p.last_error() == "stream failed: ActivationFailed"


def test_close_destroys_once_and_is_inert_after(lib):
    p = NativeScrubPlayer()
    p.close()
    p.close()
    assert len(_calls(lib, "fb_playback_destroy")) == 1
    p.pause()  # inert, no call
    assert not _calls(lib, "fb_playback_pause")
    assert p.is_playing is False and p.cursor_samples == 0
```

Run: `python -m pytest tests/unit/test_scrub_player.py -q` → FAIL (`NativeScrubPlayer` missing).

- [ ] **Step 2: Implement**

Replace `flashback_sampler/core/scrub_player.py` with:

```python
"""NativeScrubPlayer — the clip preview player on the Zig core.

Python holds a handle. The Zig render thread opens the WASAPI output,
fills it from an owned copy of the clip, and publishes cursor/playing
through atomics (core/src/Playback.zig). Nothing here touches frames.

`bind` hands the checkout's audio and rate across; the stream opens at
that rate and the OS resamples to the mix rate. Playback auto-stops at
the end of the clip (no loop); `play` at the end rewinds.
"""
from __future__ import annotations

import ctypes as C

import numpy as np

from flashback_sampler.core import native


class NativeScrubPlayer:
    def __init__(self, sample_rate: int = 48_000, channels: int = 2, device: str = ""):
        lib = native.load()
        if lib is None:
            raise RuntimeError("flashback_core library not available")
        self._lib = lib
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.device = device
        self._h = lib.fb_playback_create(device.encode("utf-8"), self.sample_rate, self.channels)
        if not self._h:
            raise RuntimeError("fb_playback_create failed (bad args, or no render backend on this OS)")

    # -- transport ------------------------------------------------------
    def bind(self, audio: np.ndarray, sample_rate: int) -> None:
        if audio.ndim == 1:
            audio = audio[:, np.newaxis]
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        n_frames, channels = audio.shape
        status = self._lib.fb_playback_bind(self._h, native._as_f32p(audio), n_frames, int(sample_rate), channels)
        if status == native._INVALID_ARG:
            raise ValueError(f"fb_playback_bind rejected {n_frames} frames x {channels} ch at {sample_rate} Hz")
        if status == native._OUT_OF_MEMORY:
            raise MemoryError(f"fb_playback_bind: could not allocate {audio.nbytes} bytes")
        if status != native._OK:
            raise RuntimeError(f"fb_playback_bind failed with status {status}")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)

    def play(self) -> None:
        if not self._h:
            return
        status = self._lib.fb_playback_play(self._h)
        if status != native._OK:
            raise RuntimeError(f"fb_playback_play failed with status {status}: {self.last_error() or ''}")

    def pause(self) -> None:
        if self._h:
            self._lib.fb_playback_pause(self._h)

    def stop(self) -> None:
        self.pause()
        self.seek_samples(0)

    def seek_samples(self, pos: int) -> None:
        if self._h:
            # The ABI takes u64; a negative int must not wrap on the wire.
            self._lib.fb_playback_seek(self._h, max(0, int(pos)))

    def seek(self, seconds: float) -> None:
        self.seek_samples(int(round(seconds * self.sample_rate)))

    def set_device(self, device: str) -> None:
        self.device = device
        if self._h:
            self._lib.fb_playback_set_device(self._h, device.encode("utf-8"))

    # -- state ----------------------------------------------------------
    @property
    def cursor_samples(self) -> int:
        return int(self._state().cursor)

    @property
    def cursor_seconds(self) -> float:
        return self.cursor_samples / float(self.sample_rate)

    @property
    def is_playing(self) -> bool:
        return bool(self._state().playing)

    @property
    def source_length_samples(self) -> int:
        return int(self._state().clip_frames)

    def last_error(self) -> str | None:
        if not self._h:
            return None
        raw = self._lib.fb_playback_last_error(self._h)
        return raw.decode("utf-8", "replace") if raw else None

    def close(self) -> None:
        if self._h:
            self._lib.fb_playback_destroy(self._h)
            self._h = None

    def _state(self) -> native.FbPlaybackState:
        st = native.FbPlaybackState()
        if self._h:
            self._lib.fb_playback_state(self._h, C.byref(st))
        return st

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
```

Update `tests/unit/test_app_state.py`:
- `:16` → `from flashback_sampler.core.scrub_player import NativeScrubPlayer`
- `:26` → `assert isinstance(st.scrub_player, NativeScrubPlayer)`
- `:395-412` → replace the body after `assert co.audio.shape == (500, 1)` with:

```python
    seen = {}
    st.scrub_player._lib = type("L", (), {
        "fb_playback_bind": staticmethod(lambda h, p, n, r, c: seen.update(n=n, rate=r, ch=c) or 0),
        "fb_playback_play": staticmethod(lambda h: seen.update(played=True) or 0),
    })()
    st.scrub_player.bind(co.audio, co.sample_rate)
    st.scrub_player.play()
    assert seen == dict(n=500, rate=1000, ch=1, played=True)
```

(`AppState` is still constructed on the real DLL; `state.py` is updated in Task 7 — until then `AppState.__init__` fails on `ScrubPlayer`. Run Task 6 and Task 7's Python gates together at the end of Task 7.)

- [ ] **Step 3: Run, verify green**

Run: `python -m pytest tests/unit/test_scrub_player.py -q` → 14 passed.

Mutation checks: remove `max(0, ...)` in `seek_samples` → the clamp test reddens (`-5` reaches the fake). Remove the 1-D reshape → the mono test raises on `audio.shape` unpack → red. Swap the `_INVALID_ARG`/`_OUT_OF_MEMORY` branches → both status tests redden. Revert.

- [ ] **Step 4: Commit**

```bash
git add flashback_sampler/core/scrub_player.py tests/unit/test_scrub_player.py tests/unit/test_app_state.py
git commit -m "feat(app): NativeScrubPlayer handle wrapper replaces the PortAudio ScrubPlayer; wrapper tests over a fake lib"
```

---

### Task 7: Output enumeration on the native list; `AppState` carries string ids

**Files:**
- Modify: `flashback_sampler/app/audio_devices.py`, `flashback_sampler/app/state.py`, `tests/unit/test_audio_devices.py`

**Interfaces:**
- `OutputDevice(id: str, name: str, max_output_channels: int, is_default: bool = False)` — `id` is the WASAPI endpoint string.
- `list_output_devices() -> list[OutputDevice]` from `native.list_devices()` rows with `kind == "render"`; `max_output_channels = mix_channels`.
- `default_output_device()` unchanged in shape.
- `AppState.scrub_player: NativeScrubPlayer`; `output_spec: OutputDevice | None` (string id); `set_output_spec(spec)` passes `spec.id` (str).
- The `sounddevice` import leaves `audio_devices.py`.

- [ ] **Step 1: Failing tests**

In `tests/unit/test_audio_devices.py` replace `:39-42` with:

```python
def test_output_device_is_frozen():
    d = OutputDevice(id="{spk}", name="Out", max_output_channels=2)
    with pytest.raises(Exception):
        d.id = "{hp}"  # type: ignore[misc]
```

Extend `_fake_devices()` (`:~145`) with two render rows and add two tests:

```python
def _fake_devices():
    return [
        {"kind": "loopback", "is_default": True, "mix_rate": 48_000, "mix_channels": 2, "id": "{spk}", "name": "Speakers"},
        {"kind": "loopback", "is_default": False, "mix_rate": 96_000, "mix_channels": 2, "id": "{hp}", "name": "Headphones"},
        {"kind": "input", "is_default": True, "mix_rate": 44_100, "mix_channels": 1, "id": "{mic}", "name": "Mic"},
        {"kind": "render", "is_default": True, "mix_rate": 48_000, "mix_channels": 2, "id": "{spk}", "name": "Speakers"},
        {"kind": "render", "is_default": False, "mix_rate": 96_000, "mix_channels": 6, "id": "{hp}", "name": "Headphones"},
    ]


def test_list_output_devices_maps_render_rows_only(monkeypatch):
    monkeypatch.setattr(audio_devices.native, "list_devices", _fake_devices)
    devs = audio_devices.list_output_devices()
    assert [(d.id, d.name, d.max_output_channels, d.is_default) for d in devs] == [
        ("{spk}", "Speakers", 2, True),
        ("{hp}", "Headphones", 6, False),
    ]
    assert all(isinstance(d.id, str) for d in devs)


def test_default_output_device_is_the_default_render_row(monkeypatch):
    monkeypatch.setattr(audio_devices.native, "list_devices", _fake_devices)
    assert audio_devices.default_output_device().id == "{spk}"


def test_capture_list_ignores_render_rows(monkeypatch):
    monkeypatch.setattr(audio_devices.native, "list_devices", _fake_devices)
    kinds = {d.kind for d in audio_devices.list_capture_devices()}
    assert kinds == {"loopback", "input"}


def test_audio_devices_does_not_import_sounddevice():
    import sys
    sys.modules.pop("sounddevice", None)
    import importlib
    importlib.reload(audio_devices)
    assert "sounddevice" not in sys.modules
```

Run: `python -m pytest tests/unit/test_audio_devices.py -q` → the new tests FAIL (`id=0` typed rows / sounddevice path).

- [ ] **Step 2: Implement `audio_devices.py`**

Replace `:74-80`:

```python
@dataclass(frozen=True)
class OutputDevice:
    """A WASAPI render endpoint used for preview playback. `id` is the
    endpoint id string; `""` means the live OS default output."""
    id: str
    name: str
    max_output_channels: int
    is_default: bool = False
```

Replace `list_output_devices` (`:117-153`):

```python
def list_output_devices() -> list[OutputDevice]:
    """Every active render endpoint, from the same native list the
    capture side reads (render endpoints appear there twice: once as a
    loopback candidate, once as an output)."""
    return [
        OutputDevice(
            id=d["id"],
            name=d["name"],
            max_output_channels=d["mix_channels"] or 2,
            is_default=d["is_default"],
        )
        for d in native.list_devices()
        if d["kind"] == "render"
    ]
```

Update the module docstring (`:1-13`): drop the "Preview output still goes through `sounddevice`" sentence. `default_output_device` (`:168-173`) is unchanged.

- [ ] **Step 3: Implement `state.py`**

- `:28` → `from flashback_sampler.core.scrub_player import NativeScrubPlayer`
- `:85-88` → `self.scrub_player = NativeScrubPlayer(sample_rate=int(sample_rate), channels=int(channels))`
- `:93-95` unchanged in text (`set_device(self.output_spec.id)` now passes a str).
- `:253-255` unchanged in text; the `OutputDevice` type now carries `id: str`.
- `shutdown` (`:434-437`) unchanged: `close()` destroys the handle (the Zig `deinit` joins the thread).

- [ ] **Step 4: Run Python gates, verify green**

Run: `python -m pytest tests/unit/test_audio_devices.py tests/unit/test_app_state.py tests/unit/test_scrub_player.py -q` → pass.
Run: `python -m pytest tests/unit -q -m "not audio_hw and not perf"` → pass except `tests/unit/test_turntable_window.py` may still be green (no test calls the play path yet); Task 8 changes the call sites.

Mutation check: in `list_output_devices`, change the filter to `d["kind"] == "loopback"` → the two output tests redden (loopback rows carry `mix_channels` 2/2, so the `6` assertion fails). Revert.

- [ ] **Step 5: Commit**

```bash
git add flashback_sampler/app/audio_devices.py flashback_sampler/app/state.py tests/unit/test_audio_devices.py
git commit -m "feat(app): output devices from the native render list; AppState carries endpoint-string ids; sounddevice out of audio_devices"
```

---

### Task 8: Window call sites, window tests with `native` mocked, hardware test

**Files:**
- Modify: `flashback_sampler/app/turntable_window.py` (`:920-921`), `tests/unit/test_turntable_window.py`
- Create: `tests/hw/test_native_playback_hw.py`

**Interfaces:**
- `_on_play_clip_clicked` calls `player.bind(audio, co.sample_rate)` then `player.play()`; the `player.open()` line goes.
- `_update_clip_playback_state` is unchanged in text; it reads `is_playing` / `cursor_samples` through the wrapper.

- [ ] **Step 1: Failing window tests**

Append to `tests/unit/test_turntable_window.py`:

```python
# ─────────────────────────────────────────────────────────────────────────
# Clip playback through NativeScrubPlayer with the native library mocked
# ─────────────────────────────────────────────────────────────────────────


def _fake_player(monkeypatch, state):
    """Swap state.scrub_player for a NativeScrubPlayer bound to a fake lib.
    The ring buffers stay on the real library; only the player is faked."""
    from tests.unit.test_scrub_player import _FakePlaybackLib
    from flashback_sampler.core import native
    from flashback_sampler.core.scrub_player import NativeScrubPlayer

    fake = _FakePlaybackLib()
    with monkeypatch.context() as m:
        m.setattr(native, "load", lambda: fake)
        state.scrub_player = NativeScrubPlayer(48_000, 2)
    return fake


def _checkout(state):
    import numpy as np
    audio = np.zeros((4800, 2), dtype=np.float32)
    audio[:, 0] = 0.5
    state.buffer.write(audio)
    return state.checkout_manager.create(duration_s=0.1)


def test_play_click_with_no_checkout_does_nothing(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    win._on_play_clip_clicked()
    assert not [n for n, _ in fake.calls if n in ("fb_playback_bind", "fb_playback_play")]


def test_play_click_binds_the_checkout_at_its_rate_and_plays(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    co = _checkout(state)
    win._tick()
    fake.state = (1, 1, 0, co.audio.shape[0], 48_000)
    win._on_play_clip_clicked()
    _arr, n, rate, ch = fake.bound
    assert (n, rate, ch) == (co.audio.shape[0], co.sample_rate, co.channels)
    assert [n_ for n_, _ in fake.calls if n_ == "fb_playback_play"] == ["fb_playback_play"]
    assert win._intending_playback is True
    assert win.clip_controls[0].text() == "STOP"


def test_play_click_while_playing_pauses_and_drops_intent(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    _checkout(state)
    win._intending_playback = True
    fake.state = (1, 1, 100, 4800, 48_000)
    win._on_play_clip_clicked()
    assert [n for n, _ in fake.calls if n == "fb_playback_pause"] == ["fb_playback_pause"]
    assert win._intending_playback is False
    assert not [n for n, _ in fake.calls if n == "fb_playback_bind"]


def test_update_playback_state_drives_the_playhead_from_the_native_cursor(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    co = _checkout(state)
    win._tick()
    seen = []
    monkeypatch.setattr(win.clip_panel.waveform, "set_playhead", seen.append)
    fake.state = (1, 1, co.audio.shape[0] // 2, co.audio.shape[0], 48_000)
    win._update_clip_playback_state()
    assert seen[-1] == pytest.approx(0.5)
    fake.state = (1, 0, co.audio.shape[0], co.audio.shape[0], 48_000)
    win._update_clip_playback_state()
    assert seen[-1] is None


def test_loop_restarts_play_after_native_auto_stop(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    _checkout(state)
    win._tick()
    win.loop_btn.setChecked(True)
    win._intending_playback = True
    win._was_playing_last_tick = True
    fake.state = (1, 0, 4800, 4800, 48_000)
    win._update_clip_playback_state()
    assert [n for n, _ in fake.calls if n == "fb_playback_play"] == ["fb_playback_play"]
```

`win._tick()` is the window's timer slot (`turntable_window.py:1610`, wired at `:294`; existing tests call it at `test_turntable_window.py:207`). `_checkout` writes 0.1 s at 48 kHz into the `state` fixture's buffer (`:16-21`, 48 kHz stereo).

Run: `python -m pytest tests/unit/test_turntable_window.py -q -k "play or playback or loop"`
Expected: `test_play_click_binds...` FAILS — `bind()` missing the rate argument (`TypeError`), and `player.open()` raises `AttributeError`.

- [ ] **Step 2: Change the call site**

`turntable_window.py:919-921`:

```python
        try:
            player.bind(audio, co.sample_rate)
            player.play()
```

(The `player.open()` line is deleted. `co.sample_rate` is the checkout's recorded rate, `checkout.py:50`; the render stream opens at that rate and the OS resamples to the mix rate — spec "Playback rate".)

- [ ] **Step 3: Run, verify green**

Run: `python -m pytest tests/unit/test_turntable_window.py -q` → pass.
Run: `python -m pytest tests/unit -q -m "not audio_hw and not perf"` → pass.

Mutation check: change the call to `player.bind(audio, 48_000)` → `test_play_click_binds...` still passes (the fixture is 48 kHz) — so change `_checkout` to build the state at a different rate? The fixture is fixed at 48 kHz (`:19`). Instead, in the test monkeypatch `co.sample_rate = 96_000` right after `_checkout` (Checkout is a plain dataclass, `checkout.py:36-37`) and assert `rate == 96_000`. Do that edit in the test now; the literal-48_000 mutation then reddens. Revert the mutation.

- [ ] **Step 4: Hardware test**

Create `tests/hw/test_native_playback_hw.py`:

```python
"""Hardware playback tests: the real default render endpoint. Run by hand:
    pytest tests/hw -m audio_hw -s
You should hear a 1 s 440 Hz tone twice (48 kHz clip, then 96 kHz clip)."""
import time

import numpy as np
import pytest

from flashback_sampler.core import native
from flashback_sampler.core.scrub_player import NativeScrubPlayer

pytestmark = pytest.mark.audio_hw


@pytest.fixture(scope="module")
def lib():
    if native.load() is None:
        pytest.skip("flashback_core not built")
    return native.load()


def _tone(rate: int, seconds: float = 1.0, hz: float = 440.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    mono = (0.2 * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    return np.stack([mono, mono], axis=1)


def test_list_devices_has_a_default_render(lib):
    devs = native.list_devices()
    assert any(d["kind"] == "render" and d["is_default"] for d in devs), devs


@pytest.mark.parametrize("rate", [48_000, 96_000])
def test_tone_plays_cursor_advances_and_playing_drops(lib, rate):
    """96 kHz is the AUTOCONVERTPCM measurement the spec asks for: the
    stream opens at the clip's rate and the engine resamples."""
    p = NativeScrubPlayer(rate, 2)
    clip = _tone(rate)
    p.bind(clip, rate)
    p.play()
    time.sleep(0.4)
    mid = p.cursor_samples
    playing_mid = p.is_playing
    err = p.last_error()
    time.sleep(1.0)
    end = p.cursor_samples
    playing_end = p.is_playing
    p.close()
    assert err is None, err
    assert 0 < mid < len(clip), mid
    assert playing_mid
    assert end == len(clip), end
    assert not playing_end
    print(f"{rate} Hz: mid={mid} end={end}")
```

Run: `python -m pytest tests/hw/test_native_playback_hw.py -m audio_hw -s -q`
Expected: 3 passed; the tone is audible twice. Record on the sub-issue: the 96 kHz result (spec risk 1) and the `GetBufferSize` value — print it from a temporary `std.debug.print` in `openRender` during this run only, then remove it (no diagnostic printing ships).

- [ ] **Step 5: Idle-CPU measurement (spec risk 2)**

Launch the app (`python -m flashback_sampler`), check out a clip, play it once, let it auto-stop, then watch Task Manager's CPU column for the process over 30 s. Compare with the `dev` build before this branch (the PortAudio `sd.OutputStream` stays open after the first play, `scrub_player.py:198-213`). Record both numbers on the sub-issue.

- [ ] **Step 6: Commit**

```bash
git add flashback_sampler/app/turntable_window.py tests/unit/test_turntable_window.py tests/hw/test_native_playback_hw.py
git commit -m "feat(app): clip playback binds at the checkout's rate through NativeScrubPlayer; window tests over a fake lib; hw tone test"
```

---

### Task 9: PR e hand-off

**Files:** none in the repo beyond what the tasks above committed. No files are deleted in PR e (`scrub_player.py` and `test_scrub_player.py` are rewritten in place), so `_ToRemove/` stays untouched.

- [ ] **Step 1: Sub-issue (do this when Task 1 starts, not here — write-at-the-moment)**

```bash
gh issue create --title "PR e: render backend, Playback.zig, output enumeration" --body "Part of #17 (phase 2 part 2, spec docs/superpowers/specs/2026-08-30-zig-core-phase2-d-f-design.md). Branch feat/zig-playback -> dev. Scope: Backend.RenderStream + Kind.render; IAudioRenderClient + event-driven WasapiBackend.openRender; Playback.zig; fb_playback_* ABI; NativeScrubPlayer; output devices from the native list; sounddevice out of audio_devices.py and scrub_player.py. Measurements to record here: AUTOCONVERTPCM at 96 kHz render, GetBufferSize after Initialize(0,0), idle CPU vs the PortAudio stream."
```

Comment on it as things land: the GetBufferSize value, the 96 kHz result, the idle-CPU pair, and any spec deviation beyond the eight listed at the top of this section.

- [ ] **Step 2: Local gate (the merge gate — no CI runs on feature branches)**

```
python -m pytest tests/unit -q -m "not audio_hw and not perf"
zig build --build-file core/build.zig test --summary all
zig build --build-file core/build.zig -Doptimize=ReleaseSafe
zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu
zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=aarch64-macos
zig fmt --check core/build.zig core/src
python -m pytest tests/hw -m audio_hw -s -q
```

Expected: Zig count = PR d's end count + 17 on the Windows host; every leg green; hw suite green with the tone audible. Then `/simplify` (one combined pass) and `/code-review` medium, inline, per the owner's review-cost rule; fix findings; re-run the gate.

- [ ] **Step 3: PR body**

Title: `feat: PR e — render backend, Playback.zig, output enumeration`. Target `dev`. Body:

```
Closes #<sub-issue>. Part of #17.

- Backend.RenderStream (wait/available/write/stop/deinit/mixRate), Backend.VTable.openRender, Kind.render = 3
- wasapi.zig: IAudioRenderClient {F294ACFC-3146-4483-A7BF-ADDCA7C260E2}, CreateEventW/WaitForSingleObject, EVENTCALLBACK
- WasapiBackend.openRender: shared-mode, event-driven, AUTOCONVERTPCM at the clip's rate; render endpoints enumerated as kind 3
- Playback.zig: render thread, owned clip copy allocated at bind, lazy open on first play, reopen-on-thread for rate/device changes
- fb_playback_* ABI + header + ctypes; NativeScrubPlayer replaces the PortAudio ScrubPlayer; output devices from the native list
- sounddevice import gone from audio_devices.py and scrub_player.py (the dependency itself leaves in PR f)

Measurements (spec "Risks to measure"): 96 kHz render under AUTOCONVERTPCM = <result>; GetBufferSize after Initialize(0,0) = <N> frames; idle CPU after auto-stop = <x %> vs PortAudio <y %>.

Zig tests: <d's count> -> <d's count + 17>.

## Zig concepts in this PR
- Event-driven wait vs polling: capture polls (`WasapiBackend.next`, Sleep(10) between GetNextPacketSize calls) because of the loopback quirk; render blocks in `WaitForSingleObject` on an event WASAPI signals once per period, so the thread costs nothing between fills. Same vtable idiom, different wake mechanism, and `Playback` cannot tell which it got.
- Owned slices + allocator at bind: `Playback.clip` is a `[]f32` the struct owns; `allocator.dupe` copies the caller's frames on the control thread in `bind`, `allocator.free` releases the previous copy, `deinit` frees the last one. The render thread only ever reads the slice. `&.{}` is a valid zero-length slice that `free` treats as a no-op.
- Reopen-on-thread: COM apartments are per thread, so the stream must be closed and reopened by the thread that opened it. `bind`/`setDevice` only set an atomic `reopen` flag; the render loop swaps it to false and does the close/open itself.
- Two-flag handshake instead of a lock: `bind` stores `playing = false` then waits for `in_copy == false`; `fill` stores `in_copy = true` before it reads `playing`. With `seq_cst` on those four accesses, no interleaving lets `fill` read a clip `bind` is about to free.
- `?T` for a resource that may be torn down mid-loop: `stream: ?Backend.RenderStream` plus `defer if (stream) |s| ...` — a failed reopen leaves `null`, and the defer skips the dead handle.
```

- [ ] **Step 4: After the owner merges**

Tick `- [ ] e — Playback + output-device enumeration` on epic #17 (`gh issue edit 17 --body ...` with the box checked, or edit in the web UI). Confirm the sub-issue closed via `Closes`. List `feat/zig-playback` (remote) with the other branch deletions pending approval on #17. Update the spec's PR e section only if a deviation beyond the eight recorded here shipped; otherwise the sub-issue comments are the record.
## PR f — Python buffer out, peak bins in Zig, deps and FLAC out, soak

Branch `feat/zig-buffer-only` → `dev`. Spec section: "PR f" in `docs/superpowers/specs/2026-08-30-zig-core-phase2-d-f-design.md:268-332`. The Global Constraints of the part-1 plan (`docs/superpowers/plans/2026-08-16-zig-core-phase2-capture.md:1-65`) apply verbatim: execute in the primary checkout (not a worktree — `soak_test.py` and `ZIG-101.md` are untracked, `CLAUDE.md` is gitignored), `--build-file core/build.zig` on every zig call, no `cd`/`&&`/`$( )` compounds, sequester to `_ToRemove/` (gitignored: `.gitignore:2`), local gates are the merge gate, CI fires only on `dev → main`.

**Assumed merged before this PR (planned separately; verify in Task 0, do not re-plan):** PR d (`Mixer.zig`, `NativeMixedSource`, `fb_ring_create(rate, channels, seconds, status: ?*FbStatus)`, `FbStatus.out_of_memory = 5`) and PR e (`Playback.zig`, `NativeScrubPlayer`, output enumeration, the two `sounddevice` import sites in `audio_devices.py:120` and `scrub_player.py:202` gone).

**Baselines measured on `docs/phase2-d-f-spec` (pre-d/e):** Zig 83 tests (`grep -c '^test ' core/src/*.zig`: abi 17, Capture 8, convert 7, FakeBackend 3, Ring 22, root 1, Summary 9, wasapi 5, WasapiBackend 1, wav 10); pytest 527 collected with `-m "not audio_hw and not perf"`. Task 0 re-measures both after d/e.

### Grep inventory (every live hit, verified 2026-08-30; specs/plans/caches excluded)

Pattern set: `soundfile|sounddevice|soundcard|FLAC|flac|AudioCircularBuffer|make_ring_buffer|RingDerivedOps|_peak_bins_impl|get_peak_bins`. Each hit names the task that edits it.

| File:line | Hit | Task |
|---|---|---|
| `.gitignore:59` | `*.flac` | none (ignores stray exports; leave) |
| `core/src/abi.zig:213` | comment: "corrupts get_peak_bins, which walks the raw buffer" | 1 |
| `core/src/Ring.zig:42-47` | comment: parity with `AudioCircularBuffer._SUMMARY_SLOT_SAMPLES` | 5 |
| `core/src/Summary.zig:164` | doc: "Mirror of buffer.py get_summary_bins (buffer.py:421)" | 5 |
| `core/src/wav.zig:3`, `:96` | "Parity vs soundfile", "checks against soundfile" | 4 |
| `core/include/flashback_core.h:35-45` | comment: "a peak-bins reader" walking `fb_ring_storage` | 1 |
| `flashback_sampler.egg-info/*` | generated, gitignored (`.gitignore:22`) | none (regenerates on the owner's next editable install; no pip run in this PR) |
| `flashback_sampler.spec:13-14`, `:34-38` | docstring bullet + `collect_all` loop | 6 |
| `flashback_sampler/app/audio_devices.py:7,76,118-120` | `sounddevice` prose + lazy import | PR e (Task 0 verifies gone) |
| `flashback_sampler/app/audio_devices.py:184` | docstring "`buffer` is an AudioCircularBuffer" | 5 |
| `flashback_sampler/app/state.py:24,176-179,225,403,416-417` | import, comments, annotation, constructor | 5 |
| `flashback_sampler/app/turntable_window.py:85` | `_peak_bins_from_audio` docstring names `AudioCircularBuffer.get_peak_bins` | 5 |
| `flashback_sampler/app/turntable_window.py:1188-1191,1204-1207,1369-1375` | FLAC ext/filter/resolve/actions | 3 |
| `flashback_sampler/app/turntable_window.py:1623` | `active_buf.get_peak_bins(seconds=…, n_bins=360)` — consumer, signature unchanged | 1 (no edit) |
| `flashback_sampler/app/widgets/waveform_view.py:7` | docstring names `AudioCircularBuffer.get_peak_bins()` | 5 |
| `flashback_sampler/core/__init__.py:1` | `from .buffer import AudioCircularBuffer` | 5 |
| `flashback_sampler/core/capture_slot.py:27,44-45,65,124` | import, docstring, annotation, constructor | 5 |
| `flashback_sampler/core/capture_source.py:7-15,29` | stale `soundcard`/`sounddevice`/`AudioCircularBuffer` prose | 6 |
| `flashback_sampler/core/checkout.py:7,20,22,27,31-32,88,92,157-160,300,317-318,329-335` | FLAC + soundfile + `RingDerivedOps` | 3, 5 |
| `flashback_sampler/core/drag_export.py:4` | docstring "Pure Python + soundfile" | 6 |
| `flashback_sampler/core/mixed_capture.py:12,32,46-47,58,64` | `make_ring_buffer`/`RingDerivedOps` | PR d sequestered it (Task 0 verifies) |
| `flashback_sampler/core/native.py:4-19,29,32-36,213-214,236-241,366-372,398-414` | docstring, import, class base, `get_peak_bins`, `close()` docstring | 1, 3, 5 |
| `flashback_sampler/core/native_capture.py:32` | error text names `NativeAudioCircularBuffer` | none (still true) |
| `flashback_sampler/core/scrub_player.py:16,114,199,202` | `sounddevice` | PR e (Task 0 verifies gone) |
| `packaging/README.md:28,38,41,46` | `collect_all` bullet, smoke-test items, rough edge | 6 |
| `PHASE2-HANDOFF.md:18-19,33-54,114` | untracked historical hand-off | none (history) |
| `PLATFORM.md:12,28-32` | "(ctypes)", `make_ring_buffer` fallback paragraph | 7 |
| `pyproject.toml:14-16` | three deps | 6 |
| `README.md:6,8,16,39,50-52` | WAV/FLAC, "pure Python + numpy", `soundcard`/`sounddevice` remain, tree | 3, 7 |
| `requirements.txt:2-4` | three deps | 6 |
| `soak_test.py:21,26` | `make_ring_buffer` (untracked) | 5 (import), 8 (port) |
| `tests/fixtures/fake_capture.py:6,16,22,28,91` | `soundcard / sounddevice` prose, `AudioCircularBuffer` import + annotations | 5 |
| `tests/hw/test_native_capture_hw.py:9,30,52,67` | `make_ring_buffer` | 5 |
| `tests/unit/test_app_state.py:14,21-24,240-241,453-454` | `RingDerivedOps` import/isinstance, comments | 5 |
| `tests/unit/test_audio_devices.py:8` | comment "a fake AudioCircularBuffer" | 5 |
| `tests/unit/test_buffer.py:2,16,21-29,72,126,230,321,468-469,710-732` | header, import, two-way fixture, Python-only tests, factory tests | 5 |
| `tests/unit/test_buffer.py:386-447,580-670` | `get_peak_bins` parity tests (keep; run native-only after Task 5) | 1, 5 |
| `tests/unit/test_capture_slot.py:10,31-33` | `RingDerivedOps` | 5 |
| `tests/unit/test_capture_source.py:13,17-18,50` | `AudioCircularBuffer` | 5 |
| `tests/unit/test_checkout.py:5,17,19` + 24 constructor sites (`:30,49,58,74,88,107,129,145,156,169,177,199,208,219,227,250,313,332,351,360,377,393,403,417,436`) | soundfile import, `AudioCircularBuffer` | 4, 5 |
| `tests/unit/test_checkout.py:322-327,445-446,465-466,511-513` | `sf.read`/`sf.info` on WAV | 4 |
| `tests/unit/test_checkout.py:331-348,449-460,493-494,519-535` | FLAC tests + docstrings | 3 |
| `tests/unit/test_drag_export.py:8,10,24,57-59,70-72` | soundfile, `AudioCircularBuffer` | 4, 5 |
| `tests/unit/test_native_smoke.py:33-35,116-131,142-160,163-212` | comments, `_peak_bins_impl` docstring, fallback test, two soundfile decode tests | 1, 4, 5 |
| `tests/unit/test_scrub_player.py:7,252` | `sounddevice` prose | PR e (Task 0 verifies gone) |
| `tests/unit/test_turntable_window.py:80,600,618` | comment, `import soundfile`, `sf.info` | 4 |

**Task → commit map:** Task 0 setup · Tasks 1–2 Zig readers · Task 3 FLAC · Task 4 wavread · Task 5 delete `buffer.py` · Task 6 deps · Task 7 docs · Task 8 soak + closure · Task 9 hand-off. One PR.

---

### Task 0: Branch, sub-issue, pre-flight numbers

- [ ] **Step 1: Verify the d/e premises**

Run and record the output in the sub-issue body:
- `git checkout dev`; `git pull`; `git checkout -b feat/zig-buffer-only`
- `ls flashback_sampler/core` — expect `mixed_capture.py` and `scrub_player.py` gone or rewritten as `NativeMixedSource` / `NativeScrubPlayer` (spec `:134-139`, `:238-245`). Record the module names PR d/e shipped; Tasks 5 and 8 cite them as `<MIXED_MODULE>` / `<MIXED_CLASS>`.
- `grep -rn "import sounddevice\|import soundcard\|from sounddevice\|from soundcard" flashback_sampler tests soak_test.py` — expect zero hits. A hit means PR e is not merged: stop and report.
- `grep -n "fb_ring_create" core/src/abi.zig core/include/flashback_core.h flashback_sampler/core/native.py` — expect the four-argument form with `status`.
- `zig build --build-file core/build.zig test --summary all` — record the count as `<Z0>`.
- `python -m pytest tests/unit --collect-only -q -m "not audio_hw and not perf"` — record the count as `<P0>`.

- [ ] **Step 2: Sub-issue**

`gh issue create --title "f — Python buffer out, peak bins in Zig, deps and FLAC out, soak" --body "<what/why, 3 bullets; link the spec section; the Task 0 numbers>"` → `<F>`. Edit epic #17's task list line `- [ ] f — delete Python buffer, deps, parity harness; final soak` (issue body line 14) to `- [ ] f — #<F> …`.

---

### Task 1: `Ring.peakBins` + `PeakBin` + `fb_ring_peak_bins`; Python calls the ABI; temporary parity test

**Files:**
- Modify: `core/src/Ring.zig` (new decls + tests), `core/src/abi.zig` (export + tests + comment at `:208-217`), `core/include/flashback_core.h`, `flashback_sampler/core/native.py` (`_declare` `:105-170`, `get_peak_bins` `:366-372`), `tests/unit/test_native_smoke.py:116-131` (docstring), `docs/superpowers/specs/2026-08-30-zig-core-phase2-d-f-design.md` (deviation note)
- Create: `tests/unit/test_peak_bins_parity.py` (TEMPORARY — Task 5 sequesters it)

**Interfaces:**
- Produces (Zig): `pub const PeakBin = extern struct { min: f32, max: f32 }`; `pub const PeakBinsError = error{ InvalidArgument, Overwritten }`; `pub fn peakBins(self: *Ring, n_frames_req: u64, n_bins: usize, out: []PeakBin) PeakBinsError!void` with `out.len == n_bins * channels`, layout `out[bin * channels + ch]`; `pub const peak_bins_max_samples_per_bin: u64 = 256`; `pub const peak_bins_read_headroom: u64 = 4096`.
- Produces (ABI): `export fn fb_ring_peak_bins(ring: *Ring, n_frames: u64, n_bins: usize, out: [*]Ring.PeakBin) FbStatus` — `invalid_arg` for `n_bins == 0`, `overwritten` after three torn attempts (with `out` zeroed), `ok` otherwise (including the empty window, `out` zeroed).
- Consumes: `Ring.total_written`, `Ring.capacity`, `Ring.storage_frames`, `Ring.frames` (`Ring.zig:20-32`).
- **Plan choice (spec deviation, recorded in Step 6):** the spec (`:276-277`) declares `peakBins(abs_start, abs_end, …)`. The numpy port re-snapshots `total_written` and re-applies the headroom clamp on every retry (`buffer.py:84-101`); with absolute bounds that loop, and its arithmetic, would have to live in Python, which the standing rule (`spec:17-20`) forbids. The export therefore takes a window length, the same shape as `fb_ring_summary_bins(ring, n_bins, n_samples, …)` (`abi.zig:251`). Python passes `int(seconds * sample_rate)` — a unit conversion.

- [ ] **Step 1: Hand-computed cases (the numbers the tests assert)**

All from `_peak_bins_impl` (`buffer.py:38-165`): `n_avail = min(tw, capacity)`; if `n_avail >= capacity and capacity > 8192` then `n_avail = capacity - 4096`; `n = min(req, n_avail)`; `abs_start = tw - n`; `edges[i] = int64(float(i) * (n / n_bins))` for `i < n_bins`, `edges[n_bins] = n` (numpy `linspace` computes `i * step` in f64 then truncates; the last edge is set to `stop`); `span_ref = edges[1] - edges[0]`; `stride = max(1, span_ref // 256)`; physical index = `abs % storage_frames` (`storage_frames = capacity + 4096`, `Ring.zig:80`).

| Case | Ring | Written | Call | Derived | Expected (min, max) per bin |
|---|---|---|---|---|---|
| A exact edges | rate 1000, ch 1, 1.0 s (cap 1000, storage 5096, no headroom: 1000 < 8192) | ramp 0..999 | req 1000, 4 bins | step 250, edges 0,250,500,750,1000, stride 1 | (0,249) (250,499) (500,749) (750,999) |
| B uneven edges | same | ramp 0..9 | req 10, 4 bins | step 2.5 → edges 0,2,5,7,10 | (0,1) (2,4) (5,6) (7,9) |
| C empty bins | same | ramp 0..2 | req 3, 5 bins | step 0.6 → edges 0,0,1,1,2,3 | bin0 empty → stays (0,0); bin1 (0,0); bin2 empty → copies bin1 (0,0); bin3 (1,1); bin4 (2,2) |
| D physical wrap | same | ramp 0..5999 in ONE `write` (chunked internally, `Ring.zig:205-256`) | req 1000, 2 bins | tw 6000, n 1000, abs_start 5000, ring_start 5000; bin0 spans physical 5000..5096 then 0..404 | (5000,5499) (5500,5999) |
| E headroom + stride grid | rate 10000, ch 1, 1.0 s (cap 10000 > 8192, storage 14096) | ramp 0..9999 | req 10000, 2 bins | n_avail 10000−4096 = 5904, abs_start 4096, step 2952, span_ref 2952, stride 2952//256 = 11, k = 2952//11 + 1 = 269; bin0 first_abs = ceil(4096/11)·11 = 373·11 = 4103, last = 4103 + 268·11 = 7051; bin1 start 7048, first_abs = 641·11 = 7051, last 9999 | (4103,7051) (7051,9999) |
| F grid anchored to absolute index | ring E after one more frame (value 10000) | tw 10001 | req 10000, 2 bins | abs_start 4097; bin0 first_abs = ceil(4097/11)·11 = 4103 (unchanged) | bin0 still (4103,7051) |
| H channels independent | rate 1000, ch 2, 1.0 s | 500 frames: ch0 = 0, ch1 = index | req 500, 1 bin | stride 1 | ch0 (0,0); ch1 (0,499) |
| I empty window | rate 1000, ch 1 | nothing | req 1000, 3 bins | n 0 | all (0,0), returns ok |

Case E pins three things at once: the headroom (without it `abs_start = 0` and bin0's min is 0), the ceil-to-stride anchor (4103 is the first multiple of 11 ≥ 4096), and `k` (269 positions reach exactly 9999 in bin1). Case F pins that the grid is keyed to absolute indices, not the window start.

- [ ] **Step 2: Failing Zig tests**

Append to `core/src/Ring.zig`:

```zig
fn peakRamp(ring: *Ring, n: usize) void {
    // Writes frames whose ch0 value is the absolute index; ch1 (if any) too.
    var buf: [1024]f32 = undefined;
    var abs: u64 = ring.total_written.load(.acquire);
    var left = n;
    while (left > 0) {
        const take = @min(left, 1024 / @as(usize, ring.channels));
        for (0..take) |i| {
            for (0..ring.channels) |c| buf[i * ring.channels + c] = @floatFromInt(abs + i);
        }
        ring.write(buf[0 .. take * ring.channels]);
        abs += take;
        left -= take;
    }
}

fn expectBin(out: []const PeakBin, i: usize, min: f32, max: f32) !void {
    try std.testing.expectEqual(min, out[i].min);
    try std.testing.expectEqual(max, out[i].max);
}

test "peakBins: exact edges, stride 1 (case A)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 1000);
    var out: [4]PeakBin = undefined;
    try ring.peakBins(1000, 4, &out);
    try expectBin(&out, 0, 0, 249);
    try expectBin(&out, 1, 250, 499);
    try expectBin(&out, 2, 500, 749);
    try expectBin(&out, 3, 750, 999);
}

test "peakBins: uneven edges truncate like numpy linspace (case B)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 10);
    var out: [4]PeakBin = undefined;
    try ring.peakBins(10, 4, &out);
    try expectBin(&out, 0, 0, 1);
    try expectBin(&out, 1, 2, 4);
    try expectBin(&out, 2, 5, 6);
    try expectBin(&out, 3, 7, 9);
}

test "peakBins: an empty bin copies its predecessor; an empty bin 0 stays zero (case C)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 3);
    var out: [5]PeakBin = undefined;
    try ring.peakBins(3, 5, &out);
    try expectBin(&out, 0, 0, 0);
    try expectBin(&out, 1, 0, 0);
    try expectBin(&out, 2, 0, 0);
    try expectBin(&out, 3, 1, 1);
    try expectBin(&out, 4, 2, 2);
    // Second ring: 5 frames, 7 bins, step 5/7 → edges 0,0,1,2,2,3,4,5.
    // Bin 3 ([2,2)) is empty with a NON-zero predecessor (1,1): this is
    // what pins the copy — in the first ring the predecessor is (0,0),
    // so deleting the copy would not redden it.
    var ring2 = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring2.deinit();
    peakRamp(&ring2, 5);
    var out7: [7]PeakBin = undefined;
    try ring2.peakBins(5, 7, &out7);
    try expectBin(&out7, 2, 1, 1);
    try expectBin(&out7, 3, 1, 1);
    try expectBin(&out7, 6, 4, 4);
}

test "peakBins: wraps at storage_frames, not capacity (case D)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 6000); // past storage_frames (1000 + 4096)
    var out: [2]PeakBin = undefined;
    try ring.peakBins(1000, 2, &out);
    try expectBin(&out, 0, 5000, 5499);
    try expectBin(&out, 1, 5500, 5999);
}

test "peakBins: headroom clamp and absolute stride grid (cases E, F)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 10_000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 10_000);
    var out: [2]PeakBin = undefined;
    try ring.peakBins(10_000, 2, &out);
    try expectBin(&out, 0, 4103, 7051);
    try expectBin(&out, 1, 7051, 9999);
    peakRamp(&ring, 1); // roll the window by one frame: the grid does not move
    try ring.peakBins(10_000, 2, &out);
    try expectBin(&out, 0, 4103, 7051);
}

test "peakBins: channels reduce independently (case H)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var frames: [1000]f32 = undefined;
    for (0..500) |i| {
        frames[i * 2] = 0;
        frames[i * 2 + 1] = @floatFromInt(i);
    }
    ring.write(&frames);
    var out: [2]PeakBin = undefined;
    try ring.peakBins(500, 1, &out);
    try expectBin(&out, 0, 0, 0);
    try expectBin(&out, 1, 0, 499);
}

test "peakBins: empty window zeroes out and succeeds; n_bins == 0 is InvalidArgument (case I)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    var out: [3]PeakBin = .{ .{ .min = 9, .max = 9 }, .{ .min = 9, .max = 9 }, .{ .min = 9, .max = 9 } };
    try ring.peakBins(1000, 3, &out);
    for (out) |b| try std.testing.expectEqual(@as(f32, 0), b.max);
    try std.testing.expectError(error.InvalidArgument, ring.peakBins(1000, 0, out[0..0]));
}
```

Append to `core/src/abi.zig`:

```zig
test "fb_ring_peak_bins rejects n_bins == 0 and reduces a ramp" {
    const ring = fb_ring_create(1000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    var out: [2]Ring.PeakBin = undefined;
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_ring_peak_bins(ring, 10, 0, &out));
    var frames: [10]f32 = undefined;
    for (&frames, 0..) |*f, i| f.* = @floatFromInt(i);
    fb_ring_write(ring, &frames, 10);
    try std.testing.expectEqual(FbStatus.ok, fb_ring_peak_bins(ring, 10, 2, &out));
    try std.testing.expectEqual(@as(f32, 5), out[1].min);
    try std.testing.expectEqual(@as(f32, 9), out[1].max);
}

test "PeakBin is two packed f32 (the ctypes host relies on this layout)" {
    try std.testing.expectEqual(@as(usize, 8), @sizeOf(Ring.PeakBin));
    try std.testing.expectEqual(@as(usize, 4), @offsetOf(Ring.PeakBin, "max"));
}
```

Verify: `fb_ring_create` takes the fourth `status` argument after PR d (spec `:126-130`); if PR d spelled it differently, match the shipped signature.

Run: `zig build --build-file core/build.zig test` → compile error (`PeakBin`, `peakBins`, `fb_ring_peak_bins` missing). Red.

- [ ] **Step 3: Implement in `Ring.zig`**

Place after `read` (`Ring.zig:346`):

```zig
/// One (min, max) pair per channel per display bin. `extern` fixes the
/// layout to two consecutive f32 — the ctypes host maps a numpy
/// float32[n_bins][channels][2] view straight onto `[*]PeakBin`.
pub const PeakBin = extern struct { min: f32, max: f32 };

pub const PeakBinsError = error{ InvalidArgument, Overwritten };

/// Cap on samples inspected per bin: above it the bin is stride-sampled so
/// the per-tick cost stays bounded regardless of ring size (256 × 360 bins
/// × stereo ≈ 46k reads, sub-millisecond).
pub const peak_bins_max_samples_per_bin: u64 = 256;
/// Slack left below capacity on a saturated ring so the writer can advance
/// during the in-place scan without tripping the verify (4096 frames ≈
/// 85 ms at 48 kHz, larger than a WASAPI period). Applied only when
/// capacity > 2 × headroom so tiny test rings still expose every frame.
pub const peak_bins_read_headroom: u64 = 4096;

/// Downsample the newest `n_frames_req` frames into `n_bins` (min, max)
/// pairs per channel for the waveform display. `out.len == n_bins *
/// channels`, laid out `out[bin * channels + ch]`.
///
/// Port of Python's `_peak_bins_impl`: same window clamp and headroom,
/// same bin edges (numpy `linspace` — `i * step` in f64, truncated), same
/// stride grid anchored to ABSOLUTE frame indices (so rolling the window
/// by a few frames does not re-pick a bin's samples), same physical
/// modulus (`storage_frames`, never `capacity`). Two ported quirks stay:
/// the stride branch reads `k` positions per bin regardless of the bin's
/// end (it may overshoot into the next bin, or past `total_written` on
/// the last bin), and NaN samples are ignored by `@min`/`@max` where
/// numpy would propagate them.
///
/// Reads `frames` IN PLACE through the seqlock — no copy of a multi-
/// hundred-MB ring at 30 Hz — then verifies `total_written` exactly like
/// `read`. On three torn attempts `out` is all zeros and the error says
/// so; an empty window is all zeros and success.
pub fn peakBins(self: *Ring, n_frames_req: u64, n_bins: usize, out: []PeakBin) PeakBinsError!void {
    if (n_bins == 0) return error.InvalidArgument;
    std.debug.assert(out.len == n_bins * self.channels);
    const chans: usize = self.channels;
    const modulus = self.storage_frames;
    var attempt: u8 = 0;
    while (attempt < 3) : (attempt += 1) {
        @memset(out, .{ .min = 0, .max = 0 });
        const tw = self.total_written.load(.acquire);
        var n_avail = @min(tw, self.capacity);
        if (n_avail >= self.capacity and self.capacity > 2 * peak_bins_read_headroom)
            n_avail = self.capacity - peak_bins_read_headroom;
        const n = @min(n_frames_req, n_avail);
        if (n == 0) return;
        const abs_start = tw - n;
        // numpy: step = n / n_bins in f64, edge_i = trunc(i * step). The
        // multiply must be `float(i) * step`, not `i * n / n_bins` — a
        // different rounding order moves edges by one frame and shifts
        // every waveform golden (spec "Risks", peak-bin parity).
        const step: f64 = @as(f64, @floatFromInt(n)) / @as(f64, @floatFromInt(n_bins));
        const span_ref = binEdge(step, 1, n, n_bins) - binEdge(step, 0, n, n_bins);
        const stride: u64 = @max(1, span_ref / peak_bins_max_samples_per_bin);

        if (stride == 1) {
            for (0..n_bins) |i| {
                const a = binEdge(step, i, n, n_bins);
                const b = binEdge(step, i + 1, n, n_bins);
                if (b <= a) {
                    if (i > 0) @memcpy(out[i * chans .. (i + 1) * chans], out[(i - 1) * chans .. i * chans]);
                    continue;
                }
                var idx: u64 = (abs_start + a) % modulus;
                var first = true;
                var f: u64 = a;
                while (f < b) : (f += 1) {
                    reduceFrame(self, idx, out[i * chans .. (i + 1) * chans], &first);
                    idx += 1;
                    if (idx == modulus) idx = 0;
                }
            }
        } else {
            // k covers span_ref at any grid alignment; tail bins may pull
            // 1–2 positions from the next bin (ported behaviour).
            const k: usize = @intCast(span_ref / stride + 1);
            for (0..n_bins) |i| {
                const bin_abs = abs_start + binEdge(step, i, n, n_bins);
                const first_abs = ((bin_abs + stride - 1) / stride) * stride; // ceil to the grid
                var first = true;
                for (0..k) |j| {
                    const idx = (first_abs + @as(u64, j) * stride) % modulus;
                    reduceFrame(self, idx, out[i * chans .. (i + 1) * chans], &first);
                }
            }
        }
        // Seqlock verify, same two clauses as `read`: the first guards the
        // unsigned subtraction against a racing flush (ReleaseSafe traps
        // on underflow); the second is the lap check.
        const t2 = self.total_written.load(.acquire);
        if (t2 >= abs_start and t2 - abs_start <= self.capacity) return;
    }
    @memset(out, .{ .min = 0, .max = 0 });
    return error.Overwritten;
}

fn binEdge(step: f64, i: usize, n: u64, n_bins: usize) u64 {
    if (i == n_bins) return n; // numpy sets the last edge to `stop` exactly
    // @intFromFloat truncates toward zero == numpy's int64 cast here (non-negative).
    return @intFromFloat(@as(f64, @floatFromInt(i)) * step);
}

/// Fold one physical frame into a bin's per-channel (min, max).
fn reduceFrame(self: *const Ring, frame_idx: u64, bin: []PeakBin, first: *bool) void {
    const base: usize = @intCast(frame_idx * self.channels);
    for (bin, 0..) |*pb, c| {
        const v = self.frames[base + c];
        if (first.*) {
            pb.* = .{ .min = v, .max = v };
        } else {
            pb.min = @min(pb.min, v);
            pb.max = @max(pb.max, v);
        }
    }
    first.* = false;
}
```

`abi.zig`, after `fb_ring_summary_bins` (`:259`):

```zig
export fn fb_ring_peak_bins(ring: *Ring, n_frames: u64, n_bins: usize, out: [*]Ring.PeakBin) FbStatus {
    ring.peakBins(n_frames, n_bins, out[0 .. n_bins * ring.channels]) catch |err| return switch (err) {
        error.InvalidArgument => .invalid_arg,
        error.Overwritten => .overwritten,
    };
    return .ok;
}
```

Rewrite the `fb_ring_storage_frames` comment (`abi.zig:208-217`): the sentence "silently corrupts get_peak_bins, which walks the raw buffer directly" becomes "silently corrupts any host that walks the raw buffer directly (the Python host no longer does — peaks come from fb_ring_peak_bins)". Same fix in `flashback_core.h:35-45` ("e.g. a peak-bins reader" → "the engine's own fb_ring_peak_bins uses storage_frames internally; a host walking the raw buffer must too").

`flashback_core.h`: after `FbSubtype` add `typedef struct FbPeakBin { float min; float max; } FbPeakBin;` and after `fb_ring_summary_bins`:

```c
/* min/max per channel per bin over the newest n_frames (headroom-clamped).
 * out holds n_bins * channels FbPeakBin, out[bin * channels + ch].
 * FB_INVALID_ARG for n_bins == 0; FB_OVERWRITTEN (out zeroed) after three
 * torn attempts; FB_OK with out zeroed for an empty window. */
FbStatus fb_ring_peak_bins(FbRing *, uint64_t n_frames, size_t n_bins, FbPeakBin *out);
```

- [ ] **Step 4: Run Zig, verify green, count +9**

`zig build --build-file core/build.zig test --summary all` → `<Z0> + 9` (7 Ring + 2 abi). `zig fmt --check core/src`.

Mutation check (edit-then-revert, one per clause):
- `binEdge`: compute `@intFromFloat(@as(f64, @floatFromInt(i * n)) / n_bins_f)` instead → case B still passes (2.5·i is exact) but case E's `span_ref`/edges can move; if E stays green, mutate instead to `@intFromFloat(@round(…))` → case B reddens (edge 2.5 → 3). Record which mutation reddened.
- Delete the headroom `if` → case E bin0 min becomes 0. Red.
- `first_abs = bin_abs` (window-relative grid) → case F bin0 min becomes 4097. Red.
- `modulus = self.capacity` → case D reads wrong slots. Red.
- Remove the `i > 0` copy → case C's second ring: bin 3 stays (0,0) instead of (1,1). Red. (The first ring alone would NOT redden — its empty bin's predecessor is (0,0) — which is why the test carries the second ring.)
- Rebuild the DLL: `zig build --build-file core/build.zig -Doptimize=ReleaseSafe` (the pytest half below loads `core/zig-out/bin/flashback_core.dll`, `native.py:50`).

- [ ] **Step 5: Python side — declaration, one-line call, temporary parity test**

`native.py` `_declare`, after the `fb_ring_summary_bins` pair (`:148-149`):

```python
    lib.fb_ring_peak_bins.argtypes = [C.c_void_p, C.c_uint64, C.c_size_t, C.POINTER(FbPeakBin)]
    lib.fb_ring_peak_bins.restype = C.c_int
```

Add next to the other `C.Structure`s (`:86-102`):

```python
class FbPeakBin(C.Structure):
    _fields_ = [("min", C.c_float), ("max", C.c_float)]
```

Replace `get_peak_bins` (`native.py:366-372`):

```python
    def get_peak_bins(self, seconds: float, n_bins: int) -> np.ndarray:
        """(n_bins, 2, channels) float32: [i, 0, c] = min, [i, 1, c] = max.
        The engine fills PeakBin{min, max} per (bin, channel); the transpose
        below is the only Python work. Zeros for an empty or torn window."""
        out = np.zeros((n_bins, self.channels, 2), dtype=np.float32)
        status = self._lib.fb_ring_peak_bins(
            self._h, max(0, int(seconds * self.sample_rate)), n_bins,
            out.ctypes.data_as(C.POINTER(FbPeakBin)),
        )
        if status == _INVALID_ARG:
            raise ValueError("n_bins must be positive")
        return np.ascontiguousarray(out.transpose(0, 2, 1))
```

(`np.zeros` with a negative `n_bins` raises `ValueError` on its own; `n_bins == 0` reaches the ABI and raises through `_INVALID_ARG`. The contiguous copy is 5.7 kB for the UI's 360×2×2 call at 30 Hz; it keeps the C-contiguous contract `_peak_bins_from_audio` (`turntable_window.py:84-100`) also honours.)

`_peak_bins_impl` and the import at `native.py:29` stay until Task 5.

Create `tests/unit/test_peak_bins_parity.py`:

```python
"""TEMPORARY parity pin: numpy `_peak_bins_impl` (buffer.py) versus
`fb_ring_peak_bins` on the same ring contents. Deleted with buffer.py in
Task 5 of the PR f plan; the Zig tests in Ring.zig pin the arithmetic
permanently."""
from __future__ import annotations

import numpy as np
import pytest

from flashback_sampler.core import native
from flashback_sampler.core.buffer import _peak_bins_impl

pytestmark = pytest.mark.skipif(native.load() is None, reason="flashback_core not built")


def _numpy_peaks(buf, seconds, n_bins):
    return _peak_bins_impl(
        buf.buffer, buf.buffer_size,
        lambda: buf.total_written,
        lambda abs_start: buf.total_written - abs_start <= buf.buffer_size,
        buf.sample_rate, buf.channels, seconds, n_bins,
    )


@pytest.mark.parametrize("rate,channels,duration,frames,seconds,n_bins", [
    (1000, 1, 1.0, 1000, 1.0, 4),          # case A: stride 1, exact edges
    (1000, 1, 1.0, 10, 0.01, 4),           # case B: uneven edges
    (1000, 1, 1.0, 3, 0.003, 5),           # case C: empty bins copy predecessor
    (1000, 2, 1.0, 6000, 1.0, 2),          # case D: physical wrap past storage_frames
    (10_000, 1, 1.0, 10_000, 1.0, 2),      # case E: headroom + stride 11
    (10_000, 2, 1.0, 10_001, 1.0, 100),    # rolling by one frame, stereo
    (48_000, 2, 2.0, 96_000 + 512 * 7, 2.0, 360),  # the UI's call (turntable_window.py:1623)
])
def test_zig_peak_bins_equal_numpy(rate, channels, duration, frames, seconds, n_bins):
    buf = native.NativeAudioCircularBuffer(duration_seconds=duration, sample_rate=rate, channels=channels)
    try:
        rng = np.random.default_rng(frames)
        buf.write((rng.standard_normal((frames, channels)) * 100).astype(np.float32))
        zig = buf.get_peak_bins(seconds, n_bins)
        ref = _numpy_peaks(buf, seconds, n_bins)
        assert zig.shape == ref.shape == (n_bins, 2, channels)
        np.testing.assert_array_equal(zig, ref)
    finally:
        buf.close()
```

Rewrite the docstring of `test_get_peak_bins_correct_past_capacity_before_physical_wrap` (`test_native_smoke.py:117-131`): it now pins `Ring.peakBins`'s `storage_frames` modulus, not `buffer.py`'s `modulus = len(ring)`; keep the 7994/1996-vs-8001/9996 numbers, re-attribute the mutation to `modulus = self.capacity` in `Ring.zig`.

Run: `python -m pytest tests/unit/test_peak_bins_parity.py tests/unit/test_buffer.py tests/unit/test_native_smoke.py -q` → green (the `[python]` parametrizations still run numpy; the `[native]` ones now run Zig).

Mutation check: in `Ring.zig` change `peak_bins_read_headroom` to 4095 → parity case E reddens (`assert_array_equal` on bin0 min). Revert, rebuild the DLL.

- [ ] **Step 6: Record the deviation, commit**

In `docs/superpowers/specs/2026-08-30-zig-core-phase2-d-f-design.md`, under "### `Ring.peakBins`" add one paragraph: "Deviation (PR f plan): `peakBins`/`fb_ring_peak_bins` take a window length `n_frames`, not `(abs_start, abs_end)` — the retry loop re-snapshots and re-clamps inside Zig, as `fb_ring_summary_bins` already does. `out` layout `[bin][channel]{min,max}`."

```bash
git add core/src/Ring.zig core/src/abi.zig core/include/flashback_core.h flashback_sampler/core/native.py tests/unit/test_peak_bins_parity.py tests/unit/test_native_smoke.py docs/superpowers/specs/2026-08-30-zig-core-phase2-d-f-design.md
git commit -m "feat(core): Ring.peakBins + fb_ring_peak_bins -- waveform peaks computed in Zig, numpy port pinned by a parity test"
```

---

### Task 2: `Ring.rmsLatest` + `fb_ring_rms` — the level meter's maths leaves Python

**Files:**
- Modify: `core/src/Ring.zig`, `core/src/abi.zig`, `core/include/flashback_core.h`, `flashback_sampler/core/native.py`, `tests/unit/test_peak_bins_parity.py` (one more temporary parity test), spec deviation note

**Interfaces:**
- Produces (Zig): `pub fn rmsLatest(self: *Ring, n_frames_req: u64, out: []f32) ReadError!void`, `out.len == channels`, window `n = min(req, total_written, capacity)` (the `get_latest` clamp, `native.py:306`), `out` zeroed on error.
- Produces (ABI): `export fn fb_ring_rms(ring: *Ring, n_frames: u64, out: [*]f32) FbStatus`.
- **Plan choice:** `RingDerivedOps.get_rms_levels` (`buffer.py:190-195`) is `sqrt(mean(audio²))` in numpy and feeds the level meter (`turntable_window.py:448`). The spec's enumeration (`:288-292`) does not name it, but its rule — methods are "one-line ABI calls or unit conversion" — excludes it. No new sync primitive: the reduction reads in `max_write_frames` chunks through `Ring.read`, so every chunk is a verified seqlock copy and the scratch is a fixed 32 KiB stack array.

- [ ] **Step 1: Failing Zig tests**

Append to `core/src/Ring.zig`:

```zig
test "rmsLatest: exact values, newest-window clamp, per-channel" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 16, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 3, 4, 0, 0 });
    var out: [1]f32 = undefined;
    try ring.rmsLatest(4, &out);
    try std.testing.expectEqual(@as(f32, 2.5), out[0]); // sqrt((9 + 16) / 4)
    try ring.rmsLatest(2, &out);
    try std.testing.expectEqual(@as(f32, 0), out[0]); // newest two frames are silent
    var st = try Ring.init(std.testing.allocator, .{ .sample_rate = 16, .channels = 2, .seconds = 1.0 });
    defer st.deinit();
    st.write(&[_]f32{ 1, 2, 1, 2 });
    var out2: [2]f32 = undefined;
    try st.rmsLatest(2, &out2);
    try std.testing.expectEqual(@as(f32, 1), out2[0]);
    try std.testing.expectEqual(@as(f32, 2), out2[1]);
}

test "rmsLatest: a window longer than max_write_frames is read in chunks" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 10_000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    const block = [_]f32{0.5} ** 9000; // > 2 chunks of 4096
    ring.write(&block);
    var out: [1]f32 = undefined;
    try ring.rmsLatest(9000, &out);
    try std.testing.expectEqual(@as(f32, 0.5), out[0]); // ss = 9000 * 0.25, exact in f64
}

test "rmsLatest: empty ring is zero and ok" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 16, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    var out: [1]f32 = .{7};
    try ring.rmsLatest(16, &out);
    try std.testing.expectEqual(@as(f32, 0), out[0]);
}
```

Append to `core/src/abi.zig`:

```zig
test "fb_ring_rms reports per-channel RMS of the newest window" {
    const ring = fb_ring_create(16, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    const in = [_]f32{ 3, 4, 0, 0 };
    fb_ring_write(ring, &in, 4);
    var out: [1]f32 = undefined;
    try std.testing.expectEqual(FbStatus.ok, fb_ring_rms(ring, 4, &out));
    try std.testing.expectEqual(@as(f32, 2.5), out[0]);
}
```

Run → compile error. Red.

- [ ] **Step 2: Implement**

`Ring.zig`, after `peakBins`:

```zig
/// RMS per channel over the newest `n_frames_req` frames, clamped like
/// `read`'s callers: min(req, total_written, capacity). `out.len ==
/// channels`. Reads through `read` in `max_write_frames` chunks — each
/// chunk is a verified seqlock copy, and the scratch is a fixed 32 KiB
/// stack array, so the ring's size never reaches the stack. Sum of
/// squares in f64 (as Summary does) so long windows keep precision.
pub fn rmsLatest(self: *Ring, n_frames_req: u64, out: []f32) ReadError!void {
    std.debug.assert(out.len == self.channels);
    @memset(out, 0);
    const tw = self.total_written.load(.acquire);
    const n = @min(n_frames_req, @min(tw, self.capacity));
    if (n == 0) return;
    var ss: [2]f64 = .{ 0, 0 };
    var scratch: [max_write_frames * 2]f32 = undefined;
    var abs = tw - n;
    var left = n;
    while (left > 0) {
        const take: usize = @intCast(@min(left, max_write_frames));
        const chunk = scratch[0 .. take * self.channels];
        try self.read(abs, chunk);
        for (chunk, 0..) |v, i| ss[i % self.channels] += @as(f64, v) * @as(f64, v);
        abs += take;
        left -= take;
    }
    for (out, 0..) |*o, c| o.* = @floatCast(@sqrt(ss[c] / @as(f64, @floatFromInt(n))));
}
```

`abi.zig`:

```zig
export fn fb_ring_rms(ring: *Ring, n_frames: u64, out: [*]f32) FbStatus {
    ring.rmsLatest(n_frames, out[0..ring.channels]) catch |err| return switch (err) {
        error.Overwritten => .overwritten,
        error.OutOfRange => .out_of_range,
    };
    return .ok;
}
```

`flashback_core.h`: `FbStatus fb_ring_rms(FbRing *, uint64_t n_frames, float *out /* channels */);` with a one-line comment.

`native.py`: declare `lib.fb_ring_rms.argtypes = [C.c_void_p, C.c_uint64, f32p]; lib.fb_ring_rms.restype = C.c_int`. Add to `NativeAudioCircularBuffer` (overrides the inherited method until Task 5 removes the base):

```python
    def get_rms_levels(self, window_seconds: float = 0.1) -> np.ndarray:
        """RMS per channel over the newest window (level meter). Zeros when
        the window is empty or torn -- the engine zeroes `out` on error."""
        out = np.zeros(self.channels, dtype=np.float32)
        self._lib.fb_ring_rms(self._h, max(0, int(window_seconds * self.sample_rate)), _as_f32p(out))
        return out
```

Append to `tests/unit/test_peak_bins_parity.py` (temporary, goes with the file):

```python
def test_zig_rms_matches_numpy_reference():
    from flashback_sampler.core.buffer import RingDerivedOps
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=48_000, channels=2)
    try:
        rng = np.random.default_rng(3)
        buf.write(rng.standard_normal((30_000, 2)).astype(np.float32))
        ref = RingDerivedOps.get_rms_levels(buf, 0.2)   # the numpy path, called unbound
        np.testing.assert_allclose(buf.get_rms_levels(0.2), ref, rtol=1e-5)
    finally:
        buf.close()
```

- [ ] **Step 3: Run, verify green, count +4; mutation**

`zig build --build-file core/build.zig test --summary all` → `<Z0> + 13`. Rebuild the DLL. `python -m pytest tests/unit/test_peak_bins_parity.py tests/unit/test_buffer.py -q -k "rms"` → green (`test_get_rms_levels_*[native]` now run Zig).

Mutation check: `@min(left, max_write_frames)` → `left` → the 9000-frame test panics on the scratch bounds (Debug). Red. Divide by `n * channels` instead of `n` → the stereo case reddens (1 → 0.707). Red.

- [ ] **Step 4: Spec note + commit**

Spec, under "### Deletions": "Deviation (PR f plan): `get_rms_levels` was numpy maths; it now calls `fb_ring_rms` (`Ring.rmsLatest`)."

```bash
git add core/src/Ring.zig core/src/abi.zig core/include/flashback_core.h flashback_sampler/core/native.py tests/unit/test_peak_bins_parity.py docs/superpowers/specs/2026-08-30-zig-core-phase2-d-f-design.md
git commit -m "feat(core): Ring.rmsLatest + fb_ring_rms -- the level meter's RMS leaves Python"
```

---

### Task 3: FLAC out — WAV-only `CheckoutManager`, menu, dialog, tests, README rows

**Files:**
- Modify: `flashback_sampler/core/checkout.py`, `flashback_sampler/core/native.py:32-36`, `flashback_sampler/app/turntable_window.py`, `tests/unit/test_checkout.py`, `tests/unit/test_turntable_window.py`, `README.md:6,39,52`

**Interfaces:**
- Produces: `CheckoutFormat = Literal["WAV"]`; `CheckoutManager.save(…, fmt="WAV", …)` raises `ValueError` for any other format and `RuntimeError` when the native library is missing (from `native.wav_write`, `native.py:198-200`). `TurntableWindow._save_current_clip(self, trimmed: bool = True)` — the `fmt` parameter goes.

- [ ] **Step 1: Failing tests**

In `tests/unit/test_checkout.py` delete `test_save_as_flac_round_trips` (`:331-348`), `test_save_flac_defaults_to_pcm_24` (`:449-452`), `test_save_flac_coerces_float_to_pcm_24` (`:455-458`), `test_flac_save_does_not_use_native_encoder` (`:519-535`); fix the docstrings at `:5` ("WAV or FLAC" → "WAV") and `:493-494` (drop the FLAC sentence). Add, next to `test_save_invalid_format_raises` (`:351`):

```python
def test_save_rejects_flac(tmp_path):
    mgr, co = _mgr_with_checkout()
    with pytest.raises(ValueError):
        mgr.save(co.id, tmp_path / "clip.flac", fmt="FLAC")


def test_save_without_native_library_raises(tmp_path, monkeypatch):
    """No soundfile fallback remains: a missing engine is an error, not a
    silent detour through another encoder."""
    from flashback_sampler.core import native
    mgr, co = _mgr_with_checkout()
    monkeypatch.setattr(native, "load", lambda: None)
    with pytest.raises(RuntimeError):
        mgr.save(co.id, tmp_path / "clip.wav")
```

In `tests/unit/test_turntable_window.py`, next to `test_clip_drag_out_uses_trimmed_range` (`:599`), using the same `qapp`/`state`/`_write_one_second` fixtures that test uses:

```python
def test_save_dialog_offers_wav_only(qapp, state, tmp_path, monkeypatch):
    from flashback_sampler.app import turntable_window as tw
    win = tw.TurntableWindow(state)
    try:
        _write_one_second(state)
        state.active_slot.checkout_manager.create(duration_s=0.5)
        win._refresh_clip_side(auto_select_newest=True)
        seen = {}

        def fake_dialog(parent, title, default_path, filter_spec):
            seen.update(default_path=default_path, filter_spec=filter_spec)
            return "", ""

        monkeypatch.setattr(tw.QFileDialog, "getSaveFileName", staticmethod(fake_dialog))
        win._save_current_clip()
        assert seen["filter_spec"] == "WAV audio (*.wav)"
        assert seen["default_path"].endswith(".wav")
    finally:
        win.close()
```

Verify: `QFileDialog` is imported into `turntable_window`'s namespace (it is called unqualified at `:1197`); `_refresh_clip_side(auto_select_newest=True)` is the call the neighbouring tests use (`:592`).

Run: `python -m pytest tests/unit/test_checkout.py tests/unit/test_turntable_window.py -q` → `test_save_rejects_flac` FAILS (FLAC still accepted), the dialog test FAILS (filter is the two-format string).

- [ ] **Step 2: Implement**

`checkout.py` before → after:
- `:7` "save to WAV/FLAC or discard" → "save to WAV or discard".
- `:20` `import soundfile as sf` → delete.
- `:27` `CheckoutFormat = Literal["WAV", "FLAC"]` → `CheckoutFormat = Literal["WAV"]`.
- `:30-32` → `# FLOAT keeps the float32 ring bit-perfect on disk (fb_wav_write memcpy).` and `_DEFAULT_SUBTYPE: dict[str, str] = {"WAV": "FLOAT"}`.
- `:88` `_VALID_FORMATS = ("WAV", "FLAC")` → `("WAV",)`.
- `:298-303` docstring: "`subtype` … FLOAT for WAV and PCM_24 for FLAC. FLAC + FLOAT coerces to PCM_24 (FLAC has no float subtype)." → "`subtype` controls the bit depth; None resolves to FLOAT."
- `:317-318` (the `if fmt == "FLAC" and subtype == "FLOAT": subtype = "PCM_24"`) → delete.
- `:329-335` →

```python
        # The Zig encoder is the only write path; native.wav_write raises
        # RuntimeError when the library is missing.
        native.wav_write(target, np.ascontiguousarray(audio, dtype=np.float32), sr, subtype)
```

`native.py:32-36` comment on `SUBTYPE_INTS`: drop the "falls back to soundfile" sentence → "Public: mirrors flashback_core.h's FbSubtype and checkout.py's CheckoutSubtype strings."

`turntable_window.py` before → after:
- `:1178` `def _save_current_clip(self, fmt: str | None = None, trimmed: bool = True)` → `def _save_current_clip(self, trimmed: bool = True)`.
- `:1188-1192` → `default_ext = ".wav"` and `filter_spec = "WAV audio (*.wav)"`.
- `:1197` `target, selected = …` → `target, _ = …`.
- `:1202-1208` (the `resolved` block) → delete.
- `:1210-1212` → `slot.checkout_manager.save(co.id, Path(target), fmt="WAV", trimmed=trimmed)`.
- `:1366` → `lambda: self._save_current_clip(trimmed=has_trim)`.
- `:1369-1375` (`act_save_flac` block) → delete.
- `:1380` → `lambda: self._save_current_clip(trimmed=False)`.
- `:646` `self._save_current_clip()` — unchanged.

`README.md`: `:6` "save it to WAV/FLAC or discard it" → "save it as a 32-bit-float WAV or discard it"; `:39` "(WAV or FLAC)" → "(WAV, 32-bit float by default)"; `:52` "(+ WAV/FLAC save)" → "(+ WAV save through `fb_wav_write`)".

- [ ] **Step 3: Verify green; mutation**

`python -m pytest tests/unit/test_checkout.py tests/unit/test_turntable_window.py tests/unit/test_drag_export.py -q` → green (the WAV tests still decode with soundfile until Task 4).

Mutation check: re-add `"FLAC"` to `_VALID_FORMATS` → `test_save_rejects_flac` red. Restore `filter_spec` to the two-format string → the dialog test red. Revert both.

- [ ] **Step 4: Commit**

```bash
git add flashback_sampler/core/checkout.py flashback_sampler/core/native.py flashback_sampler/app/turntable_window.py tests/unit/test_checkout.py tests/unit/test_turntable_window.py README.md
git commit -m "feat: FLAC out -- WAV-only checkout save, one encoder path, menu and dialog follow"
```

---

### Task 4: `tests/fixtures/wavread.py` — stdlib WAV oracle; every soundfile decode site replaced

**Files:**
- Create: `tests/fixtures/wavread.py`, `tests/unit/test_wavread.py`
- Modify: `tests/unit/test_native_smoke.py:163-212`, `tests/unit/test_checkout.py:17,322-327,445-446,465-466,511-513`, `tests/unit/test_drag_export.py:8,57-59,70-72`, `tests/unit/test_turntable_window.py:600,618`, `core/src/wav.zig:3,96`

**Interfaces:**
- Produces: `read_wav(path) -> tuple[np.ndarray, WavInfo]` — samples `float32 (frames, channels)`, `WavInfo(samplerate, channels, subtype, frames)` with `subtype ∈ {"FLOAT", "PCM_16", "PCM_24"}`. PCM is scaled by `2**(bits-1)` (the libsndfile convention the old oracle used), so code 32767 decodes to 32767/32768.
- Header facts it relies on: `wav.zig` writes a fixed 44-byte header (`header_len = 44`, `wav.zig:57`): `"fmt "` chunk of 16 bytes, format tag 3 for float32 and 1 for PCM (`formatTag`, `wav.zig:49-54`), never `WAVE_FORMAT_EXTENSIBLE`. The reader still walks chunks and honours `0xFFFE` + the SubFormat GUID, because a DAW-written fixture, or a future `wav.zig` that emits EXTENSIBLE for > 2 channels, must not break the oracle.

- [ ] **Step 1: Failing tests**

Create `tests/unit/test_wavread.py`:

```python
"""tests/fixtures/wavread.py is the WAV oracle for fb_wav_write. It must
decode what wav.zig writes today (plain 44-byte header, tags 1/3) and a
WAVE_FORMAT_EXTENSIBLE header with a padded odd-sized chunk in front."""
from __future__ import annotations

import struct

import numpy as np
import pytest

from flashback_sampler.core import native
from tests.fixtures.wavread import read_wav

pytestmark = pytest.mark.skipif(native.load() is None, reason="flashback_core not built")


def test_float32_round_trips_bit_exact(tmp_path):
    audio = np.array([[0.25, -0.25], [0.5, -0.5]], dtype=np.float32)
    native.wav_write(tmp_path / "f.wav", audio, 48_000, "FLOAT")
    got, info = read_wav(tmp_path / "f.wav")
    np.testing.assert_array_equal(got, audio)
    assert (info.samplerate, info.channels, info.subtype, info.frames) == (48_000, 2, "FLOAT", 2)


def test_pcm16_decodes_the_documented_quantizer(tmp_path):
    # wav.zig: code = round(x * 32767); decode = code / 32768.
    audio = np.array([1.0, 0.5, -1.0, 0.0], dtype=np.float32)[:, None]
    native.wav_write(tmp_path / "p.wav", audio, 44_100, "PCM_16")
    got, info = read_wav(tmp_path / "p.wav")
    expected = np.array([32767, 16384, -32767, 0], dtype=np.float32) / np.float32(32768.0)
    np.testing.assert_array_equal(got[:, 0], expected)
    assert (info.subtype, info.frames, info.channels) == ("PCM_16", 4, 1)


def test_pcm24_decodes_the_documented_quantizer(tmp_path):
    # code = round(x * 8388607): 0.5 -> 4194303.5 -> 4194304 (half away from zero).
    audio = np.array([0.5, -1.0], dtype=np.float32)[:, None]
    native.wav_write(tmp_path / "q.wav", audio, 48_000, "PCM_24")
    got, info = read_wav(tmp_path / "q.wav")
    expected = np.array([4194304, -8388607], dtype=np.float32) / np.float32(8388608.0)
    np.testing.assert_array_equal(got[:, 0], expected)
    assert info.subtype == "PCM_24"


def test_extensible_header_and_odd_chunk_padding(tmp_path):
    # Hand-built file: LIST chunk of 3 bytes (+1 pad), then a 40-byte
    # WAVE_FORMAT_EXTENSIBLE fmt whose SubFormat GUID starts 0x0003
    # (IEEE float), then one float frame.
    fmt = struct.pack("<HHIIHH", 0xFFFE, 1, 8000, 32000, 4, 32)
    fmt += struct.pack("<HHI", 22, 32, 0x4)
    fmt += struct.pack("<H", 3) + bytes.fromhex("0000000010008000 00AA00389B71".replace(" ", ""))
    assert len(fmt) == 40
    data = struct.pack("<f", 0.75)
    body = b"WAVE"
    body += b"LIST" + struct.pack("<I", 3) + b"abc" + b"\x00"
    body += b"fmt " + struct.pack("<I", 40) + fmt
    body += b"data" + struct.pack("<I", 4) + data
    (tmp_path / "x.wav").write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    got, info = read_wav(tmp_path / "x.wav")
    assert info == type(info)(8000, 1, "FLOAT", 1)
    assert got[0, 0] == np.float32(0.75)
```

Run: `python -m pytest tests/unit/test_wavread.py -q` → `ModuleNotFoundError: tests.fixtures.wavread`. Red.

- [ ] **Step 2: Implement `tests/fixtures/wavread.py`**

```python
"""Minimal RIFF/WAVE reader for tests: FLOAT32 and PCM16/24, plain or
WAVE_FORMAT_EXTENSIBLE headers. `struct` walks the chunks; numpy decodes
the samples. This is the oracle for fb_wav_write, so it shares no code
with wav.zig and never calls the engine."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_EXTENSIBLE = 0xFFFE
_TAGS = {1: "PCM", 3: "FLOAT"}


@dataclass(frozen=True)
class WavInfo:
    samplerate: int
    channels: int
    subtype: str  # "FLOAT" | "PCM_16" | "PCM_24"
    frames: int


def read_wav(path) -> tuple[np.ndarray, WavInfo]:
    """(samples float32 (frames, channels), info). PCM codes are scaled by
    2**(bits-1) -- the libsndfile convention -- so 32767 reads as 32767/32768."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid, size = struct.unpack_from("<4sI", raw, pos)
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)  # chunks are word-aligned
    if fmt is None or data is None:
        raise ValueError("missing fmt or data chunk")
    tag, channels, rate, _byte_rate, _block_align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if tag == _EXTENSIBLE:
        # cbSize(2) validBits(2) channelMask(4) precede the SubFormat GUID
        # at offset 24; its first two bytes carry the real format tag.
        (tag,) = struct.unpack_from("<H", fmt, 24)
    kind = _TAGS.get(tag)
    if kind == "FLOAT" and bits == 32:
        samples = np.frombuffer(data, dtype="<f4").astype(np.float32)
        subtype = "FLOAT"
    elif kind == "PCM" and bits == 16:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / np.float32(32768.0)
        subtype = "PCM_16"
    elif kind == "PCM" and bits == 24:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        codes = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        codes = np.where(codes & 0x800000, codes - 0x1000000, codes)  # sign-extend
        samples = codes.astype(np.float32) / np.float32(8388608.0)
        subtype = "PCM_24"
    else:
        raise ValueError(f"unsupported format tag {tag:#x} at {bits} bits")
    frames = samples.shape[0] // channels
    return samples.reshape(frames, channels), WavInfo(rate, channels, subtype, frames)
```

Run `tests/unit/test_wavread.py` → green (4).

Mutation check: drop `+ (size & 1)` → the EXTENSIBLE test reddens (misaligned walk). Drop the `_EXTENSIBLE` branch → same test reddens with "unsupported format tag 0xfffe". Change `32768.0` → `32767.0` → the PCM16 test reddens. Revert each.

- [ ] **Step 3: Replace every soundfile decode site**

`tests/unit/test_native_smoke.py:163-212` → replace both tests and the `:178-200` comment block with:

```python
def test_wav_float32_round_trips_bit_exact(tmp_path):
    from tests.fixtures.wavread import read_wav
    rng = np.random.default_rng(7)
    audio = rng.uniform(-1, 1, size=(4801, 2)).astype(np.float32)
    native.wav_write(tmp_path / "zig.wav", audio, 48_000, "FLOAT")
    got, info = read_wav(tmp_path / "zig.wav")
    assert (info.samplerate, info.channels, info.frames) == (48_000, 2, 4801)
    np.testing.assert_array_equal(got, audio)  # FLOAT32 is a memcpy of the f32 bits (wav.zig:84-90)


# wav.zig quantizes with scale 32767 / 8388607 (not 32768 / 8388608) so
# +1.0 needs no clamp; -1.0 lands one LSB short of the negative rail
# (wav.zig:91-96). @round is half-away-from-zero, hence the sign/floor form.
@pytest.mark.parametrize("subtype,scale,denom", [("PCM_16", 32767.0, 32768.0), ("PCM_24", 8388607.0, 8388608.0)])
def test_wav_pcm_codes_match_the_documented_quantizer(tmp_path, subtype, scale, denom):
    from tests.fixtures.wavread import read_wav
    rng = np.random.default_rng(11)
    audio = rng.uniform(-1, 1, size=(997, 2)).astype(np.float32)
    native.wav_write(tmp_path / "zig.wav", audio, 48_000, subtype)
    got, _ = read_wav(tmp_path / "zig.wav")
    v = (audio * np.float32(scale)).astype(np.float64)  # f32 multiply as in wav.zig, then exact rounding in f64
    codes = np.sign(v) * np.floor(np.abs(v) + 0.5)     # half away from zero == Zig @round
    np.testing.assert_array_equal(got, codes.astype(np.float32) / np.float32(denom))
```

`tests/unit/test_checkout.py`: `:17` `import soundfile as sf` → `from tests.fixtures.wavread import read_wav`; `:322-327` →

```python
    data, info = read_wav(target)
    assert info.samplerate == 48_000
    assert data.shape == (9600, 1)
    assert info.subtype == "FLOAT"
```
(keep the `np.allclose(data, co.audio, atol=1e-7)` line); `:445-446` → `assert read_wav(target)[1].subtype == "FLOAT"`; `:465-466` → `assert read_wav(target)[1].subtype == "PCM_16"`; `:511-513` → `data, info = read_wav(target)` / `assert info.samplerate == co.sample_rate`.

`tests/unit/test_drag_export.py`: `:8` → `from tests.fixtures.wavread import read_wav`; `:57-59` → `_, info = read_wav(path)` then the same two asserts on `info.subtype`/`info.frames`; `:70-72` likewise.

`tests/unit/test_turntable_window.py`: `:600` `import soundfile as sf` → `from tests.fixtures.wavread import read_wav`; `:618` → `assert read_wav(files[0])[1].frames == n // 2 - n // 4`.

`core/src/wav.zig:3` "Parity vs soundfile is DECODE-equality" → "Parity is checked by `tests/fixtures/wavread.py`, an independent stdlib reader"; `:96` "the documented contract Task 7 checks against soundfile" → "the documented contract `tests/unit/test_native_smoke.py` pins through `wavread.py`".

- [ ] **Step 4: Verify; commit**

`grep -rn "soundfile\|import sf\|sf\." tests flashback_sampler core/src` → zero hits. `python -m pytest tests/unit -q -m "not audio_hw and not perf"` → green. `zig fmt --check core/src`.

```bash
git add tests/fixtures/wavread.py tests/unit/test_wavread.py tests/unit/test_native_smoke.py tests/unit/test_checkout.py tests/unit/test_drag_export.py tests/unit/test_turntable_window.py core/src/wav.zig
git commit -m "test: stdlib WAV oracle (tests/fixtures/wavread.py) replaces every soundfile decode"
```

---

### Task 5: Delete `buffer.py` — `native.py` stands alone; every importer, fixture, and test moves to the native class

**Files:**
- Modify: `flashback_sampler/core/native.py`, `flashback_sampler/core/__init__.py`, `flashback_sampler/core/checkout.py`, `flashback_sampler/core/capture_slot.py`, `flashback_sampler/app/state.py`, `flashback_sampler/app/audio_devices.py:184`, `flashback_sampler/app/turntable_window.py:85`, `flashback_sampler/app/widgets/waveform_view.py:7`, `tests/conftest.py`, `tests/fixtures/fake_capture.py`, `tests/hw/test_native_capture_hw.py`, `tests/unit/test_buffer.py`, `tests/unit/test_capture_source.py`, `tests/unit/test_checkout.py`, `tests/unit/test_drag_export.py`, `tests/unit/test_app_state.py`, `tests/unit/test_capture_slot.py`, `tests/unit/test_audio_devices.py:8`, `tests/unit/test_native_smoke.py:33-35,142-160`, `core/src/Ring.zig:42-47`, `core/src/Summary.zig:164`, `soak_test.py:21,26`
- Move to `_ToRemove/`: `flashback_sampler/core/buffer.py`, `tests/unit/test_peak_bins_parity.py`

**Interfaces:**
- **Plan choice:** `buffer.py` is deleted entirely; no factory replaces `make_ring_buffer` — call sites construct `NativeAudioCircularBuffer(duration_seconds=…, sample_rate=…, channels=…)` directly. `native.py` keeps `NativeAudioCircularBuffer` with the derived accessors folded in as unit conversions: `gain_db` (dB ↔ linear), `buffered_seconds`, `is_full`, `capacity_bytes`, `status()`. `self.buffer` (the zero-copy view, `native.py:236-241`) and `write_pos` stay: no production reader remains, but the tests pin the physical layout and flush zeroing through them (`test_buffer.py:43-45,113,146,456,497`) and `fb_ring_storage` stays in the ABI for non-Python hosts.
- **Plan choice:** `tests/conftest.py` exits the session with the build instruction whenever the library is missing, not only under `FLASHBACK_REQUIRE_NATIVE=1` — there is no Python half left to fall back to. CI still sets the variable (`.github/workflows/test.yml:34`); it becomes redundant, not wrong.
- Produces: `flashback_sampler/core/__init__.py` is a docstring only.

- [ ] **Step 1: Failing tests**

`tests/unit/test_buffer.py`: replace `:1-29` with

```python
"""AudioCircularBuffer's behaviour contract, now served by the Zig core
alone: NativeAudioCircularBuffer over Ring.zig. The `buffer_cls` fixture
survives as a name so the 37 contract tests read unchanged."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from flashback_sampler.core import native as native_mod
from tests.fixtures.sine_source import ramp_block


@pytest.fixture
def buffer_cls():
    return native_mod.NativeAudioCircularBuffer
```

Delete `test_write_wraps_around_end_of_ring` and its lead comment (`:118-138` — Python-only: its indices assume the physical modulus equals `buffer_size`), and the `make_ring_buffer` section (`:705-732`). Rewrite the comments at `:72-75`, `:230`, `:321`, `:468-469` so they no longer contrast two implementations.

`tests/unit/test_native_smoke.py`: delete `test_load_skips_a_candidate_that_exists_but_is_not_a_valid_library` (`:142-160` — it asserts the fallback contract) and add in its place:

```python
def test_load_skips_a_candidate_that_exists_but_is_not_a_valid_library(tmp_path, monkeypatch):
    """A bundled-but-broken library must not crash load(); it reports None
    and the constructor raises a clear RuntimeError."""
    bad = tmp_path / "not_a_real_library.dll"
    bad.write_text("this is not a shared library")
    monkeypatch.setattr(native, "_candidates", lambda: [bad])
    monkeypatch.setattr(native, "_lib", None)
    monkeypatch.setattr(native, "_lib_tried", False)
    assert native.load() is None
    with pytest.raises(RuntimeError):
        native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8, channels=1)
```
(Same name, new contract; count-neutral.) Trim `:33-35` (the `AudioCircularBuffer` broadcast comparison) to "raising is deliberate: broadcasting would mask a caller bug".

Add to `tests/unit/test_app_state.py`, next to `test_appstate_wires_core_objects…` (`:19`):

```python
def test_core_package_exports_nothing_from_a_python_buffer():
    import flashback_sampler.core as core
    assert not hasattr(core, "AudioCircularBuffer")
```

Run: `python -m pytest tests/unit/test_buffer.py tests/unit/test_app_state.py -q` → the new package test FAILS (`core/__init__.py:1` still re-exports); everything else still passes (nothing deleted yet). Red on one test is the gate here; the rest of this task is a mechanical move whose gate is Step 3's grep + full run.

- [ ] **Step 2: Implement**

`native.py`:
- `:1-19` docstring → describe the class as the ring handle: Zig owns memory, writes, reads, peaks (`fb_ring_peak_bins`), RMS (`fb_ring_rms`), summary, WAV; Python holds the handle, converts units, and keeps `self.buffer` as a zero-copy view "with no production reader — tests inspect it; `write_pos` wraps at `storage_frames`". Keep the TWO SIZES paragraph (it still governs `self.buffer`'s shape and `write_pos`).
- `:29` `from flashback_sampler.core.buffer import RingDerivedOps, _peak_bins_impl` → delete; add `import math`.
- `:213` `class NativeAudioCircularBuffer(RingDerivedOps):` → `class NativeAudioCircularBuffer:`; `:214` docstring → "The app's ring buffer: a handle on a Zig `Ring`."
- `:236-241` comment: "Visualization readers (get_peak_bins) iterate this directly" → "No production reader; tests pin flush zeroing and the physical layout through it."
- Add after `gain` (`:257-263`), ported from `buffer.py:184-238` with the two-implementation prose removed:

```python
    @property
    def gain_db(self) -> float:
        """Record gain in dB; -inf when muted."""
        from flashback_sampler.core.source_status import dbfs
        return dbfs(self.gain)

    @gain_db.setter
    def gain_db(self, db: float) -> None:
        self.gain = 0.0 if db == -math.inf else float(10.0 ** (db / 20.0))

    @property
    def buffered_seconds(self) -> float:
        return min(self.total_written, self.buffer_size) / self.sample_rate

    @property
    def is_full(self) -> bool:
        return self.total_written >= self.buffer_size

    @property
    def capacity_bytes(self) -> int:
        """Bytes of the READABLE window (buffer_size × channels × 4) -- the
        RAM-accounting number (AppState.total_project_ram_bytes). Not
        self.buffer.nbytes, which includes the guard band."""
        return self.buffer_size * self.channels * 4

    def status(self) -> dict:
        return {
            "buffered_seconds": round(self.buffered_seconds, 1),
            "buffer_capacity_seconds": self.duration,
            "fill_percent": round(100 * self.buffered_seconds / self.duration, 1),
            "write_pos": self.write_pos,
            "total_written_samples": self.total_written,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "memory_mb": round(self.capacity_bytes / 1_048_576, 1),
        }
```
Verify: `buffer.py:187` imports `dbfs` lazily; keep it lazy unless `source_status.py` provably does not import `native` (check with `grep -n import flashback_sampler/core/source_status.py`).
- `:398-414` `close()` docstring: drop the `AudioCircularBuffer` comparison; keep "never use the buffer after close()".
- `:341-356` `copy_abs_range` docstring: drop "Public counterpart to AudioCircularBuffer.copy_abs_range" and the `mixed_capture.py` mention (gone with PR d); keep the retry rationale.
- `:300-303`, `:314-324` comments: "Unlike the Python impl" → "There is no lock between the snapshot and the read".

`flashback_sampler/core/__init__.py` → `"""Audio core: ctypes handles over the Zig library (native.py) and the Python shell around them."""`.

`checkout.py`: `:22` → `from flashback_sampler.core.native import NativeAudioCircularBuffer`; `:92` `buffer: RingDerivedOps` → `buffer: NativeAudioCircularBuffer`; `:157-160` comment → "total_written is one atomic read through the ABI".

`state.py`: `:24` → `from flashback_sampler.core.native import NativeAudioCircularBuffer`; `:176-179` comment → "capacity_bytes: the readable window, not the guard-banded storage"; `:225` annotation; `:403` `make_ring_buffer(` → `NativeAudioCircularBuffer(`; `:416-417` comment → "deterministic release of the Zig-owned handle".

`capture_slot.py`: `:27` → the same import; `:44-45` docstring → "its own ring buffer (`NativeAudioCircularBuffer`) + CheckoutManager"; `:65` annotation; `:124` constructor.

`audio_devices.py:184` docstring "`buffer` is an AudioCircularBuffer" → "`buffer` is the slot's NativeAudioCircularBuffer". `turntable_window.py:85` "Same shape as AudioCircularBuffer.get_peak_bins" → "Same shape as NativeAudioCircularBuffer.get_peak_bins". `waveform_view.py:7` likewise.

`tests/conftest.py:19-33` → unconditional:

```python
def pytest_sessionstart(session: pytest.Session) -> None:
    from flashback_sampler.core import native

    if native.load() is None:
        pytest.exit(
            "flashback_core native library not found (checked core/zig-out/bin, "
            "core/zig-out/lib, and flashback_sampler/core). There is no Python "
            "ring buffer any more, so nothing can run without it. Build it: "
            "`zig build --build-file core/build.zig -Doptimize=ReleaseSafe`.",
            returncode=1,
        )
```
Rewrite the module docstring (`:1-13`) to match; `FLASHBACK_REQUIRE_NATIVE` is no longer read.

`tests/fixtures/fake_capture.py`: `:6` "without pulling in soundcard / sounddevice" → "without touching the Zig capture backend"; `:16` import → delete; `:22`, `:28`, `:91` → `buffer` untyped, docstring "any ring with `write(frames)`".

`tests/hw/test_native_capture_hw.py`: `:9` → `from flashback_sampler.core.native import NativeAudioCircularBuffer`; `:30`, `:52`, `:67` `make_ring_buffer(` → `NativeAudioCircularBuffer(`.

`soak_test.py`: `:21` → the same import; `:26` → `NativeAudioCircularBuffer(`. (Task 8 does the full port.)

Constructor sites, one sed each (word boundary keeps `NativeAudioCircularBuffer(` from doubling):

```bash
sed -i 's/from flashback_sampler.core.buffer import AudioCircularBuffer/from flashback_sampler.core.native import NativeAudioCircularBuffer/; s/\bAudioCircularBuffer(/NativeAudioCircularBuffer(/g' tests/unit/test_checkout.py tests/unit/test_drag_export.py tests/unit/test_capture_source.py
```
Then fix by hand: `test_capture_source.py:17` return annotation `-> AudioCircularBuffer` → `-> NativeAudioCircularBuffer`; `test_app_state.py:14` → `from flashback_sampler.core.native import NativeAudioCircularBuffer`, `:21-24` → `assert isinstance(st.buffer, NativeAudioCircularBuffer)`, `:240-241` and `:453-454` comments; `test_capture_slot.py:10`, `:31-33` likewise; `test_audio_devices.py:8` "a fake AudioCircularBuffer" → "a fake ring buffer".

Zig prose: `Ring.zig:42-47` → "4096 frames ≈ 85 ms at 48 kHz: fine enough for smooth rolling, coarse enough that the summary stays tiny (≈330 KB for 15 min)." (the Python parity clause is gone); `Summary.zig:164` "Mirror of buffer.py get_summary_bins (buffer.py:421)" → "Aggregates frozen slots into display bins; n_avail clamps against `capacity_frames` (the ring's readable window)".

Sequester (the recipe from part-1 Tasks 7/10 — `_ToRemove/` is gitignored, `git mv` into it stages nothing):

```bash
mkdir -p _ToRemove/flashback_sampler/core _ToRemove/tests/unit
mv flashback_sampler/core/buffer.py _ToRemove/flashback_sampler/core/
mv tests/unit/test_peak_bins_parity.py _ToRemove/tests/unit/
git add -u flashback_sampler tests
```

The PR diff shows plain deletions; the bytes survive under `_ToRemove/` for the owner's one-shot approval in Task 9.

- [ ] **Step 3: Verify**

`grep -rn "core.buffer\|AudioCircularBuffer\b\|make_ring_buffer\|RingDerivedOps\|_peak_bins_impl\|buffer_cls.*params" flashback_sampler tests soak_test.py core/src core/include *.md packaging flashback_sampler.spec | grep -v "NativeAudioCircularBuffer"` → zero hits outside `PHASE2-HANDOFF.md` (untracked history) and `docs/superpowers/` (specs/plans stay as written).

`python -m pytest tests/unit -q -m "not audio_hw and not perf"` → green. Expected count: `<P0>` − 37 (the `[python]` parametrizations of the 38 `buffer_cls` tests minus the perf-marked one, which is deselected) − 1 (`test_write_wraps_around_end_of_ring`) − 2 (`make_ring_buffer` tests) − 4 (FLAC, Task 3) − 8 (parity file, Tasks 1–2) + 2 (Task 3 checkout) + 1 (Task 3 dialog) + 4 (Task 4 wavread) + 1 (package export). Record the actual number in the sub-issue; explain any difference.

Mutation check: put `from .buffer import AudioCircularBuffer` back into `core/__init__.py` → `ImportError` at collection (the module is gone) — the whole suite reddens. That is the pin; revert. Then: temporarily hide the DLL (`ren core\zig-out\bin\flashback_core.dll flashback_core.dll.off`) → `pytest` exits 1 with the build message from `conftest.py`; restore.

App smoke: `python -m flashback_sampler.app.main`, arm a loopback slot, play audio: waveform moves (peak bins via Zig), the level meter moves (RMS via Zig), FLUSH clears, checkout + SAVE writes a WAV that `read_wav` opens.

- [ ] **Step 4: Commit**

```bash
git add flashback_sampler tests core/src/Ring.zig core/src/Summary.zig
git add -u flashback_sampler tests
git commit -m "refactor: delete the Python ring buffer -- NativeAudioCircularBuffer is the only ring; tests run native-only"
```

---

### Task 6: Dependencies out; stale comments; the grep gate

**Files:**
- Modify: `pyproject.toml:14-16`, `requirements.txt:2-4`, `flashback_sampler.spec:13-14,25,34-38`, `packaging/README.md:28,38,41,46`, `flashback_sampler/core/capture_source.py:5-15,29`, `flashback_sampler/core/drag_export.py:4`, `README.md:16`

- [ ] **Step 1: The failing gate**

Run the gate before editing so its red state is on record:

```bash
grep -rn "sounddevice\|soundcard\|soundfile" flashback_sampler tests soak_test.py flashback_sampler.spec pyproject.toml requirements.txt packaging README.md PLATFORM.md core/src
```
Expected now: hits at exactly the file:lines listed above (plus any PR e left). Expected after Step 2: zero.

- [ ] **Step 2: Implement**

- `pyproject.toml:14-16` → delete the three lines (`numpy`, `PySide6`, `platformdirs` remain).
- `requirements.txt:2-4` → delete.
- `flashback_sampler.spec:13-14` → `- No pip package ships native DLLs any more; the only native binary is flashback_core.dll (below).`; `:25` `from PyInstaller.utils.hooks import collect_all` → delete; `:34-38` (the `for pkg in (…): collect_all` loop) → delete. `datas = []`, `binaries = []`, `hiddenimports = ["flashback_sampler.io"]` stay.
- `packaging/README.md:28` bullet → `- **No `collect_all` for audio packages** — capture, mixing, playback, and WAV encoding all live in `flashback_core.dll`; the only pip packages are numpy, PySide6, platformdirs.`; `:38` "via `soundcard`" → "via the Zig core"; `:41` "Tests `soundfile` / `libsndfile` made it into the bundle." → "Tests `fb_wav_write` in the bundled `flashback_core.dll`."; `:46` (the `soundcard` Realtek rough edge) → delete.
- `capture_source.py:5-15` docstring → "The concrete backend is `core/native_capture.py` (`NativeCaptureSource`, one Zig `Capture` per source) and `<MIXED_MODULE>` (`NativeMixedSource`, N sources into one ring). This module is import-cheap: no Qt, no ctypes, so unit tests can instantiate fake sources."; `:29` "concrete CaptureSource -> AudioCircularBuffer.write(frames)" → "concrete CaptureSource -> Ring.write (in Zig; fakes call NativeAudioCircularBuffer.write)".
- `drag_export.py:4` "Pure Python + soundfile — no Qt." → "Pure Python — no Qt; the write goes through CheckoutManager.save and fb_wav_write."
- `README.md:16` → "Installs the package plus test deps (numpy, PySide6, platformdirs; no audio pip packages). **Capture, mixing, and preview playback are Windows-only in this phase** — WASAPI through the Zig core; the core cross-compiles, but `WasapiBackend.zig` is the only `Backend` so far. The test suite needs the built core library (`zig build --build-file core/build.zig -Doptimize=ReleaseSafe`)."

Do NOT run `pip uninstall`; the packages stay installed in the owner's environment. Record in the PR body that `pip install -e ".[dev]"` on a fresh venv is the check that nothing imports them.

- [ ] **Step 3: Verify**

- The grep from Step 1 → zero hits.
- `grep -rn "^import sound\|^from sound\|import sound" flashback_sampler tests soak_test.py` → zero.
- Import smoke: `python -c "import flashback_sampler.app.main, flashback_sampler.app.state, flashback_sampler.core.checkout, flashback_sampler.core.native, flashback_sampler.core.drag_export, tests.fixtures.fake_capture"` → exit 0.
- `python -m flashback_sampler.app.main --help` → prints the argparse usage and exits 0 (`_parse_args` runs before `QApplication`, `main.py:47-49`).
- `python -m pytest tests/unit -q -m "not audio_hw and not perf"` → green, count unchanged from Task 5.
- Mutation check (the gate itself): add `import soundfile` to `drag_export.py` → the import smoke fails on a venv without it, and the grep gate reports one hit; revert. This proves the gate reads the paths it claims to.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml requirements.txt flashback_sampler.spec packaging/README.md flashback_sampler/core/capture_source.py flashback_sampler/core/drag_export.py README.md
git commit -m "chore: drop sounddevice, soundcard, soundfile -- numpy + PySide6 + platformdirs are the only pip deps"
```

---

### Task 7: Docs — README/PLATFORM Windows-only wording, arm-time RAM, ZIG-101 stale note, parent-spec deviations block

**Files:**
- Modify: `README.md:8,50-58`, `PLATFORM.md:9-17,22-32,42,50-51`, `ZIG-101.md` (untracked, owner's — one note at the top), `docs/superpowers/specs/2026-08-16-zig-core-phase2-design.md` (new block after `:247`'s section)

- [ ] **Step 1: README**

- `:8` "The audio core is intentionally framework-agnostic (pure Python + numpy, no Qt imports)…" → "The audio engine is a zero-dependency Zig library (`core/`): capture, mixing, playback, the ring, peaks, and WAV encoding run there. Python is a Qt shell that creates handles, starts and stops them, and reads numbers — so the engine can later sit inside a DAW plugin or an OBS dock unchanged."
- `:50` tree comment "pure Python + numpy — no Qt / soundcard / sounddevice" → "ctypes shell over the Zig core — no Qt"; `:51` `buffer.py` line → delete; `:53` `scrub_player.py` and `:56` `mixed_capture.py` lines → the names PR d/e shipped (`<MIXED_MODULE>`, the playback module; Verify with `ls flashback_sampler/core`); add a line `core/ (repo root)   # Zig engine: Ring, Summary, Capture, Mixer, Playback, WasapiBackend, wav`.
- Add under "## Run" a short "## Memory" paragraph: "Arming a slot reserves its whole ring up front: `seconds × rate × channels × 4` bytes (the FULL preset — 900 s, 48 kHz, stereo — is 345.6 MB, the number the #17 soak recorded). The project RAM budget (Preferences; default 4096 MB, `state.py:43`) refuses a slot that would exceed it (`state.py:134-142`); a ring the OS cannot commit fails at `fb_ring_create` with `out_of_memory` (PR d, #41), which the UI reports as a MemoryError until #16 gives it a home."

- [ ] **Step 2: PLATFORM.md**

- `:12` "✅ WASAPI (ctypes)" → "✅ WASAPI (Zig core)".
- After `:13` add two rows: `| Multi-source mixing | ✅ Zig core (\`Mixer.zig\`) | ⬜ | ⬜ |` and `| Preview playback | ✅ WASAPI render (Zig core) | ⬜ not yet | ⬜ not yet |`.
- `:22-32` → keep the first three sentences (the library, ctypes, cross-compile); replace `:28-32` ("`flashback_sampler/core/buffer.py`'s `make_ring_buffer()` picks … falls back to a pure-Python implementation …") with: "The app requires the native library; there is no Python fallback (phase 2 PR f deleted it). Without it `NativeAudioCircularBuffer` raises `RuntimeError` at construction and the test session exits with the build instruction (`tests/conftest.py`). Capture, mixing, and playback need a `Backend` implementation (`core/src/Backend.zig`); `WasapiBackend.zig` is the only one, so those three are Windows-only today even though the library builds everywhere."
- `:42` seam row: add `core/Mixer.zig`, `core/Playback.zig` to the file list.
- `:50-51` checklist step 1 → "Add a `Backend` implementation in `core/src/` (`enumerate`, `open`, `openRender` — the vtable in `Backend.zig`); Python needs no new code." Verify the vtable names against what PR e shipped.

- [ ] **Step 3: ZIG-101.md note (untracked; add, do not rewrite)**

Insert after line 1 (`# Zig 101, taught through \`core/\``):

```markdown
> **Stale after phase 2 (2026-08-30).** Written against phase 1; these
> sections describe mechanisms phase 2 replaced. The code is the truth:
> §2.5 (atomics) — `writer_active` is now control-thread-owned (`Capture.start`/`stop`, `Mixer`), PR d.
> §3.1 — flush is deferred to the writer (`Ring.flush` / `drainPendingFlush`, #20), not a plain store.
> §3.5 — `Summary` is a seqlock (`gen`), #23.
> §3.8 — Python no longer walks the zero-copy view for peaks; `fb_ring_peak_bins` / `fb_ring_rms` do the reads in Zig. The view remains for tests only.
> §4 "Accepted compromises": #20, #21, #23 are fixed (PRs c and part-1 Task 4); #26 is closed by PR f (no Python write path remains).
> Not covered at all: `Mixer.zig`, `Playback.zig`, the render backend, `Ring.peakBins`.
```

Before inserting, `grep -n "writer_active\|#20\|#21\|#23\|#26\|zero-copy\|get_peak_bins\|visualization" ZIG-101.md` and adjust the list to the headings actually found (the verified anchors are `:439`, `:561`, `:656`, `:694-727`).

- [ ] **Step 4: Parent spec deviations block**

Append to `docs/superpowers/specs/2026-08-16-zig-core-phase2-design.md`:

```markdown
## Deviations recorded by part 2 (2026-08-30)

Part 2 (PRs d, e, f) is specified in
`2026-08-30-zig-core-phase2-d-f-design.md`, which wins where the two
differ. Recorded there by the PR f plan: `fb_ring_peak_bins` takes a
window length, not absolute bounds; `fb_ring_rms` moves the level
meter's RMS into Zig; `tests/fixtures/wavread.py` replaces soundfile as
the WAV oracle; the test session hard-requires the native library.
```

- [ ] **Step 5: Commit**

```bash
git add README.md PLATFORM.md docs/superpowers/specs/2026-08-16-zig-core-phase2-design.md
git commit -m "docs: Windows-only capture/mixing/playback, arm-time RAM, part-2 deviations pointer"
```
(`ZIG-101.md` is untracked and stays so.)

---

### Task 8: Soak + closure — port `soak_test.py`, owner runs 300 s twice, numbers on #17, close #26 and #17

**Files:**
- Modify: `soak_test.py` (untracked; stays untracked)

- [ ] **Step 1: Port the script (one script, two modes)**

Current script (`soak_test.py:1-67`) builds one ring and one `NativeCaptureSource`. Edits:

- `:15-22` imports → add `import argparse`; drop `from flashback_sampler.core import native`; keep `NativeAudioCircularBuffer` (Task 5) and `NativeCaptureSource`; add `from flashback_sampler.core.<MIXED_MODULE> import <MIXED_CLASS>` (Task 0 names).
- `:24` `SECONDS = …` → 

```python
p = argparse.ArgumentParser(description=__doc__)
p.add_argument("seconds", nargs="?", type=int, default=300)
p.add_argument("--mixed", action="store_true", help="2-source NativeMixedSource: default loopback + default mic")
args = p.parse_args()
SECONDS = args.seconds
```
- `:26-34` →

```python
buf = NativeAudioCircularBuffer(duration_seconds=900.0, sample_rate=48_000, channels=2)
if args.mixed:
    specs = [dict(kind="loopback", device_id="", pid=0), dict(kind="input", device_id="", pid=0)]
    cap = <MIXED_CLASS>(buf, specs=specs, sample_rate=48_000, channels=2)   # Verify: the constructor PR d shipped
    engine = "NativeMixedSource(2)"
else:
    cap = NativeCaptureSource(buf, kind="loopback")
    engine = "NativeCaptureSource"
print(f"engine        : {engine}")
print(f"duration      : {SECONDS}s — play audio now\n")
peaks: list[float] = []
cap.start()
```
- `:1-11` docstring → "Xrun soak against the Zig engine. `python soak_test.py 300` (one loopback source) and `python soak_test.py 300 --mixed` (two sources through the mixer). Play audio for the whole run." Keep the per-5-s print loop, the `peaks` signal check, and the summary block (`:36-67`) unchanged; `cap.close()` stays (both wrappers expose `close`, spec `:134-139`).

Verify before the run: `python -c "import ast,sys; ast.parse(open('soak_test.py').read())"` and a 10 s dry run of each mode.

- [ ] **Step 2: Owner runs (hand the two commands over; do not run them in the session)**

`python soak_test.py 300` then `python soak_test.py 300 --mixed`, audio playing throughout. Then the app: arm the FULL preset on a loopback slot, record once, read Task Manager RSS and CPU idle-armed — the same procedure as the "after PR a" measurement on #17 (RSS 750–755 MB after first record, CPU 1.5–2.8 %, #17 comment "Phase 2 — after PR a").

- [ ] **Step 3: Comment the table on #17**

Heading "## Phase 2 — after PR f", columns `before (Python capture)` / `after PR a` / `after PR f (single)` / `after PR f (mixed ×2)`, rows: elapsed, frames written, frames expected, shortfall, xruns, blocks with signal, RSS idle-armed, CPU idle-armed. Copy the "before" and "after PR a" cells from the existing comments.

- [ ] **Step 4: Close #26**

Comment: `grep -rn "fb_ring_write\|\.write(" flashback_sampler` → the only production caller of `fb_ring_write` is `NativeAudioCircularBuffer.write` (`native.py:265-283`), used by tests and fakes; capture and mixing write from Zig threads (`Capture.zig`, `Mixer.zig`) and never cross the GIL. Paste the PR f soak rows (xruns, shortfall) beside #26's original 0.46–2.98 ms tail figures. `gh issue close 26 --comment "<that>"`.

- [ ] **Step 5: Epic closure**

After the owner merges PR f: tick `d`, `e`, `f` on #17 (a–c are ticked already, issue body lines 9-11); `gh issue close 17 --comment "All six PRs merged; final numbers above."`. (`Closes #<F>` in the PR body fires on merge because `dev` is the default branch — repo `CLAUDE.md`; #17 is closed by hand because no PR references it.) Then `git branch -r` and post the remote branches from this arc (`feat/zig-capture`, `feat/zig-process-loopback`, `feat/zig-flush-summary`, the d/e branches, `feat/zig-buffer-only`, `docs/phase2-d-f-spec`) on #17 as a deletion list for approval. Do not delete any branch.

---

### Task 9: PR f hand-off

- [ ] **Step 1: Local gate (all of it, in this order)**

- `zig fmt --check core/build.zig core/src`
- `zig build --build-file core/build.zig test --summary all` → `<Z0> + 13` (Task 1: 7 Ring + 2 abi; Task 2: 3 Ring + 1 abi). The gate is "the count rose by 13", not "green".
- `zig build --build-file core/build.zig -Doptimize=ReleaseSafe`; `-Dtarget=x86_64-linux-gnu`; `-Dtarget=aarch64-macos` (cross-compile legs; nothing in this PR is OS-gated, so all three must build).
- `python -m pytest tests/unit -q -m "not audio_hw and not perf"` → green; record the count (Task 5 Step 3's arithmetic).
- `python -m pytest tests/hw -m audio_hw -s -q` (Windows box, audio playing) → the three capture tests still pass on the native constructor.
- The Task 6 grep gate and import smoke → zero hits, exit 0.
- App smoke from Task 5 Step 3.

- [ ] **Step 2: Push, open the PR against `dev`**

Title: `feat: Python buffer out -- peaks and RMS in Zig, wavread oracle, deps and FLAC gone`

Body:
- What/why (3 bullets): no audio frame is produced, mixed, played, downsampled, or metered by Python; three pip deps and FLAC gone; tests run native-only.
- `Closes #<F>`. (#26 and #17 closed by hand in Task 8.)
- The soak table (Task 8) and the local-gate counts (`<Z0> + 13`, the pytest number). "No CI fires on this PR (budget); every gate above ran locally."
- Deviations recorded in the spec: window-length `fb_ring_peak_bins`; `fb_ring_rms`; `wavread.py`; conftest hard-requires the library.
- **Zig concepts in this PR:**
  - *Seqlock reads for display, in place.* `peakBins` scans `frames` without copying and validates afterwards with the same two-clause check as `read` (`Ring.zig`, "Seqlock verify"); `rmsLatest` instead reads in `max_write_frames` chunks through `read`, trading one extra copy for a fixed 32 KiB stack scratch. Two shapes of the same discipline: the writer never waits.
  - *`extern struct` arrays across the ABI.* `PeakBin` is `extern` so its layout is C's; `[*]PeakBin` on the Zig side is a numpy `float32[n][ch][2]` on the Python side, mapped by `ctypes.data_as`. The `@sizeOf`/`@offsetOf` test is the layout gate.
  - *Integer bin arithmetic versus float.* numpy's `linspace` computes `i * step` in f64 and truncates; the port must do the same op in the same order (`binEdge`), because `i * n / n_bins` rounds differently and shifts a bin edge by one frame. `@intFromFloat` truncates toward zero; `(a + s - 1) / s` is the integer ceil that anchors the stride grid.
  - *`@memset` with a struct value* zeroes an `[]PeakBin`; *error set unions* (`PeakBinsError`) map one-to-one onto `FbStatus` at the ABI, nowhere else.
- **`_ToRemove/` approval (the final step of the chain):** list `_ToRemove/flashback_sampler/core/buffer.py` and `_ToRemove/tests/unit/test_peak_bins_parity.py`, plus whatever PR d/e left there (`ls -R _ToRemove`). Ask for one approval to delete the folder's contents. Do not delete before it.
- **`dev → main` promotion note:** PR f merges to `dev`; CI (`test.yml`) runs only on push to `main` (~5 billed minutes). The owner promotes `dev → main` once for the d–f batch; that run is the first time the `FLASHBACK_REQUIRE_NATIVE=1` pytest job and the `zig build test` job see this branch — watch the zig job's duration (a hang shows as cancelled, part-1 lesson).

Hand the link to the owner. Do not merge.
