# Zig Core Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A zero-dependency Zig library (`core/`) providing a lock-free seqlock ring buffer + summary ring + WAV writer behind a C ABI, proven equivalent to the Python `AudioCircularBuffer` by running the existing test suite against both, and swapped into the app behind a single factory.

**Architecture:** Single-producer seqlock ring — the writer (audio callback) never locks and publishes with one release-store of `total_written`; readers copy then re-check and retry if lapped. `total_written` is the single source of truth (write position is derived, flush is one store). Python keeps PortAudio capture and visualization readers (zero-copy view over Zig-owned memory); Zig owns memory, the write path, span reads, summary aggregation, and WAV encoding.

**Tech Stack:** Zig 0.16.x (pinned, zero external deps), ctypes (no new Python deps), existing pytest suite as parity harness, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-14-zig-core-phase1-design.md` — read it first; this plan argues from it.

## Global Constraints

- **Zero external Zig dependencies.** `build.zig.zon` must never gain a `.dependencies` entry in phase 1.
- **Zig version pinned.** Task 0 records the local `zig version`; that exact string goes in `build.zig.zon` (`minimum_zig_version`) and every CI `mlugg/setup-zig` `version:` field. Never float.
- **Pre-1.0 std drift is expected.** Code snippets in this plan target 0.16 but std APIs move; ZLS is installed in this session — if a snippet's std call doesn't resolve, fix the call site to the pinned std API and keep the design. The tests define behavior, not the snippet's exact std calls.
- **RT-safety invariant:** `Ring.write` must never lock, allocate, or fail. Any change that adds a lock/alloc/error path to it is wrong regardless of what else it fixes.
- **Idiomatic Zig, not Python-in-Zig** (owner directive): file-as-struct, caller-supplied allocators, error sets internally / status codes only at the ABI, composition over inheritance, no speculative comptime.
- **Instructional comments** (owner directive, overrides comment minimalism): where a Zig concept first load-bears (allocator parameter, `.release` publish, comptime dispatch, sentinel pointers), a short comment explains what it buys. Each PR description carries a "Zig concepts in this PR" section.
- **TDD + mutation-check:** every test must be seen red before green; compound conditions get one mutation per clause. A Zig test that is red only via compile error counts as red.
- **Shipped optimize mode is ReleaseSafe** (bounds checks on). Tests run in Debug.
- **PRs → `main`**, one per task-group as mapped below; the app must work at every merge. Deletion policy: sequester to `_ToRemove/`, never `rm -rf` (user global policy).
- Python side: no new pip dependencies; `native.py` is stdlib ctypes + numpy only.

**Task → PR map:** Task 0 = setup (no PR) · Task 1 = PR "scaffold" · Tasks 2–3 = PR "ring" · Task 4 = PR "summary" · Task 5 = PR "wav" · Tasks 6–7 = PR "abi + parity" · Task 8 = PR "swap". Branch names `feat/zig-<prname>`. Each PR closes its sub-issue (targets `main`, so `Closes #NN` fires).

---

### Task 0: Toolchain check + tracker setup

**Files:** none (gh + shell only)

**Interfaces:**
- Produces: the pinned Zig version string `<ZIGVER>` used by every later task; epic + sub-issue numbers.

- [ ] **Step 1: Verify toolchain**

Run: `zig version && zig env`
Expected: a 0.16.x version prints. Record the exact string — it is `<ZIGVER>` everywhere below. If `zig` is missing, install per https://ziglang.org/download/ and re-run.

- [ ] **Step 2: Create the epic + sub-issues**

```bash
gh issue create --title "Epic: Zig core phase 1 — lock-free memory engine + WAV writer" \
  --body "Spec: docs/superpowers/specs/2026-08-14-zig-core-phase1-design.md
Plan: docs/superpowers/plans/2026-08-14-zig-core-phase1.md

Sub-issues: scaffold+CI, seqlock ring, summary ring, WAV writer, ABI+parity harness, app swap."
```

Then one sub-issue per PR in the task→PR map (6 issues), each titled `Zig core: <prname>` with a one-line body pointing at the spec section, and each linked to the epic (`gh issue edit` with a task-list in the epic body, portfolio convention). Write-at-the-moment: update issues as tasks land, not at session end.

- [ ] **Step 3: Sanity-check repo state**

Run: `git status -sb && python -m pytest tests/unit/test_buffer.py -q`
Expected: clean tree on `main`, buffer suite green. This is the baseline the parity harness extends.

---

### Task 1: Scaffold `core/` + CI zig job

**Files:**
- Create: `core/build.zig`, `core/build.zig.zon`, `core/src/root.zig`
- Modify: `.gitignore`, `.github/workflows/test.yml`

**Interfaces:**
- Produces: `zig build` (shared lib artifact `flashback_core`), `zig build test`, module layout `src/root.zig` re-exporting later files. Later tasks add files under `core/src/` and reference them from `root.zig`.

- [ ] **Step 1: Generate current-format scaffolding**

```bash
mkdir core && cd core && zig init
```

`zig init` emits `build.zig`, `build.zig.zon`, `src/main.zig`, `src/root.zig` in the *pinned version's* current format (this dodges zon-format drift). Delete `src/main.zig` (no executable); we keep `src/root.zig`.

- [ ] **Step 2: Write `build.zig`**

Replace the generated `build.zig` with (adjust builder calls to the generated file's idiom if the pinned Zig differs — the generated file shows the current API):

```zig
const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const mod = b.addModule("flashback_core", .{
        .root_source_file = b.path("src/root.zig"),
        .target = target,
        .optimize = optimize,
    });

    // Shared library: the ctypes host loads this. `linkage = .dynamic`
    // is what makes it a .dll/.so/.dylib instead of a static archive.
    const lib = b.addLibrary(.{
        .name = "flashback_core",
        .root_module = mod,
        .linkage = .dynamic,
    });
    b.installArtifact(lib);

    const tests = b.addTest(.{ .root_module = mod });
    const run_tests = b.addRunArtifact(tests);
    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_tests.step);
}
```

Edit `build.zig.zon`: name `flashback_core`, version `0.1.0`, `.minimum_zig_version = "<ZIGVER>"`, no dependencies.

- [ ] **Step 3: Write the failing smoke test**

`core/src/root.zig`:

```zig
//! flashback_core — lock-free audio ring engine.
//! Library root: everything public is re-exported here.
const std = @import("std");

test "scaffold: the test runner runs" {
    try std.testing.expect(smoke() == 42);
}
```

Run: `zig build test` (in `core/`)
Expected: FAIL — compile error, `smoke` not defined. (Compile-error red is TDD red in Zig.)

- [ ] **Step 4: Make it pass**

Add to `root.zig`:

```zig
fn smoke() u8 {
    return 42;
}
```

Run: `zig build test` → PASS. Then `zig build` → artifact appears under `core/zig-out/` (on Windows the DLL lands in `zig-out/bin/`). Note the exact artifact path — `native.py` (Task 7) searches it.

- [ ] **Step 5: Ignore build outputs**

Append to repo-root `.gitignore`:

```
core/zig-out/
core/.zig-cache/
```

- [ ] **Step 6: Add the `zig` CI job**

In `.github/workflows/test.yml`, add alongside the `pytest` job:

```yaml
  zig:
    name: zig (${{ matrix.os }})
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest, macos-latest]
    defaults:
      run:
        working-directory: core
    steps:
      - uses: actions/checkout@v4
      - uses: mlugg/setup-zig@v2
        with:
          version: <ZIGVER>
      - name: Format check
        run: zig fmt --check build.zig src
      - name: Unit tests (Debug — full safety checks)
        run: zig build test
      - name: Build shared library (ReleaseSafe)
        run: zig build -Doptimize=ReleaseSafe
      - name: Cross-compile health check
        if: matrix.os == 'ubuntu-latest'
        run: |
          zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-windows
          zig build -Doptimize=ReleaseSafe -Dtarget=aarch64-macos
          zig build -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu
```

- [ ] **Step 7: Commit, PR, verify CI**

```bash
git checkout -b feat/zig-scaffold
git add core .gitignore .github/workflows/test.yml
git commit -m "feat(core): scaffold Zig library + CI zig job"
git push -u origin feat/zig-scaffold
gh pr create --fill --body "Closes #<scaffold-issue>. Zig concepts in this PR: build.zig graph, build.zig.zon pinning, zig test runner."
```

Expected: all six CI legs green (3 zig OSes + 3 pytest pythons). Merge; delete branch.

---

### Task 2: `Ring.zig` — init, write, read (the seqlock)

**Files:**
- Create: `core/src/Ring.zig`
- Modify: `core/src/root.zig`

**Interfaces:**
- Produces (later tasks consume exactly these):
  - `Ring.init(allocator: std.mem.Allocator, config: Ring.Config) !Ring`, `Ring.deinit(self: *Ring) void`
  - `Ring.Config = struct { sample_rate: u32, channels: u16, seconds: f64, summary_slot_frames: u32 = 4096 }`
  - `Ring.write(self: *Ring, interleaved: []const f32) void` — RT-safe
  - `Ring.read(self: *Ring, abs_start: u64, out: []f32) ReadError!void`, `ReadError = error{ Overwritten, OutOfRange }`
  - fields: `frames: []f32`, `capacity: u64` (frames), `channels: u16`, `sample_rate: u32`, `total_written: std.atomic.Value(u64)`, `gain: std.atomic.Value(f32)`

- [ ] **Step 1: Write failing init/deinit test**

Create `core/src/Ring.zig` containing ONLY the tests for this step (implementation comes after red):

```zig
//! Single-producer, many-reader lock-free ring buffer.
//!
//! One writer (the audio callback) appends interleaved f32 frames and
//! publishes progress with a single release-store of `total_written`.
//! Readers copy a span, then re-check `total_written`: if the writer
//! wrapped the whole ring through their span mid-copy, the copy may be
//! torn and they retry. `total_written` is the ONLY source of truth —
//! the write position is derived (`total_written % capacity`), which is
//! what makes flush a single atomic store (Task 4).
const std = @import("std");

const Ring = @This();

test "init allocates capacity*channels frames, starts empty" {
    var ring = try Ring.init(std.testing.allocator, .{
        .sample_rate = 48_000,
        .channels = 2,
        .seconds = 1.0,
    });
    defer ring.deinit();
    try std.testing.expectEqual(@as(u64, 48_000), ring.capacity);
    try std.testing.expectEqual(@as(usize, 96_000), ring.frames.len);
    try std.testing.expectEqual(@as(u64, 0), ring.total_written.load(.acquire));
    try std.testing.expectEqual(@as(f32, 1.0), ring.gain.load(.acquire));
}
```

Run: `zig build test` → FAIL (no fields/decls). Note: `std.testing.allocator` fails the test on any leak — allocator hygiene is tested by construction; that's why `deinit` matters even here.

- [ ] **Step 2: Implement init/deinit**

```zig
allocator: std.mem.Allocator,
frames: []f32, // capacity * channels, interleaved, one allocation, forever
capacity: u64, // in frames
channels: u16,
sample_rate: u32,
total_written: std.atomic.Value(u64),
gain: std.atomic.Value(f32),

pub const Config = struct {
    sample_rate: u32,
    channels: u16,
    seconds: f64,
    summary_slot_frames: u32 = 4096,
};

pub const ReadError = error{ Overwritten, OutOfRange };

pub fn init(allocator: std.mem.Allocator, config: Config) !Ring {
    // The allocator is a PARAMETER, not a global: the caller decides the
    // allocation strategy (testing allocator in tests, one shared
    // allocator in the ABI shim). This is the core Zig memory idiom.
    // @intFromFloat TRUNCATES — deliberately, because Python's
    // `int(duration_seconds * sample_rate)` truncates the same f64
    // product, and buffer_size must agree across implementations.
    const capacity: u64 = @intFromFloat(config.seconds * @as(f64, @floatFromInt(config.sample_rate)));
    const frames = try allocator.alloc(f32, capacity * config.channels);
    errdefer allocator.free(frames); // runs only if a later `try` fails
    @memset(frames, 0);
    return .{
        .allocator = allocator,
        .frames = frames,
        .capacity = capacity,
        .channels = config.channels,
        .sample_rate = config.sample_rate,
        .total_written = std.atomic.Value(u64).init(0),
        .gain = std.atomic.Value(f32).init(1.0),
    };
}

pub fn deinit(self: *Ring) void {
    self.allocator.free(self.frames);
    self.* = undefined; // poison: use-after-deinit becomes loud in Debug
}
```

Run: `zig build test` → PASS. Commit: `feat(core): Ring init/deinit`.

- [ ] **Step 3: Failing write/read round-trip tests**

Append tests (each is one red→green cycle; do them one at a time):

```zig
test "write then read returns the same frames" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 2, .seconds = 2.0 }); // 16-frame ring
    defer ring.deinit();
    const in = [_]f32{ 0.1, -0.1, 0.2, -0.2, 0.3, -0.3 }; // 3 stereo frames
    ring.write(&in);
    try std.testing.expectEqual(@as(u64, 3), ring.total_written.load(.acquire));
    var out: [6]f32 = undefined;
    try ring.read(0, &out);
    try std.testing.expectEqualSlices(f32, &in, &out);
}

test "write wraps around the end of the ring" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 4, .channels = 1, .seconds = 1.0 }); // 4-frame ring
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2, 3 });
    ring.write(&[_]f32{ 4, 5, 6 }); // frames 3..6, wraps: positions 3,0,1
    var out: [4]f32 = undefined;
    try ring.read(2, &out); // abs 2..6 = values 3,4,5,6
    try std.testing.expectEqualSlices(f32, &[_]f32{ 3, 4, 5, 6 }, &out);
}

test "read past total_written is OutOfRange" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2 });
    var out: [4]f32 = undefined;
    try std.testing.expectError(error.OutOfRange, ring.read(0, &out));
}

test "read of lapped span is Overwritten" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 4, .channels = 1, .seconds = 1.0 }); // 4-frame ring
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2, 3, 4, 5, 6 }); // total 6; abs 0/1 overwritten
    var out: [2]f32 = undefined;
    try std.testing.expectError(error.Overwritten, ring.read(0, &out));
    try ring.read(2, &out); // oldest valid
    try std.testing.expectEqualSlices(f32, &[_]f32{ 3, 4 }, &out);
}

test "gain scales frames at write time" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.gain.store(2.0, .monotonic);
    ring.write(&[_]f32{ 0.25, -0.25 });
    var out: [2]f32 = undefined;
    try ring.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0.5, -0.5 }, &out);
}
```

Run: `zig build test` → FAIL (no `write`/`read`).

- [ ] **Step 4: Implement write and read**

```zig
/// RT-SAFE: no locks, no allocation, no failure path. Called from the
/// audio callback thread. `interleaved.len` must be a multiple of channels.
pub fn write(self: *Ring, interleaved: []const f32) void {
    std.debug.assert(interleaved.len % self.channels == 0);
    const n: u64 = interleaved.len / self.channels;
    if (n == 0) return;
    const g = self.gain.load(.monotonic);
    // Single writer: a monotonic load of our own counter is enough.
    const tw = self.total_written.load(.monotonic);
    const total_floats: usize = @intCast(self.capacity * self.channels);
    var pos: usize = @intCast((tw % self.capacity) * self.channels);
    if (g == 1.0) {
        // Fast path: at most two straight memcpy spans across the wrap.
        var remaining = interleaved;
        while (remaining.len > 0) {
            const span = @min(remaining.len, total_floats - pos);
            @memcpy(self.frames[pos .. pos + span], remaining[0..span]);
            remaining = remaining[span..];
            pos = (pos + span) % total_floats;
        }
    } else {
        for (interleaved) |s| {
            self.frames[pos] = s * g;
            pos += 1;
            if (pos == total_floats) pos = 0;
        }
    }
    // The release-store PUBLISHES: everything written above becomes
    // visible to any reader that acquire-loads a value >= tw + n.
    // This one line is the whole synchronization protocol.
    self.total_written.store(tw + n, .release);
}

/// Seqlock read: copy, then re-check. `out.len` must be a multiple of
/// channels; the span is [abs_start, abs_start + out.len/channels).
pub fn read(self: *Ring, abs_start: u64, out: []f32) ReadError!void {
    std.debug.assert(out.len % self.channels == 0);
    const n: u64 = out.len / self.channels;
    if (n == 0) return;
    var attempt: u8 = 0;
    while (attempt < 3) : (attempt += 1) {
        const t1 = self.total_written.load(.acquire);
        if (abs_start + n > t1) return error.OutOfRange; // span not written yet
        if (t1 - abs_start > self.capacity) return error.Overwritten; // already lapped
        const total_floats: usize = @intCast(self.capacity * self.channels);
        const start_f: usize = @intCast((abs_start % self.capacity) * self.channels);
        if (start_f + out.len <= total_floats) {
            @memcpy(out, self.frames[start_f .. start_f + out.len]);
        } else {
            const first = total_floats - start_f;
            @memcpy(out[0..first], self.frames[start_f..]);
            @memcpy(out[first..], self.frames[0 .. out.len - first]);
        }
        // Seqlock verify: if the writer wrapped the whole ring through our
        // span while we copied, the copy may mix generations — retry.
        // (Formal-memory-model footnote: a canonical seqlock wants a
        // fence before this load; Zig removed @fence, so we lean on the
        // acquire load + the stress test below. If the pinned std has a
        // fence/compiler-barrier API, use it here.)
        const t2 = self.total_written.load(.acquire);
        if (t2 - abs_start <= self.capacity) return;
    }
    return error.Overwritten;
}
```

Run: `zig build test` → PASS.

- [ ] **Step 5: Mutation-check the seqlock clauses**

The read validity condition is compound; one mutation per clause:
1. Change `abs_start + n > t1` to `abs_start + n > t1 + 1` → the OutOfRange test must go red. Revert.
2. Change `t1 - abs_start > self.capacity` to `>=` and re-run → the "oldest valid" read in the lapped test must go red. Revert.
3. Delete the `t2` re-check (return unconditionally after copy) → the stress test (next step) must go red. Do this mutation after Step 6 passes.

- [ ] **Step 6: The stress test (torn-read detector) — the PR's core deliverable**

Append:

```zig
test "seqlock stress: concurrent writer never yields torn reads" {
    // Tiny ring (1024 frames) so the writer laps constantly — the worst
    // case for readers. Every sample's value is a pure function of its
    // absolute index, so a reader can VERIFY every byte it gets back:
    // any mix of generations in one read is caught exactly.
    const cap_frames = 1024;
    const chans = 2;
    var ring = try Ring.init(std.testing.allocator, .{
        .sample_rate = 48_000,
        .channels = chans,
        .seconds = @as(f64, cap_frames) / 48_000.0,
    });
    defer ring.deinit();

    const H = struct {
        // f32 holds integers exactly up to 2^24 — keep values inside that.
        fn expected(abs_frame: u64, ch: u64) f32 {
            return @floatFromInt((abs_frame * chans + ch) % (1 << 24));
        }
        fn writerLoop(r: *Ring, stop: *std.atomic.Value(bool)) void {
            var abs: u64 = 0;
            var block: [128 * chans]f32 = undefined;
            while (!stop.load(.monotonic)) {
                for (0..128) |i| {
                    for (0..chans) |c| {
                        block[i * chans + c] = expected(abs + i, c);
                    }
                }
                r.write(&block);
                abs += 128;
            }
        }
    };

    var stop = std.atomic.Value(bool).init(false);
    const writer = try std.Thread.spawn(.{}, H.writerLoop, .{ &ring, &stop });
    defer writer.join();
    defer stop.store(true, .monotonic);

    var successes: u64 = 0;
    var out: [64 * chans]f32 = undefined;
    while (successes < 10_000) {
        const tw = ring.total_written.load(.acquire);
        if (tw < 512) continue;
        const abs_start = tw - 256; // mid-ring: sometimes safe, sometimes lapped
        ring.read(abs_start, &out) catch continue; // Overwritten is FINE; torn is not
        for (0..64) |i| {
            for (0..chans) |c| {
                const want = H.expected(abs_start + i, c);
                if (out[i * chans + c] != want) {
                    std.debug.print("TORN at abs {d} ch {d}: got {d}, want {d}\n", .{ abs_start + i, c, out[i * chans + c], want });
                    return error.TornRead;
                }
            }
        }
        successes += 1;
    }
}
```

Run: `zig build test` → PASS (give it a few seconds). Now run Step 5's mutation 3 (delete the `t2` re-check): this test must fail with `TornRead` within a few runs — if it doesn't go red, the stress test isn't detecting tears and must be fixed before proceeding (smaller ring, bigger reads). Revert the mutation.

- [ ] **Step 7: Export from root, format, commit**

In `root.zig` add `pub const Ring = @import("Ring.zig");` and delete the Task-1 smoke test + `smoke()` (dead scaffolding — deleting it now IS the deletion policy's plain-file exemption: it's ours, this session, in-repo via git). Run `zig fmt build.zig src && zig build test`.

```bash
git checkout -b feat/zig-ring
git add core
git commit -m "feat(core): seqlock ring — lock-free writer, retrying readers, stress-tested"
```

---

### Task 3: `Ring.flush` — one store, no zeroing needed (plus hygiene)

**Files:**
- Modify: `core/src/Ring.zig`

**Interfaces:**
- Produces: `Ring.flush(self: *Ring) void`. Task 4 extends it to poison summary slots.

- [ ] **Step 1: Failing tests**

```zig
test "flush empties the ring; writer restarts cleanly at abs 0" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2, 3, 4, 5 });
    ring.flush();
    try std.testing.expectEqual(@as(u64, 0), ring.total_written.load(.acquire));
    var out: [1]f32 = undefined;
    try std.testing.expectError(error.OutOfRange, ring.read(0, &out)); // nothing readable
    ring.write(&[_]f32{ 9, 8 });
    try ring.read(0, &out);
    try std.testing.expectEqual(@as(f32, 9), out[0]);
}
```

Run: `zig build test` → FAIL.

- [ ] **Step 2: Implement**

```zig
/// Discard all buffered audio. Because `total_written` is the single
/// source of truth and readers never address at-or-beyond it, resetting
/// it to zero makes every stale byte unreachable — no zeroing REQUIRED
/// for correctness. We zero anyway (hygiene: `.buffer` is exposed as a
/// zero-copy view to the Python host). Called from a control thread,
/// never the audio thread. Racing an active writer costs at most one
/// audio block rendered as silence — silence is a valid sample, never
/// torn garbage. Documented and accepted in the spec.
pub fn flush(self: *Ring) void {
    self.total_written.store(0, .release);
    @memset(self.frames, 0);
}
```

Run: `zig build test` → PASS. Mutation-check: swap the two lines' order and confirm... it still passes single-threaded (both orders work quiescently) — the ORDER is for the concurrent case (make bytes unreachable *before* scribbling zeros over them); pin it with the comment and move on. Commit: `feat(core): flush — single-store reset`.

- [ ] **Step 3: PR for Tasks 2+3**

```bash
git push -u origin feat/zig-ring
gh pr create --fill --body "Closes #<ring-issue>. Zig concepts in this PR: std.atomic.Value + acquire/release, file-as-struct, errdefer, std.Thread, testing allocator leak detection, @memcpy spans."
```

CI green → merge.

---

### Task 4: `Summary.zig` — pre-decimated stats ring

**Files:**
- Create: `core/src/Summary.zig`
- Modify: `core/src/Ring.zig`, `core/src/root.zig`

**Interfaces:**
- Consumes: `Ring` fields from Task 2.
- Produces:
  - `Summary.init(allocator, capacity_frames: u64, slot_frames: u32, channels: u16) !Summary`, `Summary.deinit(self: *Summary) void`
  - `Summary.update(self: *Summary, interleaved: []const f32, gain: f32, start_abs: u64) void`
  - `Summary.poison(self: *Summary) void` — all `slot_abs = -1`
  - `Summary.rmsBins(self: *const Summary, total_written: u64, n_samples_req: u64, bin_span_frames: u64, out: []f32) void` — out is `n_bins * channels`, n_bins = out.len/channels
  - `Ring.write` calls `summary.update`; `Ring.flush` calls `summary.poison`; field `Ring.summary: Summary`
  - This is the exact port of `AudioCircularBuffer._update_summary_locked` + `get_summary_bins` aggregation in `flashback_sampler/core/buffer.py:113-143,421-492` — read both before implementing; semantics must match (parity tests will compare numerically).

- [ ] **Step 1: Failing slot-accumulation tests**

Create `core/src/Summary.zig` with header + tests:

```zig
//! Pre-decimated summary ring: slots of `slot_frames` frames, each
//! storing (min, max, sum-of-squares, count) per channel, keyed by the
//! absolute index of the slot's first frame (its GENERATION tag — the
//! same trick the seqlock uses, applied per-slot). A slot whose tag
//! doesn't match the incoming span's generation is overwritten, not
//! accumulated. Poisoning every tag to -1 is how flush invalidates the
//! whole summary in O(n_slots) without touching audio data.
const std = @import("std");

const Summary = @This();

test "one slot accumulates min/max/ss/count across writes" {
    var s = try Summary.init(std.testing.allocator, 8, 4, 1); // 8-frame ring, 4-frame slots, mono
    defer s.deinit();
    s.update(&[_]f32{ 0.5, -0.5 }, 1.0, 0); // abs 0..2 → slot 0
    s.update(&[_]f32{ 1.0, -0.25 }, 1.0, 2); // abs 2..4 → still slot 0
    try std.testing.expectEqual(@as(f32, -0.5), s.min[0]);
    try std.testing.expectEqual(@as(f32, 1.0), s.max[0]);
    try std.testing.expectApproxEqAbs(@as(f64, 0.25 + 0.25 + 1.0 + 0.0625), s.ss[0], 1e-12); // 0.5², 0.5², 1², 0.25²
    try std.testing.expectEqual(@as(u64, 4), s.count[0]);
    try std.testing.expectEqual(@as(i64, 0), s.slot_abs[0]);
}

test "a new generation overwrites a recycled slot" {
    var s = try Summary.init(std.testing.allocator, 8, 4, 1); // 2 slots
    defer s.deinit();
    s.update(&[_]f32{ 1, 1, 1, 1 }, 1.0, 0); // slot 0, gen abs 0
    s.update(&[_]f32{ 2, 2, 2, 2 }, 1.0, 4); // slot 1
    s.update(&[_]f32{ 3, 3 }, 1.0, 8); // slot 0 again, NEW gen abs 8 → overwrite
    try std.testing.expectEqual(@as(f32, 3), s.min[0]);
    try std.testing.expectEqual(@as(u64, 2), s.count[0]);
    try std.testing.expectEqual(@as(i64, 8), s.slot_abs[0]);
}
```

Run → FAIL.

- [ ] **Step 2: Implement init/deinit/update/poison**

```zig
allocator: std.mem.Allocator,
slot_frames: u32,
n_slots: u64,
capacity_frames: u64, // the RING's true capacity — NOT n_slots*slot_frames,
// which under-counts when capacity isn't slot-aligned (48000/4096 isn't);
// window clamping in rmsBins must match Python's buffer_size clamp exactly
channels: u16,
min: []f32, // n_slots * channels
max: []f32,
ss: []f64, // sum of squares — f64 like the Python original, so long
// accumulations don't lose precision in f32
count: []u64, // per slot (frames counted)
slot_abs: []i64, // generation tag; -1 = never written / poisoned

pub fn init(allocator: std.mem.Allocator, capacity_frames: u64, slot_frames: u32, channels: u16) !Summary {
    const n_slots = @max(1, capacity_frames / slot_frames);
    const min = try allocator.alloc(f32, n_slots * channels);
    errdefer allocator.free(min);
    const max = try allocator.alloc(f32, n_slots * channels);
    errdefer allocator.free(max);
    const ss = try allocator.alloc(f64, n_slots * channels);
    errdefer allocator.free(ss);
    const count = try allocator.alloc(u64, n_slots);
    errdefer allocator.free(count);
    const slot_abs = try allocator.alloc(i64, n_slots);
    errdefer allocator.free(slot_abs);
    var s = Summary{
        .allocator = allocator,
        .slot_frames = slot_frames,
        .n_slots = n_slots,
        .capacity_frames = capacity_frames,
        .channels = channels,
        .min = min,
        .max = max,
        .ss = ss,
        .count = count,
        .slot_abs = slot_abs,
    };
    s.poison();
    @memset(s.min, 0);
    @memset(s.max, 0);
    @memset(s.ss, 0);
    @memset(s.count, 0);
    return s;
}

pub fn deinit(self: *Summary) void {
    self.allocator.free(self.min);
    self.allocator.free(self.max);
    self.allocator.free(self.ss);
    self.allocator.free(self.count);
    self.allocator.free(self.slot_abs);
    self.* = undefined;
}

pub fn poison(self: *Summary) void {
    @memset(self.slot_abs, -1);
}

/// Mirror of buffer.py _update_summary_locked. `interleaved` is the
/// PRE-gain input; gain is re-applied here (a block is ~1k frames — the
/// extra multiply is nothing, and it keeps write()'s fast path free of
/// a second pass). Runs on the audio thread: no locks, no allocation.
pub fn update(self: *Summary, interleaved: []const f32, gain: f32, start_abs: u64) void {
    const chans = self.channels;
    const n: u64 = interleaved.len / chans;
    if (n == 0) return;
    const slot_first = start_abs / self.slot_frames;
    const slot_last = (start_abs + n - 1) / self.slot_frames;
    var s_global = slot_first;
    while (s_global <= slot_last) : (s_global += 1) {
        const slot_idx: usize = @intCast(s_global % self.n_slots);
        const slot_start_abs: i64 = @intCast(s_global * self.slot_frames);
        const f_from: u64 = if (s_global * self.slot_frames > start_abs) s_global * self.slot_frames - start_abs else 0;
        const f_to: u64 = @min(n, s_global * self.slot_frames + self.slot_frames - start_abs);
        const fresh = self.slot_abs[slot_idx] != slot_start_abs;
        if (fresh) {
            self.slot_abs[slot_idx] = slot_start_abs;
            self.count[slot_idx] = 0;
            for (0..chans) |c| {
                self.min[slot_idx * chans + c] = std.math.floatMax(f32);
                self.max[slot_idx * chans + c] = -std.math.floatMax(f32);
                self.ss[slot_idx * chans + c] = 0;
            }
        }
        var f = f_from;
        while (f < f_to) : (f += 1) {
            for (0..chans) |c| {
                const v = interleaved[@intCast(f * chans + c)] * gain;
                const i = slot_idx * chans + c;
                self.min[i] = @min(self.min[i], v);
                self.max[i] = @max(self.max[i], v);
                self.ss[i] += @as(f64, v) * @as(f64, v);
            }
        }
        self.count[slot_idx] += f_to - f_from;
    }
}
```

Run → PASS. Commit: `feat(core): Summary slots — generational accumulate`.

- [ ] **Step 3: Failing rmsBins test (numeric port of get_summary_bins)**

```zig
test "rmsBins aggregates frozen slots into display bins" {
    var s = try Summary.init(std.testing.allocator, 16, 4, 1); // 4 slots
    defer s.deinit();
    // Two full slots: constant 0.5 (ss=1.0/slot) then constant 1.0 (ss=4.0/slot)
    s.update(&[_]f32{ 0.5, 0.5, 0.5, 0.5 }, 1.0, 0);
    s.update(&[_]f32{ 1, 1, 1, 1 }, 1.0, 4);
    var out: [2]f32 = undefined; // 2 bins, mono
    s.rmsBins(8, 8, 0, &out); // tw=8, all 8 samples, auto bin span
    try std.testing.expectApproxEqAbs(@as(f32, 0.5), out[0], 1e-6);
    try std.testing.expectApproxEqAbs(@as(f32, 1.0), out[1], 1e-6);
}
```

Run → FAIL.

- [ ] **Step 4: Implement rmsBins**

Port of `get_summary_bins` (buffer.py:421-492) aggregation — same clamping, same bin assignment:

```zig
/// out.len = n_bins * channels. n_samples_req = 0 → all available.
/// bin_span_frames = 0 → derived from window (n_samples / n_bins).
/// Slots whose generation tag falls inside [abs_start, abs_start+n)
/// scatter-add ss and count into their bin; out = sqrt(ss/count).
pub fn rmsBins(self: *const Summary, total_written: u64, n_samples_req: u64, bin_span_frames: u64, out: []f32) void {
    const chans = self.channels;
    const n_bins = out.len / chans;
    @memset(out, 0);
    if (n_bins == 0) return;
    const n_avail = @min(total_written, self.capacity_frames);
    const n_samples = if (n_samples_req == 0) n_avail else @min(n_samples_req, n_avail);
    if (n_samples == 0) return;
    const abs_start = total_written - n_samples;
    const bin_span: f64 = if (bin_span_frames > 0)
        @floatFromInt(bin_span_frames)
    else
        @as(f64, @floatFromInt(n_samples)) / @as(f64, @floatFromInt(n_bins));

    // Small fixed accumulators would need allocation for arbitrary n_bins;
    // instead accumulate straight into out's shape using two stack-free
    // passes over slots (n_slots is small: capacity/4096).
    // Pass 1 must accumulate ss (f64) and count per bin — allocation-free
    // by bounding n_bins: callers ask for display bins (≤ ~4096). Assert it.
    std.debug.assert(n_bins <= 4096);
    var bin_ss: [4096 * 2]f64 = undefined; // max bins * max channels
    var bin_cnt: [4096]u64 = undefined;
    std.debug.assert(chans <= 2);
    @memset(bin_ss[0 .. n_bins * chans], 0);
    @memset(bin_cnt[0..n_bins], 0);

    for (0..@intCast(self.n_slots)) |slot_idx| {
        const tag = self.slot_abs[slot_idx];
        if (tag < 0) continue;
        const tag_u: u64 = @intCast(tag);
        if (tag_u < abs_start or tag_u >= abs_start + n_samples) continue;
        var bin: usize = @intFromFloat(@as(f64, @floatFromInt(tag_u - abs_start)) / bin_span);
        if (bin >= n_bins) bin = n_bins - 1;
        for (0..chans) |c| {
            bin_ss[bin * chans + c] += self.ss[slot_idx * chans + c];
        }
        bin_cnt[bin] += self.count[slot_idx];
    }
    for (0..n_bins) |b| {
        if (bin_cnt[b] == 0) continue;
        for (0..chans) |c| {
            out[b * chans + c] = @floatCast(@sqrt(bin_ss[b * chans + c] / @as(f64, @floatFromInt(bin_cnt[b]))));
        }
    }
}
```

(The 4096-bin / 2-channel bound is a real interface contract, asserted loudly, not a silent cap — the ABI layer in Task 6 turns it into `INVALID_ARG`.)

Run → PASS.

- [ ] **Step 5: Wire into Ring**

Failing test first (append to `Ring.zig`):

```zig
test "write feeds the summary; flush poisons it" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 16, .channels = 1, .seconds = 1.0, .summary_slot_frames = 4 });
    defer ring.deinit();
    ring.write(&[_]f32{ 0.5, 0.5, 0.5, 0.5 });
    var out: [1]f32 = undefined;
    ring.summary.rmsBins(ring.total_written.load(.acquire), 0, 0, &out);
    try std.testing.expectApproxEqAbs(@as(f32, 0.5), out[0], 1e-6);
    ring.flush();
    ring.summary.rmsBins(0, 0, 0, &out);
    try std.testing.expectEqual(@as(f32, 0), out[0]);
}
```

Red, then: add `summary: Summary` field to Ring; `init` constructs it (`Summary.init(allocator, capacity, config.summary_slot_frames, config.channels)` with `errdefer`), `deinit` frees it, `write` calls `self.summary.update(interleaved, g, tw)` before the release-store, `flush` calls `self.summary.poison()` before the total_written store. Green. Note in a comment on `flush`: poisoning BEFORE the store means a racing writer that wins a slot tag write leaves one transiently-mixed slot (~85 ms, self-heals) — spec-documented.

- [ ] **Step 6: Export, format, PR**

`root.zig`: `pub const Summary = @import("Summary.zig");`. `zig fmt`, full `zig build test`.

```bash
git checkout -b feat/zig-summary && git add core
git commit -m "feat(core): summary ring — generational slots + rms bins, wired into write/flush"
git push -u origin feat/zig-summary
gh pr create --fill --body "Closes #<summary-issue>. Zig concepts in this PR: multi-errdefer init, f64 accumulation, debug asserts as interface contracts, composition (Ring has-a Summary)."
```

---

### Task 5: `wav.zig` — zero-dep WAV encoder

**Files:**
- Create: `core/src/wav.zig`
- Modify: `core/src/root.zig`

**Interfaces:**
- Produces:
  - `wav.Subtype = enum(u8) { float32 = 0, pcm_24 = 1, pcm_16 = 2 }`
  - `wav.header_len = 44`, `wav.bytesPerSample(st: Subtype) u8`
  - `wav.writeHeader(out: *[44]u8, rate: u32, channels: u16, st: Subtype, n_frames: u64) void`
  - `wav.encodeSamples(st: Subtype, samples: []const f32, out: []u8) usize` (returns bytes written)
  - `wav.writeFile(path: []const u8, samples: []const f32, rate: u32, channels: u16, st: Subtype) !void`
- Quantization contract (parity tests in Task 7 depend on these exact formulas): `pcm_16: clamp(round(x*32767), -32768, 32767)`; `pcm_24: clamp(round(x*8388607), -8388608, 8388607)`, both little-endian. FLOAT32 is a raw memcpy of the f32 bits (little-endian hosts only — comptime-assert it).

- [ ] **Step 1: Failing golden-header test**

Create `core/src/wav.zig`:

```zig
//! Minimal RIFF/WAVE writer. FLOAT32 payload is the ring's bytes
//! verbatim — a bit-perfect pull. 44-byte canonical header; libsndfile
//! and every DAW read it. Parity vs soundfile is DECODE-equality
//! (samples + format), not byte-equality (libsndfile adds PEAK/fact
//! chunks we deliberately don't).
const std = @import("std");
const builtin = @import("builtin");

comptime {
    // FLOAT32's memcpy path writes host-endian bits as file bytes.
    // Every supported target is little-endian; make that loud, not lucky.
    std.debug.assert(builtin.target.cpu.arch.endian() == .little);
}

test "golden 44-byte header: 48k stereo float32, 4 frames" {
    var h: [44]u8 = undefined;
    writeHeader(&h, 48_000, 2, .float32, 4);
    try std.testing.expectEqualSlices(u8, "RIFF", h[0..4]);
    try std.testing.expectEqual(@as(u32, 68), std.mem.readInt(u32, h[4..8], .little)); // 36 + data(32)
    try std.testing.expectEqualSlices(u8, "WAVE", h[8..12]);
    try std.testing.expectEqualSlices(u8, "fmt ", h[12..16]);
    try std.testing.expectEqual(@as(u32, 16), std.mem.readInt(u32, h[16..20], .little));
    try std.testing.expectEqual(@as(u16, 3), std.mem.readInt(u16, h[20..22], .little)); // IEEE float
    try std.testing.expectEqual(@as(u16, 2), std.mem.readInt(u16, h[22..24], .little));
    try std.testing.expectEqual(@as(u32, 48_000), std.mem.readInt(u32, h[24..28], .little));
    try std.testing.expectEqual(@as(u32, 384_000), std.mem.readInt(u32, h[28..32], .little)); // byte rate
    try std.testing.expectEqual(@as(u16, 8), std.mem.readInt(u16, h[32..34], .little)); // block align
    try std.testing.expectEqual(@as(u16, 32), std.mem.readInt(u16, h[34..36], .little)); // bits
    try std.testing.expectEqualSlices(u8, "data", h[36..40]);
    try std.testing.expectEqual(@as(u32, 32), std.mem.readInt(u32, h[40..44], .little));
}
```

Run → FAIL.

- [ ] **Step 2: Implement header + subtype table**

```zig
pub const Subtype = enum(u8) {
    float32 = 0,
    pcm_24 = 1,
    pcm_16 = 2,

    // comptime-checked exhaustive dispatch: adding a subtype without
    // updating these tables is a compile error, not a runtime surprise.
    pub fn bytesPerSample(self: Subtype) u8 {
        return switch (self) {
            .float32 => 4,
            .pcm_24 => 3,
            .pcm_16 => 2,
        };
    }
    fn formatTag(self: Subtype) u16 {
        return switch (self) {
            .float32 => 3, // WAVE_FORMAT_IEEE_FLOAT
            .pcm_24, .pcm_16 => 1, // WAVE_FORMAT_PCM
        };
    }
};

pub const header_len = 44;

pub fn writeHeader(out: *[header_len]u8, rate: u32, channels: u16, st: Subtype, n_frames: u64) void {
    const bps: u32 = st.bytesPerSample();
    const block_align: u16 = @intCast(bps * channels);
    const data_len: u32 = @intCast(n_frames * block_align);
    @memcpy(out[0..4], "RIFF");
    std.mem.writeInt(u32, out[4..8], 36 + data_len, .little);
    @memcpy(out[8..12], "WAVE");
    @memcpy(out[12..16], "fmt ");
    std.mem.writeInt(u32, out[16..20], 16, .little);
    std.mem.writeInt(u16, out[20..22], st.formatTag(), .little);
    std.mem.writeInt(u16, out[22..24], channels, .little);
    std.mem.writeInt(u32, out[24..28], rate, .little);
    std.mem.writeInt(u32, out[28..32], rate * block_align, .little);
    std.mem.writeInt(u16, out[32..34], block_align, .little);
    std.mem.writeInt(u16, out[34..36], @as(u16, bps) * 8, .little);
    @memcpy(out[36..40], "data");
    std.mem.writeInt(u32, out[40..44], data_len, .little);
}
```

Run → PASS. Commit.

- [ ] **Step 3: Failing encode tests (the quantization contract)**

```zig
test "float32 encode is the raw bits" {
    const in = [_]f32{ 0.5, -1.0 };
    var out: [8]u8 = undefined;
    try std.testing.expectEqual(@as(usize, 8), encodeSamples(.float32, &in, &out));
    try std.testing.expectEqualSlices(u8, std.mem.sliceAsBytes(&in), &out);
}

test "pcm16 quantization: round-half-away, clamped" {
    const in = [_]f32{ 0.0, 1.0, -1.0, 0.5, 1.5 }; // 1.5 must clamp
    var out: [10]u8 = undefined;
    _ = encodeSamples(.pcm_16, &in, &out);
    try std.testing.expectEqual(@as(i16, 0), std.mem.readInt(i16, out[0..2], .little));
    try std.testing.expectEqual(@as(i16, 32767), std.mem.readInt(i16, out[2..4], .little));
    try std.testing.expectEqual(@as(i16, -32767), std.mem.readInt(i16, out[4..6], .little));
    try std.testing.expectEqual(@as(i16, 16384), std.mem.readInt(i16, out[6..8], .little)); // round(0.5*32767)=16384 (16383.5 → away from zero)
    try std.testing.expectEqual(@as(i16, 32767), std.mem.readInt(i16, out[8..10], .little));
}

test "pcm24 writes 3 little-endian bytes per sample" {
    const in = [_]f32{ 1.0, -1.0 };
    var out: [6]u8 = undefined;
    _ = encodeSamples(.pcm_24, &in, &out);
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0xFF, 0xFF, 0x7F }, out[0..3]); // 8388607
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0x01, 0x00, 0x80 }, out[3..6]); // -8388607
}
```

Run → FAIL.

- [ ] **Step 4: Implement encodeSamples**

```zig
/// Encode f32 samples into `out` per the subtype's quantization
/// contract (see plan/spec). Returns bytes written. `out` must hold
/// samples.len * bytesPerSample.
pub fn encodeSamples(st: Subtype, samples: []const f32, out: []u8) usize {
    switch (st) {
        .float32 => {
            const bytes = std.mem.sliceAsBytes(samples);
            @memcpy(out[0..bytes.len], bytes);
            return bytes.len;
        },
        .pcm_16 => {
            for (samples, 0..) |s, i| {
                const clamped = std.math.clamp(s, -1.0, 1.0);
                const v: i16 = @intFromFloat(std.math.clamp(@round(clamped * 32767.0), -32768.0, 32767.0));
                std.mem.writeInt(i16, out[i * 2 ..][0..2], v, .little);
            }
            return samples.len * 2;
        },
        .pcm_24 => {
            for (samples, 0..) |s, i| {
                const clamped = std.math.clamp(s, -1.0, 1.0);
                const v: i32 = @intFromFloat(std.math.clamp(@round(clamped * 8388607.0), -8388608.0, 8388607.0));
                out[i * 3] = @truncate(@as(u32, @bitCast(v)));
                out[i * 3 + 1] = @truncate(@as(u32, @bitCast(v)) >> 8);
                out[i * 3 + 2] = @truncate(@as(u32, @bitCast(v)) >> 16);
            }
            return samples.len * 3;
        },
    }
}
```

Run → PASS.

- [ ] **Step 5: writeFile (chunked, fixed stack buffer) + round-trip test**

Failing test:

```zig
test "writeFile round-trips float32 through a real file" {
    const path = "zig-cache-test-roundtrip.wav";
    defer std.fs.cwd().deleteFile(path) catch {};
    const in = [_]f32{ 0.1, -0.2, 0.3, -0.4 }; // 2 stereo frames
    try writeFile(path, &in, 48_000, 2, .float32);
    var buf: [44 + 16]u8 = undefined;
    const got = try std.fs.cwd().readFile(path, &buf);
    try std.testing.expectEqual(@as(usize, 60), got.len);
    try std.testing.expectEqualSlices(u8, std.mem.sliceAsBytes(&in), got[44..]);
}
```

Then implement:

```zig
/// Stream samples to `path` through a fixed 64 KiB stack buffer — no
/// allocation regardless of clip length (a 15-minute grab never doubles
/// memory). Chunk boundary is sample-aligned for every subtype
/// (16384 samples * 4 bytes max).
pub fn writeFile(path: []const u8, samples: []const f32, rate: u32, channels: u16, st: Subtype) !void {
    var file = try std.fs.cwd().createFile(path, .{});
    defer file.close();
    var header: [header_len]u8 = undefined;
    writeHeader(&header, rate, channels, st, samples.len / channels);
    try file.writeAll(&header);
    var buf: [16384 * 4]u8 = undefined;
    var remaining = samples;
    while (remaining.len > 0) {
        const take = @min(remaining.len, 16384);
        const n = encodeSamples(st, remaining[0..take], &buf);
        try file.writeAll(buf[0..n]);
        remaining = remaining[take..];
    }
}
```

(If the pinned std's file API differs — 0.15+ reworked `std.Io` — ZLS will show the current `createFile`/`writeAll` shapes; keep the chunked-fixed-buffer design.)

Run → PASS.

- [ ] **Step 6: Export, format, PR**

`root.zig`: `pub const wav = @import("wav.zig");`

```bash
git checkout -b feat/zig-wav && git add core
git commit -m "feat(core): zero-dep WAV writer — float32 bit-perfect, pcm16/24 quantized"
git push -u origin feat/zig-wav
gh pr create --fill --body "Closes #<wav-issue>. Zig concepts in this PR: comptime endianness assert, exhaustive switch dispatch on enums, std.mem.writeInt, fixed stack buffers, defer for file close."
```

---

### Task 6: `abi.zig` + C header — the export surface

**Files:**
- Create: `core/src/abi.zig`, `core/include/flashback_core.h`
- Modify: `core/src/root.zig`

**Interfaces:**
- Consumes: `Ring`, `Summary`, `wav` exactly as produced above.
- Produces: the C ABI from the spec (refined form) — `fb_ring_create/destroy/write/read/flush/total_written/capacity/storage/set_gain/gain/summary_bins` + `fb_wav_write`. Status codes: `0 ok, 1 overwritten, 2 out_of_range, 3 io_error, 4 invalid_arg`. Subtype ints match `wav.Subtype` (0 float32, 1 pcm_24, 2 pcm_16). Task 7's ctypes bindings mirror these EXACTLY.

- [ ] **Step 1: Failing ABI test (call the exports from Zig)**

Create `core/src/abi.zig`:

```zig
//! C ABI shim. Thin and boring by design: translate error unions to
//! status codes, pointers+lengths to slices, and NOTHING else. All
//! logic lives in Ring/Summary/wav — this file must stay liftable-host
//! plumbing. `export fn` gives the symbol an unmangled C name in the
//! shared library.
const std = @import("std");
const Ring = @import("Ring.zig");
const wav = @import("wav.zig");

// One allocator instance for every ABI-created object. smp_allocator is
// std's thread-safe general-purpose choice; if the pinned std lacks it,
// use std.heap.c_allocator (links libc, also fine for a shared lib).
const allocator = std.heap.smp_allocator;

pub const FbStatus = enum(c_int) {
    ok = 0,
    overwritten = 1,
    out_of_range = 2,
    io_error = 3,
    invalid_arg = 4,
};

test "abi round-trip: create, write, read, destroy" {
    const ring = fb_ring_create(48_000, 2, 1.0) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    const in = [_]f32{ 0.1, -0.1, 0.2, -0.2 };
    fb_ring_write(ring, &in, 2);
    try std.testing.expectEqual(@as(u64, 2), fb_ring_total_written(ring));
    // out must hold n_frames * channels floats for the LARGEST call below
    // (3 frames * 2 ch = 6) — the ABI slices a many-item pointer and
    // cannot bounds-check for us; the caller owns the length contract.
    var out: [6]f32 = undefined;
    try std.testing.expectEqual(FbStatus.ok, fb_ring_read(ring, 0, 2, &out));
    try std.testing.expectEqualSlices(f32, &in, out[0..4]);
    try std.testing.expectEqual(FbStatus.out_of_range, fb_ring_read(ring, 0, 3, &out));
}
```

Run → FAIL (exports missing).

- [ ] **Step 2: Implement the exports**

```zig
export fn fb_ring_create(rate: u32, channels: u16, seconds: f64) ?*Ring {
    if (rate == 0 or channels == 0 or channels > 2 or seconds <= 0) return null;
    const ring = allocator.create(Ring) catch return null;
    ring.* = Ring.init(allocator, .{
        .sample_rate = rate,
        .channels = channels,
        .seconds = seconds,
    }) catch {
        allocator.destroy(ring);
        return null;
    };
    return ring;
}

export fn fb_ring_destroy(ring: *Ring) void {
    ring.deinit();
    allocator.destroy(ring);
}

export fn fb_ring_write(ring: *Ring, frames: [*]const f32, n_frames: usize) void {
    ring.write(frames[0 .. n_frames * ring.channels]);
}

export fn fb_ring_total_written(ring: *const Ring) u64 {
    return ring.total_written.load(.acquire);
}

export fn fb_ring_capacity(ring: *const Ring) u64 {
    return ring.capacity;
}

export fn fb_ring_storage(ring: *const Ring) [*]const f32 {
    // Zero-copy view for the Python host's visualization readers. The
    // pointer is valid until fb_ring_destroy; the host does its own
    // seqlock verify against fb_ring_total_written (see native.py).
    return ring.frames.ptr;
}

export fn fb_ring_set_gain(ring: *Ring, gain: f32) void {
    ring.gain.store(gain, .monotonic);
}

export fn fb_ring_gain(ring: *const Ring) f32 {
    return ring.gain.load(.monotonic);
}

export fn fb_ring_flush(ring: *Ring) void {
    ring.flush();
}

export fn fb_ring_read(ring: *Ring, abs_start: u64, n_frames: usize, out: [*]f32) FbStatus {
    ring.read(abs_start, out[0 .. n_frames * ring.channels]) catch |err| return switch (err) {
        error.Overwritten => .overwritten,
        error.OutOfRange => .out_of_range,
    };
    return .ok;
}

export fn fb_ring_summary_bins(ring: *Ring, n_bins: usize, n_samples: u64, bin_span_frames: u64, out_rms: [*]f32) FbStatus {
    if (n_bins == 0 or n_bins > 4096) return .invalid_arg; // Summary's asserted bound
    ring.summary.rmsBins(ring.total_written.load(.acquire), n_samples, bin_span_frames, out_rms[0 .. n_bins * ring.channels]);
    return .ok;
}

export fn fb_wav_write(path: [*:0]const u8, frames: [*]const f32, n_frames: usize, rate: u32, channels: u16, subtype: c_int) FbStatus {
    if (rate == 0 or channels == 0) return .invalid_arg;
    if (subtype < 0 or subtype > 2) return .invalid_arg;
    const st: wav.Subtype = @enumFromInt(@as(u8, @intCast(subtype)));
    // [*:0] is a SENTINEL pointer: length is found by scanning for the
    // 0 terminator — exactly C's char*. std.mem.span turns it into a slice.
    wav.writeFile(std.mem.span(path), frames[0 .. n_frames * channels], rate, channels, st) catch return .io_error;
    return .ok;
}
```

Run → PASS. Mutation-check `fb_ring_summary_bins`'s guard: one mutation per clause (`n_bins == 0` and `n_bins > 4096` each removed → a test must go red — add the two tiny red tests if not yet covered).

- [ ] **Step 3: Ensure exports reach the DLL**

`export fn` symbols are only emitted if the file is reachable from the root module. In `root.zig` add:

```zig
pub const abi = @import("abi.zig");

comptime {
    // Force-reference the ABI so its `export fn`s are emitted into the
    // shared library even though nothing in Zig calls them.
    _ = abi;
}
```

Run: `zig build -Doptimize=ReleaseSafe`, then verify symbols:
- Windows: `python -c "import ctypes; d=ctypes.CDLL('zig-out/bin/flashback_core.dll'); print(d.fb_ring_create)"`
Expected: a function handle prints, no AttributeError.

- [ ] **Step 4: Write `core/include/flashback_core.h`**

```c
/* flashback_core — C ABI for the Zig audio ring engine.
 * Mirrors core/src/abi.zig; keep in lockstep. ctypes does not read this
 * file — it exists for future non-Python hosts (CLAP plugin, mobile). */
#ifndef FLASHBACK_CORE_H
#define FLASHBACK_CORE_H
#include <stddef.h>
#include <stdint.h>

typedef struct FbRing FbRing; /* opaque */

typedef enum FbStatus {
  FB_OK = 0,
  FB_OVERWRITTEN = 1,
  FB_OUT_OF_RANGE = 2,
  FB_IO_ERROR = 3,
  FB_INVALID_ARG = 4
} FbStatus;

typedef enum FbSubtype { FB_FLOAT32 = 0, FB_PCM_24 = 1, FB_PCM_16 = 2 } FbSubtype;

FbRing *fb_ring_create(uint32_t rate, uint16_t channels, double seconds);
void fb_ring_destroy(FbRing *);
void fb_ring_write(FbRing *, const float *frames, size_t n_frames);
uint64_t fb_ring_total_written(const FbRing *);
uint64_t fb_ring_capacity(const FbRing *);
const float *fb_ring_storage(const FbRing *);
void fb_ring_set_gain(FbRing *, float gain);
float fb_ring_gain(const FbRing *);
void fb_ring_flush(FbRing *);
FbStatus fb_ring_read(FbRing *, uint64_t abs_start, size_t n_frames, float *out);
FbStatus fb_ring_summary_bins(FbRing *, size_t n_bins, uint64_t n_samples,
                              uint64_t bin_span_frames, float *out_rms);
FbStatus fb_wav_write(const char *path, const float *frames, size_t n_frames,
                      uint32_t rate, uint16_t channels, FbSubtype subtype);
#endif
```

- [ ] **Step 5: Format + commit (PR comes with Task 7)**

```bash
git checkout -b feat/zig-abi && zig fmt build.zig src && git add core
git commit -m "feat(core): C ABI exports + header"
```

---

### Task 7: `native.py` + parity harness

**Files:**
- Create: `flashback_sampler/core/native.py`, `tests/unit/test_native_smoke.py`
- Modify: `flashback_sampler/core/buffer.py` (extract shared pieces), `tests/unit/test_buffer.py` (parity fixture), `.github/workflows/test.yml` (pytest job builds the DLL)

**Interfaces:**
- Consumes: the ABI from Task 6, exactly as in `flashback_core.h`.
- Produces:
  - `native.load() -> ctypes.CDLL | None` (cached; None when the library isn't built)
  - `native.NativeAudioCircularBuffer(duration_seconds=900.0, sample_rate=48000, channels=2)` — drop-in for `AudioCircularBuffer`'s public surface: `write, get_latest, get_segment, get_peak_bins, get_summary_bins, get_rms_levels, flush, gain, gain_db, buffered_seconds, is_full, status, total_written, write_pos, buffer_size, buffer, sample_rate, channels, duration, close()`
  - `native.wav_write(path, audio: np.ndarray, sample_rate: int, subtype: str) -> None` (raises `RuntimeError` on non-OK status; subtype strings `"FLOAT"|"PCM_24"|"PCM_16"` matching checkout.py's)
  - In `buffer.py`: `class RingDerivedOps` mixin (`gain_db` property, `get_rms_levels`, `buffered_seconds`, `is_full`, `status`) and free function `_peak_bins_impl(ring_view, snapshot, verify, sample_rate, channels, seconds, n_bins)` — both classes use them; `AudioCircularBuffer` behavior unchanged.

- [ ] **Step 1: Failing ctypes smoke test**

`tests/unit/test_native_smoke.py`:

```python
"""Native library smoke: bindings load and round-trip. Skips (not fails)
when the Zig library isn't built, so Zig-less dev environments stay green."""
import numpy as np
import pytest

native = pytest.importorskip("flashback_sampler.core.native")

pytestmark = pytest.mark.skipif(native.load() is None, reason="flashback_core library not built (cd core && zig build -Doptimize=ReleaseSafe)")


def test_roundtrip_write_read():
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8, channels=2)
    frames = np.array([[0.1, -0.1], [0.2, -0.2]], dtype=np.float32)
    buf.write(frames)
    got = buf.get_latest(10.0)
    np.testing.assert_array_equal(got, frames)
    buf.close()


def test_zero_copy_storage_view_sees_writes():
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8, channels=1)
    buf.write(np.array([0.5], dtype=np.float32))
    assert buf.buffer[0, 0] == np.float32(0.5)
    buf.close()
```

Run: `python -m pytest tests/unit/test_native_smoke.py -v` → FAIL (module missing). First build the library: `cd core && zig build -Doptimize=ReleaseSafe && cd ..`

- [ ] **Step 2: Implement `native.py`**

```python
"""
ctypes bindings for the Zig core (core/ → flashback_core shared library).

Mirrors core/include/flashback_core.h. NativeAudioCircularBuffer is a
drop-in for AudioCircularBuffer: Zig owns the memory, the write path,
span reads, summary aggregation, and WAV encoding; Python keeps the
visualization readers over a ZERO-COPY numpy view of Zig-owned storage
(same seqlock verify, no lock — the atomics are on the Zig side).
"""
from __future__ import annotations

import ctypes as C
import sys
from pathlib import Path

import numpy as np

from flashback_sampler.core.buffer import RingDerivedOps, _peak_bins_impl

_OK, _OVERWRITTEN, _OUT_OF_RANGE, _IO_ERROR, _INVALID_ARG = range(5)
# Public: checkout.py routes only subtypes present here to the native
# encoder (anything else falls back to soundfile).
SUBTYPE_INTS = {"FLOAT": 0, "PCM_24": 1, "PCM_16": 2}

_lib: C.CDLL | None = None
_lib_tried = False


def _candidates() -> list[Path]:
    names = {"win32": "flashback_core.dll", "darwin": "libflashback_core.dylib"}
    name = names.get(sys.platform, "libflashback_core.so")
    here = Path(__file__).resolve()
    repo = here.parents[2]
    return [
        here.parent / name,                    # bundled (PyInstaller / wheel)
        repo / "core" / "zig-out" / "bin" / name,   # dev build (Windows DLLs land in bin/)
        repo / "core" / "zig-out" / "lib" / name,   # dev build (unix)
    ]


def load() -> C.CDLL | None:
    """Load and memoize the core library; None if not built anywhere."""
    global _lib, _lib_tried
    if _lib_tried:
        return _lib
    _lib_tried = True
    for path in _candidates():
        if not path.exists():
            continue
        lib = C.CDLL(str(path))
        _declare(lib)
        _lib = lib
        break
    return _lib


def _declare(lib: C.CDLL) -> None:
    f32p = C.POINTER(C.c_float)
    lib.fb_ring_create.argtypes = [C.c_uint32, C.c_uint16, C.c_double]
    lib.fb_ring_create.restype = C.c_void_p
    lib.fb_ring_destroy.argtypes = [C.c_void_p]
    lib.fb_ring_write.argtypes = [C.c_void_p, f32p, C.c_size_t]
    lib.fb_ring_total_written.argtypes = [C.c_void_p]
    lib.fb_ring_total_written.restype = C.c_uint64
    lib.fb_ring_capacity.argtypes = [C.c_void_p]
    lib.fb_ring_capacity.restype = C.c_uint64
    lib.fb_ring_storage.argtypes = [C.c_void_p]
    lib.fb_ring_storage.restype = f32p
    lib.fb_ring_set_gain.argtypes = [C.c_void_p, C.c_float]
    lib.fb_ring_gain.argtypes = [C.c_void_p]
    lib.fb_ring_gain.restype = C.c_float
    lib.fb_ring_flush.argtypes = [C.c_void_p]
    lib.fb_ring_read.argtypes = [C.c_void_p, C.c_uint64, C.c_size_t, f32p]
    lib.fb_ring_read.restype = C.c_int
    lib.fb_ring_summary_bins.argtypes = [C.c_void_p, C.c_size_t, C.c_uint64, C.c_uint64, f32p]
    lib.fb_ring_summary_bins.restype = C.c_int
    lib.fb_wav_write.argtypes = [C.c_char_p, f32p, C.c_size_t, C.c_uint32, C.c_uint16, C.c_int]
    lib.fb_wav_write.restype = C.c_int


def _as_f32p(a: np.ndarray):
    return a.ctypes.data_as(C.POINTER(C.c_float))


def wav_write(path, audio: np.ndarray, sample_rate: int, subtype: str) -> None:
    """Write `audio` [N, channels] float32 via the Zig encoder."""
    lib = load()
    if lib is None:
        raise RuntimeError("flashback_core library not available")
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    n_frames, channels = audio.shape
    status = lib.fb_wav_write(
        str(path).encode("utf-8"), _as_f32p(audio), n_frames,
        sample_rate, channels, SUBTYPE_INTS[subtype],
    )
    if status != _OK:
        raise RuntimeError(f"fb_wav_write failed with status {status}")


class NativeAudioCircularBuffer(RingDerivedOps):
    """AudioCircularBuffer's public surface over the Zig core."""

    def __init__(self, duration_seconds: float = 900.0, sample_rate: int = 48_000, channels: int = 2):
        lib = load()
        if lib is None:
            raise RuntimeError("flashback_core library not available")
        self._lib = lib
        self.sample_rate = sample_rate
        self.channels = channels
        self.duration = duration_seconds
        self._h = lib.fb_ring_create(sample_rate, channels, duration_seconds)
        if not self._h:
            raise MemoryError("fb_ring_create failed")
        self.buffer_size = int(lib.fb_ring_capacity(self._h))
        # Zero-copy view of Zig-owned storage. Read-only by convention;
        # valid until close(). Visualization readers (get_peak_bins)
        # iterate this directly — no copies at 30 Hz.
        storage = lib.fb_ring_storage(self._h)
        self.buffer = np.ctypeslib.as_array(storage, shape=(self.buffer_size, channels))

    # -- primitives -----------------------------------------------------

    @property
    def total_written(self) -> int:
        return int(self._lib.fb_ring_total_written(self._h))

    @property
    def write_pos(self) -> int:
        return self.total_written % self.buffer_size

    @property
    def gain(self) -> float:
        return float(self._lib.fb_ring_gain(self._h))

    @gain.setter
    def gain(self, value: float) -> None:
        self._lib.fb_ring_set_gain(self._h, float(value))

    def write(self, frames: np.ndarray) -> None:
        if frames.ndim == 1:
            frames = frames[:, np.newaxis]
        frames = np.ascontiguousarray(frames, dtype=np.float32)
        self._lib.fb_ring_write(self._h, _as_f32p(frames), len(frames))

    def flush(self) -> None:
        self._lib.fb_ring_flush(self._h)

    def _read_abs(self, abs_start: int, n: int) -> np.ndarray:
        out = np.empty((n, self.channels), dtype=np.float32)
        status = self._lib.fb_ring_read(self._h, abs_start, n, _as_f32p(out))
        if status != _OK:
            # Overwritten mid-read is the seqlock's honest answer for a
            # span that no longer exists; callers get empty, same shape
            # as the Python implementation's clamped-to-nothing result.
            return np.zeros((0, self.channels), dtype=np.float32)
        return out

    # -- AudioCircularBuffer surface ------------------------------------

    def get_latest(self, seconds: float) -> np.ndarray:
        # Unlike the Python impl there is no lock between snapshotting
        # total_written and reading — a fast writer on a tiny ring can lap
        # us in the gap. Re-snapshot and retry; the span rides the writer.
        for _ in range(3):
            tw = self.total_written
            n = min(int(seconds * self.sample_rate), min(tw, self.buffer_size))
            if n <= 0:
                return np.zeros((0, self.channels), dtype=np.float32)
            got = self._read_abs(tw - n, n)
            if len(got):
                return got
        return np.zeros((0, self.channels), dtype=np.float32)

    def get_segment(self, start_ago: float, end_ago: float) -> np.ndarray:
        if start_ago <= end_ago:
            raise ValueError("start_ago must be greater than end_ago")
        tw = self.total_written
        n_avail = min(tw, self.buffer_size)
        avail_secs = n_avail / self.sample_rate
        start_ago = min(start_ago, avail_secs)
        end_ago = max(end_ago, 0.0)
        n_start = int(start_ago * self.sample_rate)
        n_end = int(end_ago * self.sample_rate)
        span = n_start - n_end
        if span <= 0:
            return np.zeros((0, self.channels), dtype=np.float32)
        return self._read_abs(tw - n_start, span)

    def get_peak_bins(self, seconds: float, n_bins: int) -> np.ndarray:
        return _peak_bins_impl(
            ring=self.buffer,
            snapshot=lambda: self.total_written,
            verify=lambda abs_start: self.total_written - abs_start <= self.buffer_size,
            sample_rate=self.sample_rate,
            channels=self.channels,
            seconds=seconds,
            n_bins=n_bins,
        )

    def get_summary_bins(self, n_bins: int, seconds=None, bin_span_samples=None) -> np.ndarray:
        if n_bins <= 0:
            raise ValueError("n_bins must be positive")
        out = np.zeros((n_bins, self.channels), dtype=np.float32)
        n_samples = 0 if seconds is None else int(seconds * self.sample_rate)
        span = 0 if not bin_span_samples else int(bin_span_samples)
        status = self._lib.fb_ring_summary_bins(self._h, n_bins, n_samples, span, _as_f32p(out))
        if status != _OK:
            raise ValueError(f"fb_ring_summary_bins status {status}")
        return out

    def close(self) -> None:
        if self._h:
            self.buffer = None
            self._lib.fb_ring_destroy(self._h)
            self._h = None

    def __del__(self):  # belt-and-braces; tests call close() explicitly
        try:
            self.close()
        except Exception:
            pass
```

- [ ] **Step 3: Extract the shared pieces from `buffer.py` (behavior-preserving refactor)**

Do this refactor with the buffer suite green before and after — no semantic change:

1. **`RingDerivedOps` mixin** — move `gain_db` (property + setter), `get_rms_levels`, `buffered_seconds`, `is_full`, `status` from `AudioCircularBuffer` into a new `class RingDerivedOps` above it, expressed only in terms of `self.get_latest`, `self.total_written`, `self.buffer_size`, `self.sample_rate`, `self.channels`, `self.duration`, `self.gain`, `self.write_pos`. `status()`'s `memory_mb` becomes `self.buffer_size * self.channels * 4 / 1_048_576` (same number, no `.nbytes` dependency). `AudioCircularBuffer(RingDerivedOps)` inherits them. NOTE: `AudioCircularBuffer.total_written`/`write_pos`/`gain` are plain attributes and stay so — the mixin only reads them.
2. **`_peak_bins_impl` free function** — lift the body of `get_peak_bins` (buffer.py:308-419) into `_peak_bins_impl(ring, snapshot, verify, sample_rate, channels, seconds, n_bins)` where `snapshot()` returns `total_written` and `verify(abs_start)` returns "span still valid". Keep `_PEAK_BINS_MAX_SAMPLES_PER_BIN` / `_PEAK_BINS_READ_HEADROOM` as module constants. `AudioCircularBuffer.get_peak_bins` becomes:

```python
def get_peak_bins(self, seconds: float, n_bins: int) -> np.ndarray:
    def snapshot():
        with self._lock:
            return self.total_written
    def verify(abs_start):
        with self._lock:
            return self.total_written - abs_start <= self.buffer_size
    return _peak_bins_impl(self.buffer, snapshot, verify,
                           self.sample_rate, self.channels, seconds, n_bins)
```

(The lock scope narrows from "snapshot indices under lock" to "read the counter under lock" — same guarantees, since the impl's verify step is what protects the copy. The existing flicker/stability tests at test_buffer.py:409,459 are the behavioral gate.)

Run: `python -m pytest tests/unit/test_buffer.py -q` → all green before proceeding.

- [ ] **Step 4: Run the smoke tests**

Run: `python -m pytest tests/unit/test_native_smoke.py -v`
Expected: PASS (with library built). Also confirm skip works: temporarily rename `core/zig-out` → tests SKIP, not fail. Rename back.

- [ ] **Step 5: Parity fixture — the whole buffer suite runs against both implementations**

In `tests/unit/test_buffer.py`, add at top:

```python
from flashback_sampler.core import native as native_mod


@pytest.fixture(params=["python", "native"])
def buffer_cls(request):
    """Every test in this file runs twice: once per implementation.
    This suite IS the parity contract for the Zig core."""
    if request.param == "native":
        if native_mod.load() is None:
            pytest.skip("flashback_core library not built")
        return native_mod.NativeAudioCircularBuffer
    return AudioCircularBuffer
```

Then mechanically: every test takes `buffer_cls` and constructs via `buffer_cls(...)` instead of `AudioCircularBuffer(...)` (31 sites). Tests that poke true internals (`_lock`, `_sum_*`) stay Python-only: give those `def test_x():` (no fixture) with a `# python-impl internal` comment — expected: only tests reading `_sum_*` / `_lock` / monkeypatching internals; everything on the public surface parametrizes.

Run: `python -m pytest tests/unit/test_buffer.py -v`
Expected: roughly 2× the test count, all green. Debug any native-side divergence in the ZIG code or adapter — the Python implementation is the reference; do not weaken a test to pass (that's the parity harness's entire point).

- [ ] **Step 6: WAV decode-equality parity test**

Append to `tests/unit/test_native_smoke.py`:

```python
def test_wav_float32_decode_equals_soundfile(tmp_path):
    import soundfile as sf
    rng = np.random.default_rng(7)
    audio = rng.uniform(-1, 1, size=(4801, 2)).astype(np.float32)
    zig_path, sf_path = tmp_path / "zig.wav", tmp_path / "sf.wav"
    native.wav_write(zig_path, audio, 48_000, "FLOAT")
    sf.write(str(sf_path), audio, 48_000, format="WAV", subtype="FLOAT")
    got_z, sr_z = sf.read(str(zig_path), dtype="float32")
    got_s, sr_s = sf.read(str(sf_path), dtype="float32")
    assert sr_z == sr_s == 48_000
    np.testing.assert_array_equal(got_z, got_s)  # bit-identical samples


@pytest.mark.parametrize("subtype,tol", [("PCM_24", 1 / 8388607), ("PCM_16", 1 / 32767)])
def test_wav_pcm_decode_within_one_lsb_of_soundfile(tmp_path, subtype, tol):
    import soundfile as sf
    rng = np.random.default_rng(11)
    audio = rng.uniform(-1, 1, size=(997, 2)).astype(np.float32)
    zig_path, sf_path = tmp_path / "zig.wav", tmp_path / "sf.wav"
    native.wav_write(zig_path, audio, 48_000, subtype)
    sf.write(str(sf_path), audio, 48_000, format="WAV", subtype=subtype)
    got_z, _ = sf.read(str(zig_path), dtype="float32")
    got_s, _ = sf.read(str(sf_path), dtype="float32")
    assert np.abs(got_z - got_s).max() <= tol  # quantizers may differ by 1 LSB
```

Red first (functions exist, but run to confirm both actually pass — if the FLOAT case fails on header parsing, libsndfile rejected the 44-byte float header: add a `fact` chunk + 18-byte fmt in `wav.zig` and update its golden test accordingly). Green → commit.

- [ ] **Step 7: CI — pytest job builds the library**

In `.github/workflows/test.yml` pytest job, before the test step:

```yaml
      - uses: mlugg/setup-zig@v2
        with:
          version: <ZIGVER>
      - name: Build native core
        run: zig build -Doptimize=ReleaseSafe
        working-directory: core
```

Expected effect: the parity leg stops skipping on CI — verify in the run log that native-param tests RAN (grep the job log for `native` and `PASSED`, not `SKIPPED`); a skip on CI means the DLL wasn't found and the parity gate is silently vacuous.

- [ ] **Step 8: PR for Tasks 6+7**

```bash
git add core flashback_sampler tests .github
git commit -m "feat(core): C ABI + ctypes host + parity harness over both buffer implementations"
git push -u origin feat/zig-abi
gh pr create --fill --body "Closes #<abi-issue>. Zig concepts in this PR: export fn / C ABI, sentinel pointers ([*:0]), opaque handles, comptime force-reference for exports."
```

---

### Task 8: Swap PR — the app runs on the Zig core

**Files:**
- Modify: `flashback_sampler/core/buffer.py` (factory), `flashback_sampler/app/state.py:390`, `flashback_sampler/core/capture.py:32`, `flashback_sampler/core/capture_slot.py:123`, `flashback_sampler/core/loopback_capture.py:46`, `flashback_sampler/core/mixed_capture.py:63`, `flashback_sampler/core/checkout.py:325`, `.github/workflows/release.yml`, `flashback_sampler.spec`, `PLATFORM.md`
- Test: `tests/unit/test_buffer.py` (factory tests), `tests/unit/test_checkout.py` (WAV routing)

**Interfaces:**
- Consumes: `native.load()`, `NativeAudioCircularBuffer`, `native.wav_write` from Task 7.
- Produces: `make_ring_buffer(duration_seconds=900.0, sample_rate=48_000, channels=2)` in `flashback_sampler/core/buffer.py` — the ONLY way app code constructs ring buffers from now on.

- [ ] **Step 1: Failing factory test**

In `tests/unit/test_buffer.py`:

```python
def test_make_ring_buffer_prefers_native_when_available():
    from flashback_sampler.core.buffer import make_ring_buffer
    buf = make_ring_buffer(duration_seconds=1.0, sample_rate=8, channels=2)
    if native_mod.load() is not None:
        assert type(buf).__name__ == "NativeAudioCircularBuffer"
    else:
        assert isinstance(buf, AudioCircularBuffer)
    assert buf.sample_rate == 8 and buf.channels == 2
```

Red → implement in `buffer.py`:

```python
def make_ring_buffer(duration_seconds: float = 900.0, sample_rate: int = 48_000, channels: int = 2):
    """One constructor for every ring buffer in the app: the Zig core
    when its library is present, the Python implementation otherwise.
    (The Python fallback dies with phase 2; every call site already
    speaks the shared surface so deletion will be a no-op here.)"""
    from flashback_sampler.core import native
    if native.load() is not None:
        return native.NativeAudioCircularBuffer(
            duration_seconds=duration_seconds, sample_rate=sample_rate, channels=channels)
    return AudioCircularBuffer(
        duration_seconds=duration_seconds, sample_rate=sample_rate, channels=channels)
```

Green.

- [ ] **Step 2: Route the five construction sites through the factory**

At each site (`app/state.py:390`, `core/capture.py:32`, `core/capture_slot.py:123`, `core/loopback_capture.py:46`, `core/mixed_capture.py:63` — re-grep `AudioCircularBuffer(` to catch drift since this plan was written), replace the constructor call with `make_ring_buffer(...)` preserving that site's exact arguments. Import at each module head: `from flashback_sampler.core.buffer import make_ring_buffer`.

Run: `python -m pytest tests/unit -q` → green (the app-level suites now exercise whichever implementation is present — on this machine, native).

- [ ] **Step 3: WAV checkout routes through the Zig encoder**

Failing test in `tests/unit/test_checkout.py` (follow the file's existing fixture style — read it first):

```python
def test_wav_save_uses_native_encoder_when_available(tmp_path, monkeypatch):
    from flashback_sampler.core import checkout as co, native
    if native.load() is None:
        pytest.skip("flashback_core library not built")
    calls = []
    real = native.wav_write
    monkeypatch.setattr(native, "wav_write", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    # drive the existing save path for a WAV/FLOAT checkout (reuse the
    # file's existing checkout-construction helper/fixture) …
    assert calls, "WAV save did not route through the native encoder"
```

Red → in `checkout.py` around line 325, replace the unconditional `sf.write` with:

```python
if fmt == "WAV" and subtype in native.SUBTYPE_INTS and native.load() is not None:
    native.wav_write(target, np.ascontiguousarray(audio, dtype=np.float32), sr, subtype)
else:
    sf.write(str(target), audio, sr, format=fmt, subtype=subtype)
```

(`from flashback_sampler.core import native` at module head.) FLAC keeps its existing path + FLOAT→PCM_24 coercion untouched. Green — and the existing checkout WAV tests must stay green over the native encoder (they read files back with soundfile: decode-equality holds by Task 7's parity tests).

- [ ] **Step 4: Release packaging**

1. `.github/workflows/release.yml`: add the same setup-zig + `zig build -Doptimize=ReleaseSafe` (working-directory `core`) step before the PyInstaller step.
2. `flashback_sampler.spec`: read the file, then add the DLL to the bundle — in the `Analysis(...)` call extend `binaries` with `[("core/zig-out/bin/flashback_core.dll", "flashback_sampler/core")]` (destination = the package dir `native._candidates()` checks first).
3. Verify locally: `pyinstaller flashback_sampler.spec` then confirm `dist/**/flashback_sampler/core/flashback_core.dll` exists and the built app launches (`run` it, arm a capture, confirm status shows filling).

- [ ] **Step 5: Docs**

- `PLATFORM.md`: add a row/note — audio ring + WAV encode now live in `core/` (Zig, all platforms, no seam needed); update the packaging row.
- Spec: flip **Status:** line to `implemented (PR #s …)`.

- [ ] **Step 6: Full battery + PR**

Run: `python -m pytest -q` (whole suite) and `cd core && zig build test`.
Then the review battery per repo workflow (`/simplify`, then `/code-review` at feature-PR tier — INLINE per user's global policy, one pass, not the multi-agent workflow).

```bash
git checkout -b feat/zig-swap && git add -A
git commit -m "feat: app runs on the Zig core — factory swap + native WAV checkout + bundled DLL"
git push -u origin feat/zig-swap
gh pr create --fill --body "Closes #<swap-issue>. Closes #<epic> checklist item. Zig concepts in this PR: none new — this is the payoff PR."
```

Acceptance before merge (mirror of the DAW-arc bar): on this machine, launch the app, capture system audio ~2 min, scrub both decks, drag a slice into Ableton Live 12, confirm the dropped WAV plays and the project reference survives save/reopen. Report results honestly in the PR.

---

## Verification (whole phase)

1. `cd core && zig build test` — green, including the stress test.
2. `python -m pytest -q` — green; confirm parity params RAN (not skipped).
3. CI: zig job green on 3 OSes; cross-compile leg green; pytest legs green with native tests running.
4. Manual: the Ableton acceptance in Task 8.
5. The numbers that motivated this: measure and record in the epic — app RSS before/after swap, and dropped-callback counter over a 15-min soak (expect: no regression; jitter improvement shows fully in phase 2 when capture moves).
