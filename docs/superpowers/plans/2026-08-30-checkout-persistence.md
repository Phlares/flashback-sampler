# Checkout Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every checkout scratches to a WAV on disk through a Zig writer thread, RAM holds one Zig-owned copy per checkout under a global byte budget, slices are references into their parent's file, and drag-out exports the parent span with markers at the slice.

**Architecture:** `wav.zig` gains a read side and a file→file `copyRange`; `peaks.zig` is the one bin-edge reducer shared by the ring, flat buffers and files. `Checkout.zig` holds the one RAM copy plus `(file, start, n)`; `Scratch.zig` runs the writer thread (write and load jobs on one intrusive FIFO) and the LRU byte cache. `Playback.bind` takes a `ClipSource` union (frames or file range). Python keeps handles, manifests, refcounts and adoption.

**Tech Stack:** Zig 0.16.0 (pinned; zero external deps; `std.Io` file API; `std.Io.Mutex`/`Condition`), ctypes, numpy, PySide6, pytest, `platformdirs`.

**Spec:** `docs/superpowers/specs/2026-08-30-checkout-persistence-design.md` — read it first. Parent: `2026-08-30-zig-core-phase2-d-f-design.md`. This spec wins where they differ.

## Global Constraints

All constraints of `docs/superpowers/plans/2026-08-30-zig-core-phase2-d-f.md` "Global Constraints" apply verbatim. Restated and extended:

- **Zero external Zig dependencies.** `core/build.zig.zon` never gains a `.dependencies` entry.
- **Zig 0.16.0 pinned.** If a std call does not resolve, fix the call site to the pinned API and keep the design. Verified in the pinned std for this plan: `Dir.cwd().openFile(io, path, .{})` (`lib/std/Io/Dir.zig:577`, mode defaults to `.read_only`), `Dir.createFile(io, path, .{})` (`Dir.zig:638`), `Dir.rename(old_dir, old_sub_path, new_dir, new_sub_path, io)` (`Dir.zig:1093`), `Dir.deleteFile(io, path)` (`Dir.zig:1004`), `File.readPositionalAll(io, buf, offset) !usize` (`File.zig:576`), `File.length(io) !u64` (`File.zig:289`), `File.writeStreamingAll(io, bytes)` (`File.zig:623`), `File.close(io)`, `std.Io.Mutex` (`.init`, `lockUncancelable(io)`, `unlock(io)`; `Io.zig:1587`), `std.Io.Condition` (`.init`, `waitUncancelable(io, &mutex)`, `signal(io)`, `broadcast(io)`; `Io.zig:1653`), `std.Thread.spawn(.{}, f, .{args})` / `join()`, `std.testing.io`, `std.testing.tmpDir`. There is no `std.Thread.Mutex` and no `std.Thread.sleep` in 0.16.
- **Python will disappear.** No audio in numpy after PR h; `native.py` keeps ctypes declarations and one-line calls. Python decides *what* (paths, spans, refcounts, manifests); Zig does the work.
- **One copy.** A checkout's frames exist once, in Zig. The writer streams from that copy; `Playback.bind` copies once (from RAM or from the file).
- **Zero heap after `Scratch.start`.** Intrusive lists; `wav.writeFile`'s 64 KiB stack buffer; the reader's 64 KiB stack buffer.
- **RT-safety invariant:** `Ring.write`, capture, mixer and render loops never lock, allocate or fail. `Scratch`'s mutex is never taken on those threads.
- **Control-thread ownership:** the scope that spawns joins. `Scratch.start`/`stop` follow `Capture.start`/`stop` (`core/src/Capture.zig:69-101`).
- **Idiomatic Zig:** file-as-struct, caller-supplied allocators, error sets internally / `FbStatus` at the ABI, fixed arrays on hot paths, instructional comments where a concept first load-bears; each PR carries a "Zig concepts in this PR" section.
- **TDD + mutation-check:** every test seen red before green; compound guards get one mutation per clause; verify by edit-then-revert on the real source. **The Zig gate is "the count rose"** (`zig build --build-file core/build.zig test --summary all`; 146 at the start of this plan). pytest baseline 510 (`python -m pytest tests/unit -q -m "not audio_hw and not perf"`).
- **Every new Zig file is re-exported in `core/src/root.zig` as its own `pub const`** (`refAllDecls` is one level deep). Missing this makes `zig build test` report success having run none of the file's tests.
- **Shipped optimize mode is ReleaseSafe.** Zig tests run in Debug.
- **PRs → `dev`**, one per PR group (g, h, i); the app works at every merge; owner merges. **No CI on feature branches.** Local gate before every push: `python -m pytest tests/unit -q -m "not audio_hw and not perf"` + `zig build --build-file core/build.zig test --summary all` + `zig fmt --check core/src` + `zig build --build-file core/build.zig -Doptimize=ReleaseSafe` for native, `-Dtarget=x86_64-linux-gnu` and `-Dtarget=aarch64-macos`.
- **Deletion policy:** sequester to `_ToRemove/` (gitignored — a move stages nothing; stage the deletion explicitly with `git rm --cached` or `git add -A`), never `rm -rf`; one approval prompt at the end of each PR.
- **Execute in the primary checkout, not a worktree** (`soak_test.py`, `ZIG-101.md`, `PHASE2-HANDOFF.md` are untracked at the repo root; do not touch them).
- **Shell on this machine:** no `cd` compounds, no `$( )`, no `&&`. Always `--build-file core/build.zig`. **Edit `.zig` files with the Edit tool only** (python heredocs mangle em dashes and line endings).
- **No new pip dependencies.** `platformdirs` is already a dependency.
- **Issues are status truth.** One sub-issue per PR under epic #53; comment when something material is learned; `Closes #NN` in the PR body; tick the epic box on merge.
- **Alpha-free UI colours** (`Documents/dev/CLAUDE.md`).
- **Owner-at-the-machine tasks** (the measurement in PR h, the spike in PR i) are marked; the executor prepares everything and stops for the owner.

## File map

| File | Responsibility | PR |
|---|---|---|
| `core/src/peaks.zig` (new) | `PeakBin`, `binEdge`, `reduceFrame`, `peakBinsFlat`, `peakBinsFile` — the one reducer | g |
| `core/src/wav.zig` | + `Info`, `open`, `readFrames`, `decodeSamples`, `copyRange`; PR h: + `io`, `write_mutex`; PR i: + `Markers` | g, h, i |
| `core/src/Ring.zig` | `peakBins` calls `peaks.binEdge`/`reduceFrame`; `PeakBin` becomes an alias | g |
| `core/src/Checkout.zig` (new) | one RAM copy + `(file, start, n)` + write state; `createFromRing`, `adopt`, `slice`, `load`, `evict`, `peakBins`, `source` | h |
| `core/src/Scratch.zig` (new) | writer thread, FIFO (write/load jobs), LRU byte cache, pin/touch/budget | h |
| `core/src/Playback.zig` | `ClipSource` union; `bind(src, rate, channels)` | h |
| `core/src/abi.zig`, `core/include/flashback_core.h` | `fb_wav_*`, `fb_scratch_*`, `fb_checkout_*`, `fb_playback_bind_checkout`; mutex moves to `wav.zig` | g, h, i |
| `core/src/root.zig` | re-exports `peaks`, `Checkout`, `Scratch` | g, h |
| `flashback_sampler/core/native.py` | declarations + `wav_info/wav_read/wav_peak_bins`, `NativeScratch` | g, h |
| `flashback_sampler/core/manifest.py` (new) | per-checkout JSON: write/read/scan/resolve | h |
| `flashback_sampler/core/checkout.py` | `Checkout` over a handle; `CheckoutManager` with refcounts, manifests, `adopt_manifest`, `slice` | h, i |
| `flashback_sampler/core/scrub_player.py` | `bind_checkout(handle, scratch, start, n)` | h |
| `flashback_sampler/core/drag_export.py` | `export_span`, `render_drag_file` over spans + markers | i |
| `flashback_sampler/app/config.py` | `scratch_dir`, `checkout_cache_mb`, `drag_handle_mb` prefs | h, i |
| `flashback_sampler/app/state.py` | owns `NativeScratch`; adoption; RAM accounting | h |
| `flashback_sampler/app/preferences_dialog.py` | scratch dir row; drag cap row | h, i |
| `flashback_sampler/app/turntable_window.py` | bins from the handle, pin on select, bind_checkout, slice on drag, buffer drag ± half | h, i |
| `tests/unit/test_wav_read.py` (new), `test_checkout.py`, `test_manifest.py` (new), `test_scratch.py` (new), `test_drag_export.py`, `test_app_state.py`, `test_turntable_window.py`, `test_scrub_player.py`, `test_config.py` | | g, h, i |

**Task → PR map:** PR **g** `feat/zig-wav-read` Tasks g0–g7 · PR **h** `feat/zig-scratch` Tasks h0–h12 · PR **i** `feat/slices-handles` Tasks i0–i7. Each PR's tasks assume the previous PR merged into `dev`.

**Plan choices recorded up front** (the spec is silent or the code disagrees; each is restated at its task):

| # | Choice | Why |
|---|---|---|
| P1 | `wav.copyRange` ships in PR g **without** a `markers` parameter; PR i adds it and updates both callers. | The spec's `markers: null` in PR g would be a parameter with no definition — a placeholder. |
| P2 | `wav.open` walks chunks with positional reads from the file (not from a fixed header buffer). | DAW-written files carry `bext`/`iXML`/`LIST` chunks of kilobytes before `data`; a fixed buffer would miss `data`. Tests write byte arrays to `std.testing.tmpDir` files. |
| P3 | `ClipSource` lives in `Playback.zig`; `Checkout.zig` imports `Playback` (no cycle: `Playback` never imports `Checkout`). | The consumer owns the type it consumes. |
| P4 | `Checkout.source(start, n)` takes a sub-range so trimmed playback binds the trim without a numpy slice. `fb_playback_bind_checkout` takes `(start, n)`. | Today's trimmed play binds `trimmed_audio()`; the playhead maths (`turntable_window.py:1698-1712`) adds `trim_in` back. Keep both. |
| P5 | The window's `_evict_oldest_saved_checkout` stays for the **count** cap only; its RAM-cap branch dies. | The spec keeps `max_active_checkouts = 16`; without the count-cap eviction the 17th buffer drag fails. Recorded as a spec edit in the PR h hand-off. |
| P6 | `Scratch.write_fn` is a function-pointer field defaulting to the real writer; tests inject a slow or failing writer. | A `comptime` parameter would make `Scratch` generic and the ABI handle type awkward. One field, one seam. |
| P7 | LRU is an intrusive doubly-linked list with move-to-head; no `last_use` tick. | The spec's `last_use` field is redundant with the list order. |
| P8 | Slice checkouts are created from the parent's **handle** (`fb_checkout_slice`) and the manager holds one refcount per file path. Zig never deletes files. | Python owns lifetime decisions (a Python exception must not leak a file), Zig owns bytes. |
| P9 | The export span formula (`export_span`) is a pure Python function. The budget is EXTRA audio around the slice; the slice is never truncated (owner ruling 2026-08-31). | It chooses *what* to export (policy), like `drag_filename`; the copy is Zig. |
| P10 | `Checkout.peakBins` from RAM uses `peaks.peakBinsFlat`; from the file, `peaks.peakBinsFile` with the checkout's `(start, n)`. Both give identical bins for identical audio (Task g4's parity test). | One reducer, two sources. |
| P11 | Manifest bins are stored as flat lists `[n_bins * 2 * channels]` in the numpy layout `(n_bins, 2, channels)`. | The window's `_clip_bins_cache` already holds that layout. |
| P12 | PR h's window changes replace `co.audio.shape[0]` with `co.n_frames` at every site (`turntable_window.py:606, 817, 954, 970, 985, 1703, 1710`). | Mechanical; listed so none is missed. |

---
## PR g — `wav.zig` read side, `peaks.zig`

**Branch:** `feat/zig-wav-read` from `dev`. **Target:** `dev`. **Spec section:** "PR g". Baseline Zig 146 / pytest 510. **Task → count map:** g1 +4 = 150 · g2 +7 = 157 · g3 +6 = 163 · g4 +2 = 165 · g5 +3 = 168 · g6 +2 Zig = 170, pytest +6 = 516.

### Task g0: Branch, sub-issue

**Files:** none.

- [ ] **Step 1: Branch**

```bash
git checkout dev
git pull
git checkout -b feat/zig-wav-read
```

- [ ] **Step 2: Verify the baseline**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: `146/146 tests passed` (if the number differs, record it on the sub-issue and shift every count below).

Run: `python -m pytest tests/unit -q -m "not audio_hw and not perf"`
Expected: `510 passed`.

- [ ] **Step 3: Sub-issue**

```bash
gh issue create --title "PR g: Zig WAV reader + peaks.zig (checkout persistence)" --body "Sub-issue of #53. Spec: docs/superpowers/specs/2026-08-30-checkout-persistence-design.md (PR g). Plan: docs/superpowers/plans/2026-08-30-checkout-persistence.md Tasks g0-g7. Engine only: wav.zig read side (Info/open/readFrames/copyRange), peaks.zig (one reducer for Ring/flat/file), fb_wav_info / fb_wav_read / fb_wav_peak_bins. No production Python change."
```

Add `- [ ] #NN PR g` to the epic #53 task list (edit the issue body with `gh issue edit 53 --body-file`), and comment on #53 that PR g started.

### Task g1: `peaks.zig` — the one reducer; `Ring.peakBins` uses it

**Files:**
- Create: `core/src/peaks.zig`
- Modify: `core/src/Ring.zig:353-450` (`PeakBin`, `binEdge`, `reduceFrame`, `peakBins`)
- Modify: `core/src/root.zig` (re-export)

**Interfaces:**
- Produces: `peaks.PeakBin` (extern struct, unchanged layout), `peaks.binEdge(step: f64, i: usize, n: u64, n_bins: usize) u64`, `peaks.reduceFrame(frame: []const f32, out_bin: []PeakBin, first: *bool) void`, `peaks.peakBinsFlat(frames: []const f32, channels: u16, n_bins: usize, out: []PeakBin) void`. `Ring.PeakBin` stays as `pub const PeakBin = peaks.PeakBin;` so `abi.zig` compiles unchanged.

- [ ] **Step 1: Read the current reducer**

Read `core/src/Ring.zig:353-450`: `PeakBin`, `peak_bins_max_samples_per_bin`, `peak_bins_read_headroom`, `peakBins`, `binEdge`, `reduceFrame`. Note `reduceFrame(self, idx, out_bin, first)` reads the frame at physical index `idx` from `self.frames`.

- [ ] **Step 2: Write the failing tests in `core/src/peaks.zig`**

```zig
//! peaks.zig — the ONE min/max bin reducer. Three callers, one bin-edge
//! rule: Ring.peakBins (live ring, seqlock), peakBinsFlat (a checkout's
//! RAM copy), peakBinsFile (a scratch file, streamed). Bin edges follow
//! numpy.linspace's integer cast: edge_i = trunc(float(i) * step),
//! last edge = n. Keep the multiply order — `i * n / n_bins` rounds
//! differently by one frame on some (n, n_bins) pairs (case G below).
const std = @import("std");

/// Layout is C's: [bin][channel] of {min, max}. The Python host maps
/// this as float32[n_bins][channels][2].
pub const PeakBin = extern struct { min: f32, max: f32 };

test "binEdge matches numpy linspace int64 cast, case G n=30 bins=22" {
    // numpy: edges = linspace(0, 30, 23).astype(int64); edges[11] == 14.
    // The exact rational 11*30/22 == 15 — integer maths would give 15.
    const step: f64 = 30.0 / 22.0;
    try std.testing.expectEqual(@as(u64, 14), binEdge(step, 11, 30, 22));
    try std.testing.expectEqual(@as(u64, 30), binEdge(step, 22, 30, 22));
    try std.testing.expectEqual(@as(u64, 0), binEdge(step, 0, 30, 22));
}

test "peakBinsFlat: min/max per bin per channel, stereo ramp" {
    // 8 frames, 2 channels: L = i, R = -i
    var frames: [16]f32 = undefined;
    for (0..8) |i| {
        frames[i * 2] = @floatFromInt(i);
        frames[i * 2 + 1] = -@as(f32, @floatFromInt(i));
    }
    var out: [4 * 2]PeakBin = undefined; // 4 bins x 2 ch
    peakBinsFlat(&frames, 2, 4, &out);
    // bin 0 = frames 0,1 ; bin 3 = frames 6,7
    try std.testing.expectEqual(@as(f32, 0), out[0].min);
    try std.testing.expectEqual(@as(f32, 1), out[0].max);
    try std.testing.expectEqual(@as(f32, -1), out[1].min); // R of bin 0
    try std.testing.expectEqual(@as(f32, 0), out[1].max);
    try std.testing.expectEqual(@as(f32, 6), out[6].min);
    try std.testing.expectEqual(@as(f32, 7), out[6].max);
    try std.testing.expectEqual(@as(f32, -7), out[7].min);
}

test "peakBinsFlat: an empty bin copies the previous bin; the first empty bin stays zero" {
    // 3 frames into 6 bins: edges 0,0,1,1,2,2,3 -> bins 0,2,4 are empty
    const frames = [_]f32{ 0.5, -0.5, 0.25 };
    var out: [6]PeakBin = undefined;
    peakBinsFlat(&frames, 1, 6, &out);
    try std.testing.expectEqual(@as(f32, 0), out[0].min); // empty, nothing before it
    try std.testing.expectEqual(@as(f32, 0), out[0].max);
    try std.testing.expectEqual(@as(f32, 0.5), out[1].max);
    try std.testing.expectEqual(@as(f32, 0.5), out[2].max); // copied from bin 1
    try std.testing.expectEqual(@as(f32, -0.5), out[3].min);
    try std.testing.expectEqual(@as(f32, -0.5), out[4].min); // copied from bin 3
    try std.testing.expectEqual(@as(f32, 0.25), out[5].max);
}

test "peakBinsFlat: zero frames zeroes every bin" {
    var out: [3 * 2]PeakBin = undefined;
    peakBinsFlat(&.{}, 2, 3, &out);
    for (out) |b| {
        try std.testing.expectEqual(@as(f32, 0), b.min);
        try std.testing.expectEqual(@as(f32, 0), b.max);
    }
}
```

- [ ] **Step 3: Add the re-export and run the tests to see them fail**

In `core/src/root.zig`, after `pub const convert = @import("convert.zig");` add:

```zig
pub const peaks = @import("peaks.zig");
```

Run: `zig build --build-file core/build.zig test --summary all`
Expected: compile error `use of undeclared identifier 'binEdge'` (the tests reference functions not yet written).

- [ ] **Step 4: Implement the reducer**

Append to `core/src/peaks.zig`:

```zig
/// numpy: step = n / n_bins in f64, edge_i = trunc(i * step). The
/// multiply must be `float(i) * step`, not `i * n / n_bins` — a
/// different rounding order moves edges by one frame and shifts every
/// waveform golden (spec "Risks", peak-bin parity).
pub fn binEdge(step: f64, i: usize, n: u64, n_bins: usize) u64 {
    if (i == n_bins) return n; // numpy sets the last edge to `stop` exactly
    // @intFromFloat truncates toward zero == numpy's int64 cast here (non-negative).
    return @intFromFloat(@as(f64, @floatFromInt(i)) * step);
}

/// Fold one interleaved frame into `out_bin` (one PeakBin per channel).
/// `first` is the "no frame seen yet" flag the caller owns per bin: the
/// first frame SETS min and max (a bin's min must not start at 0 when
/// every sample is positive), later frames widen them.
pub fn reduceFrame(frame: []const f32, out_bin: []PeakBin, first: *bool) void {
    if (first.*) {
        for (frame, 0..) |s, c| out_bin[c] = .{ .min = s, .max = s };
        first.* = false;
        return;
    }
    for (frame, 0..) |s, c| {
        out_bin[c].min = @min(out_bin[c].min, s);
        out_bin[c].max = @max(out_bin[c].max, s);
    }
}

/// Bins over a flat interleaved buffer — the numpy `_peak_bins_from_audio`
/// semantics: zeroed output, an empty bin (b <= a) copies the previous
/// bin, bin 0 stays zero when empty. `out.len == n_bins * channels`.
pub fn peakBinsFlat(frames: []const f32, channels: u16, n_bins: usize, out: []PeakBin) void {
    const chans: usize = channels;
    std.debug.assert(out.len == n_bins * chans);
    @memset(out, .{ .min = 0, .max = 0 });
    const n: u64 = frames.len / chans;
    if (n == 0 or n_bins == 0) return;
    const step: f64 = @as(f64, @floatFromInt(n)) / @as(f64, @floatFromInt(n_bins));
    for (0..n_bins) |i| {
        const a = binEdge(step, i, n, n_bins);
        const b = binEdge(step, i + 1, n, n_bins);
        const bin = out[i * chans .. (i + 1) * chans];
        if (b <= a) {
            if (i > 0) @memcpy(bin, out[(i - 1) * chans .. i * chans]);
            continue;
        }
        var first = true;
        var f: usize = @intCast(a);
        const end: usize = @intCast(b);
        while (f < end) : (f += 1) reduceFrame(frames[f * chans .. (f + 1) * chans], bin, &first);
    }
}
```

- [ ] **Step 5: Point `Ring.zig` at the shared pieces**

In `core/src/Ring.zig`:
- Add `const peaks = @import("peaks.zig");` after `const Summary = @import("Summary.zig");`.
- Replace `pub const PeakBin = extern struct { min: f32, max: f32 };` with `pub const PeakBin = peaks.PeakBin;`.
- Delete Ring's private `fn binEdge(...)` and replace every `binEdge(` call inside `peakBins` with `peaks.binEdge(`.
- Replace Ring's private `fn reduceFrame(self: *Ring, idx: u64, out_bin: []PeakBin, first: *bool)` body with a one-line forward:

```zig
/// Frame at PHYSICAL index `idx` (already wrapped by the caller) folded
/// through the shared reducer — Ring owns the wrap, peaks owns the fold.
fn reduceFrame(self: *Ring, idx: u64, out_bin: []PeakBin, first: *bool) void {
    const chans: usize = self.channels;
    const at: usize = @intCast(idx * chans);
    peaks.reduceFrame(self.frames[at .. at + chans], out_bin, first);
}
```

Keep every `peakBins` test in `Ring.zig` untouched: they are the parity gate for this move.

- [ ] **Step 6: Run the tests**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: `150/150 tests passed` (146 + 4). Every existing `Ring.peakBins` test still passes — that is the parity proof for the move.

- [ ] **Step 7: Mutation check (edit, observe, revert)**

In `peaks.binEdge` change `@as(f64, @floatFromInt(i)) * step` to `@as(f64, @floatFromInt(i * n)) / @as(f64, @floatFromInt(n_bins))`. Run the tests. Expected: "case G" reddens (15 ≠ 14) AND Ring's case G test reddens. Revert.
In `reduceFrame` remove the `first` branch (always widen from zero). Expected: the stereo-ramp test reddens (bin 3 min would be 0, not 6). Revert.

- [ ] **Step 8: fmt + commit**

```bash
zig fmt core/src/peaks.zig core/src/Ring.zig core/src/root.zig
git add core/src/peaks.zig core/src/Ring.zig core/src/root.zig
git commit -m "feat(core): peaks.zig — one bin-edge reducer for Ring, flat buffers and files"
```

### Task g2: `wav.zig` — `Info`, `open`, the chunk walk

**Files:**
- Modify: `core/src/wav.zig` (append after `writeFile`)

**Interfaces:**
- Produces: `wav.Info { rate: u32, channels: u16, subtype: Subtype, frames: u64, data_offset: u64 }`, `wav.OpenError`, `wav.Opened { file: std.Io.File, info: Info }`, `wav.open(path: []const u8) OpenError!Opened`, `wav.io` (the `std.Io` every wav call uses; PR h moves `abi.zig`'s mutex next to it).

- [ ] **Step 1: Write the failing tests (append to `core/src/wav.zig`)**

The tests build files from byte arrays in `std.testing.tmpDir` and call `open` through `tmpWritePath` (already in the file).

```zig
/// Test helper: a minimal RIFF/WAVE byte image. `fmt_body` is the raw
/// fmt chunk body (16 bytes plain, 40 bytes EXTENSIBLE); `pre_data` is
/// any chunk bytes to place between fmt and data; `data` is the payload.
fn wavImage(buf: []u8, fmt_body: []const u8, pre_data: []const u8, data: []const u8) []const u8 {
    var w: usize = 0;
    @memcpy(buf[w .. w + 4], "RIFF");
    w += 4;
    const riff_len: u32 = @intCast(4 + 8 + fmt_body.len + pre_data.len + 8 + data.len);
    std.mem.writeInt(u32, buf[w..][0..4], riff_len, .little);
    w += 4;
    @memcpy(buf[w .. w + 4], "WAVE");
    w += 4;
    @memcpy(buf[w .. w + 4], "fmt ");
    w += 4;
    std.mem.writeInt(u32, buf[w..][0..4], @intCast(fmt_body.len), .little);
    w += 4;
    @memcpy(buf[w .. w + fmt_body.len], fmt_body);
    w += fmt_body.len;
    @memcpy(buf[w .. w + pre_data.len], pre_data);
    w += pre_data.len;
    @memcpy(buf[w .. w + 4], "data");
    w += 4;
    std.mem.writeInt(u32, buf[w..][0..4], @intCast(data.len), .little);
    w += 4;
    @memcpy(buf[w .. w + data.len], data);
    w += data.len;
    return buf[0..w];
}

fn fmtPlain(tag: u16, channels: u16, rate: u32, bits: u16) [16]u8 {
    var b: [16]u8 = undefined;
    const block: u16 = channels * (bits / 8);
    std.mem.writeInt(u16, b[0..2], tag, .little);
    std.mem.writeInt(u16, b[2..4], channels, .little);
    std.mem.writeInt(u32, b[4..8], rate, .little);
    std.mem.writeInt(u32, b[8..12], rate * block, .little);
    std.mem.writeInt(u16, b[12..14], block, .little);
    std.mem.writeInt(u16, b[14..16], bits, .little);
    return b;
}

/// WAVE_FORMAT_EXTENSIBLE: tag 0xFFFE, cbSize 22, validBits, channelMask,
/// then a 16-byte SubFormat GUID whose first two bytes are the real tag.
fn fmtExtensible(real_tag: u16, channels: u16, rate: u32, bits: u16) [40]u8 {
    var b: [40]u8 = undefined;
    const head = fmtPlain(0xFFFE, channels, rate, bits);
    @memcpy(b[0..16], &head);
    std.mem.writeInt(u16, b[16..18], 22, .little); // cbSize
    std.mem.writeInt(u16, b[18..20], bits, .little); // valid bits
    std.mem.writeInt(u32, b[20..24], 3, .little); // channel mask L|R
    @memset(b[24..40], 0);
    std.mem.writeInt(u16, b[24..26], real_tag, .little);
    return b;
}

fn writeTmp(tmp: *const std.testing.TmpDir, name: []const u8, bytes: []const u8) !void {
    try tmp.dir.writeFile(std.testing.io, .{ .sub_path = name, .data = bytes });
}

test "open: plain float32 header" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var img: [128]u8 = undefined;
    const data = [_]u8{0} ** 32; // 4 stereo float frames
    const bytes = wavImage(&img, &fmtPlain(3, 2, 48_000, 32), &.{}, &data);
    try writeTmp(&tmp, "plain.wav", bytes);
    var pb: [64]u8 = undefined;
    var o = try open(tmpWritePath(&pb, &tmp, "plain.wav"));
    defer o.file.close(io);
    try std.testing.expectEqual(@as(u32, 48_000), o.info.rate);
    try std.testing.expectEqual(@as(u16, 2), o.info.channels);
    try std.testing.expectEqual(Subtype.float32, o.info.subtype);
    try std.testing.expectEqual(@as(u64, 4), o.info.frames);
    try std.testing.expectEqual(@as(u64, 44), o.info.data_offset);
}

test "open: EXTENSIBLE pcm24 takes the tag from the SubFormat GUID" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var img: [160]u8 = undefined;
    const data = [_]u8{0} ** 18; // 3 stereo pcm24 frames
    const bytes = wavImage(&img, &fmtExtensible(1, 2, 96_000, 24), &.{}, &data);
    try writeTmp(&tmp, "ext.wav", bytes);
    var pb: [64]u8 = undefined;
    var o = try open(tmpWritePath(&pb, &tmp, "ext.wav"));
    defer o.file.close(io);
    try std.testing.expectEqual(Subtype.pcm_24, o.info.subtype);
    try std.testing.expectEqual(@as(u64, 3), o.info.frames);
    try std.testing.expectEqual(@as(u64, 12 + 8 + 40 + 8), o.info.data_offset);
}

test "open: an odd-sized unknown chunk before data is skipped with its pad byte" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var img: [160]u8 = undefined;
    // 'junk' chunk of 3 bytes + 1 pad byte
    const junk = [_]u8{ 'j', 'u', 'n', 'k', 3, 0, 0, 0, 1, 2, 3, 0 };
    const data = [_]u8{0} ** 8; // 2 mono float frames
    const bytes = wavImage(&img, &fmtPlain(3, 1, 44_100, 32), &junk, &data);
    try writeTmp(&tmp, "junk.wav", bytes);
    var pb: [64]u8 = undefined;
    var o = try open(tmpWritePath(&pb, &tmp, "junk.wav"));
    defer o.file.close(io);
    try std.testing.expectEqual(@as(u64, 2), o.info.frames);
    try std.testing.expectEqual(@as(u64, 44 + 12), o.info.data_offset);
}

test "open: data chunk longer than the file clamps frames (a crash-truncated .part)" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var img: [128]u8 = undefined;
    const data = [_]u8{0} ** 16; // header says 16 bytes ...
    const bytes = wavImage(&img, &fmtPlain(3, 1, 8_000, 32), &.{}, &data);
    // ... but write only 44 + 10 bytes: 2 whole frames + 2 stray bytes
    try writeTmp(&tmp, "trunc.wav", bytes[0 .. 44 + 10]);
    var pb: [64]u8 = undefined;
    var o = try open(tmpWritePath(&pb, &tmp, "trunc.wav"));
    defer o.file.close(io);
    try std.testing.expectEqual(@as(u64, 2), o.info.frames);
}

test "open: not RIFF/WAVE, missing fmt, missing data" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    try writeTmp(&tmp, "not.wav", "RIFX....WAVE");
    try std.testing.expectError(error.NotWave, open(tmpWritePath(&pb, &tmp, "not.wav")));
    // fmt absent: a 'junk' chunk then data
    const no_fmt = "RIFF" ++ [_]u8{ 20, 0, 0, 0 } ++ "WAVE" ++ "data" ++ [_]u8{ 4, 0, 0, 0 } ++ [_]u8{ 0, 0, 0, 0 };
    try writeTmp(&tmp, "nofmt.wav", no_fmt);
    try std.testing.expectError(error.MissingFmt, open(tmpWritePath(&pb, &tmp, "nofmt.wav")));
    // data absent
    var img: [64]u8 = undefined;
    const f = fmtPlain(3, 1, 8_000, 32);
    const hdr_only = wavImage(&img, &f, &.{}, &.{});
    try writeTmp(&tmp, "nodata.wav", hdr_only[0 .. hdr_only.len - 8]); // drop the data chunk header
    try std.testing.expectError(error.MissingData, open(tmpWritePath(&pb, &tmp, "nodata.wav")));
}

test "open: pcm32 and float64 are Unsupported" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var img: [128]u8 = undefined;
    var pb: [64]u8 = undefined;
    const d = [_]u8{0} ** 8;
    try writeTmp(&tmp, "p32.wav", wavImage(&img, &fmtPlain(1, 1, 8_000, 32), &.{}, &d));
    try std.testing.expectError(error.Unsupported, open(tmpWritePath(&pb, &tmp, "p32.wav")));
    try writeTmp(&tmp, "f64.wav", wavImage(&img, &fmtPlain(3, 1, 8_000, 64), &.{}, &d));
    try std.testing.expectError(error.Unsupported, open(tmpWritePath(&pb, &tmp, "f64.wav")));
}

test "open: a missing file surfaces the OS error" {
    try std.testing.expectError(error.FileNotFound, open(".zig-cache/tmp/does-not-exist.wav"));
}
```

- [ ] **Step 2: Run to verify they fail**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: compile error `use of undeclared identifier 'open'` / `'io'`.

- [ ] **Step 3: Implement `Info`, `io`, `open`**

Insert after `pub const header_len = 44;` in `core/src/wav.zig`:

```zig
/// The one `std.Io` every wav call uses — the synchronous singleton
/// `writeFile` already reaches for (see its doc comment). Public so
/// callers that hold a `File` from `open` can close it with the same Io.
pub const io = std.Io.Threaded.global_single_threaded.io();

/// What a reader needs to pull samples: format, count, and where the
/// payload starts. `frames` is clamped to what the FILE holds, not what
/// the `data` size claims — a `.part` left by a crash reads its true
/// prefix instead of failing.
pub const Info = struct {
    rate: u32,
    channels: u16,
    subtype: Subtype,
    frames: u64,
    data_offset: u64,

    pub fn blockAlign(self: Info) u64 {
        return @as(u64, self.channels) * self.subtype.bytesPerSample();
    }
};

pub const ParseError = error{ NotWave, MissingFmt, MissingData, Unsupported };
pub const OpenError = ParseError || std.Io.File.OpenError || std.Io.File.ReadPositionalError || std.Io.File.LengthError;
pub const Opened = struct { file: std.Io.File, info: Info };

/// Open `path` and walk its chunks to the `data` chunk. Positional reads
/// (`readPositionalAll` takes an explicit offset, so no seek state is
/// shared between threads). DAW-written files put `bext`, `iXML` and
/// `LIST` chunks of kilobytes before `data`; the walk skips any chunk it
/// does not know, honouring the RIFF word-alignment pad byte.
pub fn open(path: []const u8) OpenError!Opened {
    const file = try std.Io.Dir.cwd().openFile(io, path, .{});
    errdefer file.close(io);
    return .{ .file = file, .info = try scan(file) };
}

const Fmt = struct { channels: u16, rate: u32, block_align: u16, subtype: Subtype };

fn scan(file: std.Io.File) OpenError!Info {
    const len = try file.length(io);
    var hdr: [12]u8 = undefined;
    if (try file.readPositionalAll(io, &hdr, 0) != 12) return error.NotWave;
    if (!std.mem.eql(u8, hdr[0..4], "RIFF") or !std.mem.eql(u8, hdr[8..12], "WAVE")) return error.NotWave;
    var pos: u64 = 12;
    var fmt: ?Fmt = null;
    while (pos + 8 <= len) {
        var ch: [8]u8 = undefined;
        if (try file.readPositionalAll(io, &ch, pos) != 8) break;
        const size: u64 = std.mem.readInt(u32, ch[4..8], .little);
        const body = pos + 8;
        if (std.mem.eql(u8, ch[0..4], "fmt ")) {
            var fb: [40]u8 = undefined;
            const want: usize = @intCast(@min(size, 40));
            if (want < 16 or try file.readPositionalAll(io, fb[0..want], body) != want) return error.MissingFmt;
            fmt = try parseFmt(fb[0..want]);
        } else if (std.mem.eql(u8, ch[0..4], "data")) {
            const f = fmt orelse return error.MissingFmt;
            if (body > len) return error.MissingData;
            const avail = @min(size, len - body);
            return .{
                .rate = f.rate,
                .channels = f.channels,
                .subtype = f.subtype,
                .frames = avail / f.block_align,
                .data_offset = body,
            };
        }
        pos = body + size + (size & 1); // chunks are word-aligned
    }
    return if (fmt == null) error.MissingFmt else error.MissingData;
}

/// The fmt body. Plain: tag u16, channels u16, rate u32, byte rate u32,
/// block align u16, bits u16. EXTENSIBLE (tag 0xFFFE): cbSize u16,
/// valid bits u16, channel mask u32, then the 16-byte SubFormat GUID
/// at offset 24 whose first two bytes carry the real tag. Same rule as
/// tests/fixtures/wavread.py — the independent oracle.
fn parseFmt(fb: []const u8) ParseError!Fmt {
    var tag = std.mem.readInt(u16, fb[0..2], .little);
    const channels = std.mem.readInt(u16, fb[2..4], .little);
    const rate = std.mem.readInt(u32, fb[4..8], .little);
    const block_align = std.mem.readInt(u16, fb[12..14], .little);
    const bits = std.mem.readInt(u16, fb[14..16], .little);
    if (tag == 0xFFFE) {
        if (fb.len < 26) return error.Unsupported;
        tag = std.mem.readInt(u16, fb[24..26], .little);
    }
    const subtype: Subtype = switch (tag) {
        3 => if (bits == 32) Subtype.float32 else return error.Unsupported,
        1 => switch (bits) {
            16 => Subtype.pcm_16,
            24 => Subtype.pcm_24,
            else => return error.Unsupported,
        },
        else => return error.Unsupported,
    };
    if (channels == 0 or rate == 0) return error.Unsupported;
    if (block_align != channels * subtype.bytesPerSample()) return error.Unsupported;
    return .{ .channels = channels, .rate = rate, .block_align = block_align, .subtype = subtype };
}
```

- [ ] **Step 4: Run the tests**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: `157/157 tests passed` (150 + 7).

If `std.Io.File.LengthError` or `ReadPositionalError` do not resolve under those names, open `lib/std/Io/File.zig` in the pinned std, find the error set `length`/`readPositional` return (`File.zig:289`, `File.zig:506`) and use that name; do not widen to `anyerror`.

- [ ] **Step 5: Mutation checks**

(a) In `parseFmt` change `fb[24..26]` to `fb[22..24]`. Expected: the EXTENSIBLE test reddens (`Unsupported`, tag reads 0). Revert.
(b) In `scan` change `avail / f.block_align` to `size / f.block_align`. Expected: the truncation test reddens (4 ≠ 2). Revert.
(c) In `scan` remove `+ (size & 1)`. Expected: the odd-chunk test reddens (`MissingData`). Revert.

- [ ] **Step 6: fmt + commit**

```bash
zig fmt core/src/wav.zig
git add core/src/wav.zig
git commit -m "feat(core): wav.open — chunk walk, EXTENSIBLE fmt, frames clamped to the file"
```

### Task g3: `wav.readFrames` + `decodeSamples`, sample-exact round trip

**Files:**
- Modify: `core/src/wav.zig`

**Interfaces:**
- Produces: `wav.ReadError = error{OutOfRange} || std.Io.File.ReadPositionalError`, `wav.readFrames(file: std.Io.File, info: Info, start_frame: u64, out: []f32) ReadError!void` (`out.len` must be a multiple of `info.channels`; reads `out.len / channels` frames), `wav.decodeSamples(st: Subtype, bytes: []const u8, out: []f32) void`, `wav.read_chunk_bytes = 65536`.

- [ ] **Step 1: Write the failing tests (append to `core/src/wav.zig`)**

```zig
test "decodeSamples: pcm16 and pcm24 scale by 2^(bits-1); float32 is the raw bits" {
    var out: [2]f32 = undefined;
    decodeSamples(.pcm_16, &[_]u8{ 0xFF, 0x7F, 0x00, 0x80 }, &out); // 32767, -32768
    try std.testing.expectApproxEqAbs(@as(f32, 32767.0 / 32768.0), out[0], 1e-9);
    try std.testing.expectEqual(@as(f32, -1.0), out[1]);
    decodeSamples(.pcm_24, &[_]u8{ 0xFF, 0xFF, 0x7F, 0x00, 0x00, 0x80 }, &out); // 8388607, -8388608
    try std.testing.expectApproxEqAbs(@as(f32, 8388607.0 / 8388608.0), out[0], 1e-9);
    try std.testing.expectEqual(@as(f32, -1.0), out[1]);
    const in = [_]f32{ 0.5, -0.25 };
    decodeSamples(.float32, std.mem.sliceAsBytes(&in), &out);
    try std.testing.expectEqualSlices(f32, &in, &out);
}

test "writeFile -> readFrames is sample-exact for float32 and code-exact for pcm" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    // 3 stereo frames, values that survive pcm quantization exactly
    const in = [_]f32{ 0.0, 0.5, -0.5, 1.0, -1.0, 0.25 };
    inline for (.{ Subtype.float32, Subtype.pcm_16, Subtype.pcm_24 }) |st| {
        const path = tmpWritePath(&pb, &tmp, "rt.wav");
        try writeFile(path, &in, 48_000, 2, st);
        var o = try open(path);
        defer o.file.close(io);
        try std.testing.expectEqual(@as(u64, 3), o.info.frames);
        var out: [6]f32 = undefined;
        try readFrames(o.file, o.info, 0, &out);
        // encode then decode: q(x) = round(x * (scale)) / 2^(bits-1)
        const scale: f32 = switch (st) {
            .float32 => 1.0,
            .pcm_16 => 32767.0,
            .pcm_24 => 8388607.0,
        };
        const denom: f32 = switch (st) {
            .float32 => 1.0,
            .pcm_16 => 32768.0,
            .pcm_24 => 8388608.0,
        };
        for (in, out) |x, y| {
            const expect = if (st == .float32) x else @round(x * scale) / denom;
            try std.testing.expectApproxEqAbs(expect, y, 1e-7);
        }
    }
}

test "readFrames: a sub-span starts at start_frame" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    var in: [10]f32 = undefined; // 5 stereo frames: L = i, R = 10 + i
    for (0..5) |i| {
        in[i * 2] = @floatFromInt(i);
        in[i * 2 + 1] = @floatFromInt(10 + i);
    }
    const path = tmpWritePath(&pb, &tmp, "span.wav");
    try writeFile(path, &in, 8_000, 2, .float32);
    var o = try open(path);
    defer o.file.close(io);
    var out: [4]f32 = undefined; // frames 2 and 3
    try readFrames(o.file, o.info, 2, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 2, 12, 3, 13 }, &out);
}

test "readFrames: past the end is OutOfRange, nothing partial" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3 };
    const path = tmpWritePath(&pb, &tmp, "oor.wav");
    try writeFile(path, &in, 8_000, 1, .float32);
    var o = try open(path);
    defer o.file.close(io);
    var out: [2]f32 = undefined;
    try std.testing.expectError(error.OutOfRange, readFrames(o.file, o.info, 2, &out));
}

test "readFrames spans more than one chunk-buffer iteration" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    // read_chunk_bytes / 4 = 16384 float32 mono frames per chunk; +5 forces a second, partial chunk
    const n = read_chunk_bytes / 4 + 5;
    var samples: [n]f32 = undefined;
    for (&samples, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i)) / @as(f32, n);
    const path = tmpWritePath(&pb, &tmp, "big.wav");
    try writeFile(path, &samples, 44_100, 1, .float32);
    var o = try open(path);
    defer o.file.close(io);
    var out: [n]f32 = undefined;
    try readFrames(o.file, o.info, 0, &out);
    try std.testing.expectEqualSlices(f32, &samples, &out);
}

test "readFrames: a truncated .part reads its clamped prefix" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3, 4 };
    const path = tmpWritePath(&pb, &tmp, "part.wav");
    try writeFile(path, &in, 8_000, 1, .float32);
    // chop the file to header + 2 frames + 2 stray bytes
    var f = try std.Io.Dir.cwd().openFile(io, path, .{ .mode = .read_write });
    try f.setLength(io, header_len + 8 + 2);
    f.close(io);
    var o = try open(path);
    defer o.file.close(io);
    try std.testing.expectEqual(@as(u64, 2), o.info.frames);
    var out: [2]f32 = undefined;
    try readFrames(o.file, o.info, 0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 2 }, &out);
}
```

- [ ] **Step 2: Run to verify they fail**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: compile error `use of undeclared identifier 'decodeSamples'`.

- [ ] **Step 3: Implement**

Insert after `open`/`scan`/`parseFmt` in `core/src/wav.zig`:

```zig
/// Read and write share one chunk size: 64 KiB on the stack, never the
/// heap. 16384 float32 mono samples, 8192 stereo frames per iteration.
pub const read_chunk_bytes = 16384 * 4;

pub const ReadError = error{OutOfRange} || std.Io.File.ReadPositionalError;

/// Fill `out` (interleaved, `out.len / info.channels` frames) from
/// `start_frame`. Whole-span or nothing: a span past `info.frames`
/// returns OutOfRange before any read.
pub fn readFrames(file: std.Io.File, info: Info, start_frame: u64, out: []f32) ReadError!void {
    const chans: u64 = info.channels;
    std.debug.assert(out.len % chans == 0);
    const n_frames: u64 = out.len / chans;
    if (start_frame + n_frames > info.frames) return error.OutOfRange;
    const block = info.blockAlign();
    const frames_per_chunk: u64 = read_chunk_bytes / block;
    var buf: [read_chunk_bytes]u8 = undefined;
    var done: u64 = 0;
    while (done < n_frames) {
        const take = @min(n_frames - done, frames_per_chunk);
        const nbytes: usize = @intCast(take * block);
        const offset = info.data_offset + (start_frame + done) * block;
        const got = try file.readPositionalAll(io, buf[0..nbytes], offset);
        // `frames` was clamped to the file at open; a short read here
        // means the file shrank underneath us. Treat it as the span
        // being gone, not as silence.
        if (got != nbytes) return error.OutOfRange;
        const o_start: usize = @intCast(done * chans);
        const o_end: usize = @intCast((done + take) * chans);
        decodeSamples(info.subtype, buf[0..nbytes], out[o_start..o_end]);
        done += take;
    }
}

/// The inverse of `encodeSamples`. PCM codes divide by 2^(bits-1) — the
/// libsndfile convention `tests/fixtures/wavread.py` pins (32767 reads
/// as 32767/32768) — so encode→decode is exact at the codes, not at the
/// original floats. FLOAT32 is a memcpy of the bits.
pub fn decodeSamples(st: Subtype, bytes: []const u8, out: []f32) void {
    switch (st) {
        .float32 => @memcpy(std.mem.sliceAsBytes(out), bytes[0 .. out.len * 4]),
        .pcm_16 => for (out, 0..) |*s, i| {
            const v = std.mem.readInt(i16, bytes[i * 2 ..][0..2], .little);
            s.* = @as(f32, @floatFromInt(v)) / 32768.0;
        },
        .pcm_24 => for (out, 0..) |*s, i| {
            // Three little-endian bytes; shift into the top of an i32 so
            // the arithmetic shift back sign-extends bit 23.
            const raw: u32 = @as(u32, bytes[i * 3]) | (@as(u32, bytes[i * 3 + 1]) << 8) | (@as(u32, bytes[i * 3 + 2]) << 16);
            const v: i32 = @as(i32, @bitCast(raw << 8)) >> 8;
            s.* = @as(f32, @floatFromInt(v)) / 8388608.0;
        },
    }
}
```

- [ ] **Step 4: Run the tests**

Run: `zig build --build-file core/build.zig test --summary all`
Expected: `163/163 tests passed` (157 + 6).

If `File.setLength` is not the 0.16 name in the truncation test, use the name at `lib/std/Io/File.zig:280` (verified: `setLength(file, io, new_length)`).

- [ ] **Step 5: Mutation checks**

(a) In `decodeSamples` pcm_24 change `>> 8` (the arithmetic shift) to a plain `@as(i32, @intCast(raw))`. Expected: the decode test reddens (-8388608 becomes 8388608 → +1.0). Revert.
(b) In `readFrames` remove the `start_frame + n_frames > info.frames` guard. Expected: the OutOfRange test reddens (a short read now surfaces as OutOfRange too, but the truncation test's expectations on `frames` are unchanged — confirm the OutOfRange test is the one that reddens, then revert).
(c) In `readFrames` replace `(start_frame + done) * block` with `done * block`. Expected: the sub-span test reddens. Revert.

- [ ] **Step 6: fmt + commit**

```bash
zig fmt core/src/wav.zig
git add core/src/wav.zig
git commit -m "feat(core): wav.readFrames + decodeSamples, sample-exact round trip"
```

### Task g4: `peaks.peakBinsFile` — streamed bins from a file

**Files:**
- Modify: `core/src/peaks.zig`

**Interfaces:**
- Consumes: `wav.open`, `wav.readFrames`, `wav.Info`, `wav.read_chunk_bytes`.
- Produces: `peaks.peakBinsFile(file: std.Io.File, info: wav.Info, start_frame: u64, n_frames: u64, n_bins: usize, out: []PeakBin) wav.ReadError!void`.

- [ ] **Step 1: Write the failing tests (append to `core/src/peaks.zig`)**

```zig
const wav = @import("wav.zig");

fn tmpPath(buf: []u8, tmp: *const std.testing.TmpDir, name: []const u8) []const u8 {
    return std.fmt.bufPrint(buf, ".zig-cache/tmp/{s}/{s}", .{ tmp.sub_path, name }) catch unreachable;
}

test "peakBinsFile equals peakBinsFlat on the same audio, across a chunk boundary" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    // stereo, 8192 + 100 frames: crosses the 8192-frame stereo chunk
    const n = wav.read_chunk_bytes / 8 + 100;
    var frames: [n * 2]f32 = undefined;
    var x: u32 = 12345;
    for (&frames) |*s| {
        x = x *% 1664525 +% 1013904223; // LCG, deterministic noise
        s.* = @as(f32, @floatFromInt(x >> 8)) / 16777216.0 * 2.0 - 1.0;
    }
    const path = tmpPath(&pb, &tmp, "peaks.wav");
    try wav.writeFile(path, &frames, 48_000, 2, .float32);
    var o = try wav.open(path);
    defer o.file.close(wav.io);
    var from_file: [37 * 2]PeakBin = undefined;
    var from_flat: [37 * 2]PeakBin = undefined;
    try peakBinsFile(o.file, o.info, 0, n, 37, &from_file);
    peakBinsFlat(&frames, 2, 37, &from_flat);
    for (from_file, from_flat) |a, b| {
        try std.testing.expectEqual(b.min, a.min);
        try std.testing.expectEqual(b.max, a.max);
    }
}

test "peakBinsFile on a sub-range equals peakBinsFlat on the slice" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    var frames: [50]f32 = undefined; // mono, 50 frames
    for (&frames, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i % 7)) - 3.0;
    const path = tmpPath(&pb, &tmp, "sub.wav");
    try wav.writeFile(path, &frames, 8_000, 1, .float32);
    var o = try wav.open(path);
    defer o.file.close(wav.io);
    var from_file: [6]PeakBin = undefined;
    var from_flat: [6]PeakBin = undefined;
    try peakBinsFile(o.file, o.info, 10, 30, 6, &from_file); // frames 10..40
    peakBinsFlat(frames[10..40], 1, 6, &from_flat);
    for (from_file, from_flat) |a, b| {
        try std.testing.expectEqual(b.min, a.min);
        try std.testing.expectEqual(b.max, a.max);
    }
}
```

- [ ] **Step 2: Run to verify they fail**

Expected: compile error `use of undeclared identifier 'peakBinsFile'`.

- [ ] **Step 3: Implement**

Append to `core/src/peaks.zig` (before the tests is fine; Zig does not care about order):

```zig
/// Bins over `n_frames` frames of a file starting at `start_frame`,
/// streamed through one 64 KiB stack buffer. Same edges and the same
/// empty-bin rule as peakBinsFlat, so the two agree bit for bit on the
/// same audio (the parity tests below). The bin index advances with the
/// frame counter; a bin that receives no frame is filled from its
/// predecessor in a final pass, exactly as peakBinsFlat does inline.
pub fn peakBinsFile(file: std.Io.File, info: wav.Info, start_frame: u64, n_frames: u64, n_bins: usize, out: []PeakBin) wav.ReadError!void {
    const chans: usize = info.channels;
    std.debug.assert(out.len == n_bins * chans);
    @memset(out, .{ .min = 0, .max = 0 });
    if (n_frames == 0 or n_bins == 0) return;
    if (start_frame + n_frames > info.frames) return error.OutOfRange;
    const step: f64 = @as(f64, @floatFromInt(n_frames)) / @as(f64, @floatFromInt(n_bins));
    const frames_per_chunk: u64 = wav.read_chunk_bytes / (4 * @as(u64, chans));
    var buf: [wav.read_chunk_bytes / 4]f32 = undefined;
    var bin: usize = 0;
    var next_edge: u64 = binEdge(step, 1, n_frames, n_bins);
    var first = true;
    var f: u64 = 0;
    while (f < n_frames) {
        const take = @min(n_frames - f, frames_per_chunk);
        const samples: usize = @intCast(take * chans);
        try wav.readFrames(file, info, start_frame + f, buf[0..samples]);
        var k: u64 = 0;
        while (k < take) : (k += 1) {
            const abs = f + k;
            // Advance past every edge at or before this frame. Bins whose
            // span is empty (edge_i == edge_{i+1}) are skipped here and
            // filled in the pass below.
            while (abs >= next_edge and bin + 1 < n_bins) {
                bin += 1;
                next_edge = binEdge(step, bin + 1, n_frames, n_bins);
                first = true;
            }
            const at: usize = @intCast(k * chans);
            reduceFrame(buf[at .. at + chans], out[bin * chans .. (bin + 1) * chans], &first);
        }
        f += take;
    }
    // Empty bins copy their predecessor (bin 0 stays zero) — the same
    // rule peakBinsFlat applies inline.
    for (1..n_bins) |i| {
        const a = binEdge(step, i, n_frames, n_bins);
        const b = binEdge(step, i + 1, n_frames, n_bins);
        if (b <= a) @memcpy(out[i * chans .. (i + 1) * chans], out[(i - 1) * chans .. i * chans]);
    }
}
```

- [ ] **Step 4: Run the tests**

Expected: `165/165 tests passed` (163 + 2).

- [ ] **Step 5: Mutation check**

Change `while (abs >= next_edge and bin + 1 < n_bins)` to `while (abs > next_edge and bin + 1 < n_bins)`. Expected: the chunk-boundary parity test reddens (bins shift by one frame). Revert.

- [ ] **Step 6: fmt + commit**

```bash
zig fmt core/src/peaks.zig
git add core/src/peaks.zig
git commit -m "feat(core): peaks.peakBinsFile — streamed bins, parity with peakBinsFlat"
```

### Task g5: `wav.copyRange` — file→file streaming copy

**Files:**
- Modify: `core/src/wav.zig`

**Interfaces:**
- Produces: `wav.copyRange(src: []const u8, dst: []const u8, start_frame: u64, n_frames: u64, st: Subtype) !void` (P1: no markers parameter until PR i).

- [ ] **Step 1: Write the failing tests (append to `core/src/wav.zig`)**

```zig
test "copyRange: a sub-span, float32 -> pcm16, reads back as the quantized slice" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    const in = [_]f32{ 0.0, 0.5, -0.5, 1.0, 0.25, -0.25 }; // 6 mono frames
    const src = tmpWritePath(&pa, &tmp, "src.wav");
    try writeFile(src, &in, 8_000, 1, .float32);
    const dst = tmpWritePath(&pd, &tmp, "dst.wav");
    try copyRange(src, dst, 1, 3, .pcm_16); // frames 1..4 = 0.5, -0.5, 1.0
    var o = try open(dst);
    defer o.file.close(io);
    try std.testing.expectEqual(Subtype.pcm_16, o.info.subtype);
    try std.testing.expectEqual(@as(u64, 3), o.info.frames);
    try std.testing.expectEqual(@as(u32, 8_000), o.info.rate);
    var out: [3]f32 = undefined;
    try readFrames(o.file, o.info, 0, &out);
    try std.testing.expectApproxEqAbs(@as(f32, 16384.0 / 32768.0), out[0], 1e-7);
    try std.testing.expectApproxEqAbs(@as(f32, -16384.0 / 32768.0), out[1], 1e-7);
    try std.testing.expectApproxEqAbs(@as(f32, 32767.0 / 32768.0), out[2], 1e-7);
}

test "copyRange: float32 -> float32 is byte-identical to writeFile of the slice" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    var pe: [64]u8 = undefined;
    var in: [40]f32 = undefined; // 20 stereo frames
    for (&in, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i)) * 0.01;
    const src = tmpWritePath(&pa, &tmp, "a.wav");
    try writeFile(src, &in, 48_000, 2, .float32);
    try copyRange(src, tmpWritePath(&pd, &tmp, "b.wav"), 5, 10, .float32);
    try writeFile(tmpWritePath(&pe, &tmp, "c.wav"), in[10..30], 48_000, 2, .float32);
    var b: [header_len + 80]u8 = undefined;
    var c: [header_len + 80]u8 = undefined;
    const gb = try tmp.dir.readFile(std.testing.io, "b.wav", &b);
    const gc = try tmp.dir.readFile(std.testing.io, "c.wav", &c);
    try std.testing.expectEqualSlices(u8, gc, gb);
}

test "copyRange: past the source end is OutOfRange and creates no file" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3 };
    const src = tmpWritePath(&pa, &tmp, "s.wav");
    try writeFile(src, &in, 8_000, 1, .float32);
    try std.testing.expectError(error.OutOfRange, copyRange(src, tmpWritePath(&pd, &tmp, "never.wav"), 2, 5, .float32));
    try std.testing.expectError(error.FileNotFound, tmp.dir.statFile(std.testing.io, "never.wav", .{}));
}
```

- [ ] **Step 2: Run to verify they fail**

Expected: compile error `use of undeclared identifier 'copyRange'`.

- [ ] **Step 3: Implement**

Insert after `decodeSamples`:

```zig
/// Stream `n_frames` from `start_frame` of `src` into a new `dst` in
/// subtype `st`. One 64 KiB read buffer, one 64 KiB encode buffer, both
/// on the stack. The range is validated against the source BEFORE dst
/// is created, so an OutOfRange leaves no file behind. Serialisation
/// with other writers is the caller's job (PR h: `write_mutex`).
pub fn copyRange(src: []const u8, dst: []const u8, start_frame: u64, n_frames: u64, st: Subtype) !void {
    var o = try open(src);
    defer o.file.close(io);
    if (start_frame + n_frames > o.info.frames) return error.OutOfRange;
    const chans: u64 = o.info.channels;
    const data_len_wide: u64 = n_frames * chans * st.bytesPerSample();
    if (data_len_wide > std.math.maxInt(u32) - header_len) return error.TooLong;
    var out = try std.Io.Dir.cwd().createFile(io, dst, .{});
    defer out.close(io);
    var header: [header_len]u8 = undefined;
    writeHeader(&header, o.info.rate, o.info.channels, st, n_frames);
    try out.writeStreamingAll(io, &header);
    const frames_per_chunk: u64 = read_chunk_bytes / (4 * chans);
    var samples: [read_chunk_bytes / 4]f32 = undefined;
    var enc: [read_chunk_bytes]u8 = undefined;
    var done: u64 = 0;
    while (done < n_frames) {
        const take = @min(n_frames - done, frames_per_chunk);
        const ns: usize = @intCast(take * chans);
        try readFrames(o.file, o.info, start_frame + done, samples[0..ns]);
        const n = encodeSamples(st, samples[0..ns], &enc);
        try out.writeStreamingAll(io, enc[0..n]);
        done += take;
    }
}
```

- [ ] **Step 4: Run the tests**

Expected: `168/168 tests passed` (165 + 3).

- [ ] **Step 5: Mutation check**

Move the `OutOfRange` check below `createFile`. Expected: the "creates no file" test reddens (`statFile` finds the file). Revert.

- [ ] **Step 6: fmt + commit**

```bash
zig fmt core/src/wav.zig
git add core/src/wav.zig
git commit -m "feat(core): wav.copyRange — file-to-file streaming slice"
```

### Task g6: ABI + `native.py` + Python oracle tests

**Files:**
- Modify: `core/src/abi.zig` (after `fb_wav_write`, `abi.zig:300-317`)
- Modify: `core/include/flashback_core.h` (after `fb_wav_write`, line 74)
- Modify: `flashback_sampler/core/native.py` (`_declare` after the `fb_wav_write` lines 167-168; new functions after `wav_write`, line 253-266)
- Create: `tests/unit/test_wav_read.py`

**Interfaces:**
- Produces: `fb_wav_info(path, FbWavInfo*) FbStatus`, `fb_wav_read(path, start_frame u64, n_frames size_t, float* out) FbStatus`, `fb_wav_peak_bins(path, start_frame u64, n_frames u64, n_bins size_t, FbPeakBin* out) FbStatus`; Python `native.FbWavInfo`, `native.wav_info(path) -> FbWavInfo`, `native.wav_read(path, start_frame, n_frames) -> np.ndarray (n_frames, channels) float32`, `native.wav_peak_bins(path, start_frame, n_frames, n_bins) -> np.ndarray (n_bins, 2, channels)`. Status mapping: `NotWave/MissingFmt/MissingData/Unsupported` → `invalid_arg`; `OutOfRange` → `out_of_range`; OS errors → `io_error`.

- [ ] **Step 1: Write the failing Python tests — `tests/unit/test_wav_read.py`**

```python
"""fb_wav_info / fb_wav_read / fb_wav_peak_bins against two oracles:
tests/fixtures/wavread.py (an independent stdlib reader) for files the
engine wrote, and a struct-built WAVE_FORMAT_EXTENSIBLE fixture for
DAW-written headers."""
from __future__ import annotations

import struct

import numpy as np
import pytest

from flashback_sampler.core import native
from tests.fixtures.wavread import read_wav


def _ramp(frames: int, channels: int) -> np.ndarray:
    a = np.arange(frames * channels, dtype=np.float32).reshape(frames, channels)
    return a / np.float32(frames * channels)


def _extensible_pcm16(path, rate: int, channels: int, codes: np.ndarray) -> None:
    """A WAVE_FORMAT_EXTENSIBLE header (tag 0xFFFE, 40-byte fmt) around
    little-endian int16 codes — the shape a DAW export carries."""
    data = codes.astype("<i2").tobytes()
    block = channels * 2
    fmt = struct.pack("<HHIIHH", 0xFFFE, channels, rate, rate * block, block, 16)
    fmt += struct.pack("<HHI", 22, 16, 3)  # cbSize, valid bits, channel mask
    fmt += struct.pack("<H", 1) + b"\x00\x00" + bytes.fromhex("000010008000 00aa00389b71".replace(" ", ""))
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


@pytest.mark.parametrize("subtype", ["FLOAT", "PCM_24", "PCM_16"])
def test_wav_read_agrees_with_the_stdlib_oracle(tmp_path, subtype):
    p = tmp_path / "engine.wav"
    audio = _ramp(1000, 2)
    native.wav_write(p, audio, 44_100, subtype)
    info = native.wav_info(p)
    assert (info.rate, info.channels, info.frames) == (44_100, 2, 1000)
    got = native.wav_read(p, 0, 1000)
    oracle, oinfo = read_wav(p)
    assert oinfo.subtype == subtype
    np.testing.assert_array_equal(got, oracle)


def test_wav_read_sub_span(tmp_path):
    p = tmp_path / "span.wav"
    audio = _ramp(50, 1)
    native.wav_write(p, audio, 8_000, "FLOAT")
    got = native.wav_read(p, 10, 5)
    np.testing.assert_array_equal(got, audio[10:15])


def test_wav_read_extensible_pcm16_header(tmp_path):
    p = tmp_path / "daw.wav"
    codes = np.array([[0, 32767], [-32768, 1], [100, -100]], dtype=np.int16)
    _extensible_pcm16(p, 96_000, 2, codes)
    info = native.wav_info(p)
    assert (info.rate, info.channels, info.frames, info.subtype) == (96_000, 2, 3, native.SUBTYPE_INTS["PCM_16"])
    got = native.wav_read(p, 0, 3)
    oracle, _ = read_wav(p)
    np.testing.assert_array_equal(got, oracle)


def test_wav_read_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        native.wav_info(tmp_path / "missing.wav")
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wave file at all")
    with pytest.raises(ValueError):
        native.wav_info(junk)
    p = tmp_path / "short.wav"
    native.wav_write(p, _ramp(4, 1), 8_000, "FLOAT")
    with pytest.raises(ValueError):
        native.wav_read(p, 3, 2)


def test_wav_peak_bins_match_ring_peak_bins_on_the_same_audio(tmp_path):
    # Ring.peakBins and peaks.peakBinsFile share one reducer; prove it
    # through the ABI on a stride-1 window (n <= 256 * n_bins).
    from flashback_sampler.core.native import NativeAudioCircularBuffer
    audio = (np.random.default_rng(7).standard_normal((3000, 2)) * 0.5).astype(np.float32)
    p = tmp_path / "peaks.wav"
    native.wav_write(p, audio, 48_000, "FLOAT")
    buf = NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=48_000, channels=2)
    buf.write(audio)
    from_ring = buf.get_peak_bins(3000 / 48_000, 30)
    from_file = native.wav_peak_bins(p, 0, 3000, 30)
    buf.close()
    np.testing.assert_array_equal(from_file, from_ring)


def test_wav_peak_bins_shape_and_sub_range(tmp_path):
    audio = _ramp(100, 1)
    p = tmp_path / "sub.wav"
    native.wav_write(p, audio, 8_000, "FLOAT")
    bins = native.wav_peak_bins(p, 20, 40, 4)
    assert bins.shape == (4, 2, 1)
    # bin 0 = frames 20..30
    assert bins[0, 0, 0] == pytest.approx(audio[20, 0])
    assert bins[0, 1, 0] == pytest.approx(audio[29, 0])
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_wav_read.py -q`
Expected: `AttributeError: module ... has no attribute 'wav_info'`.

- [ ] **Step 3: Zig exports (`core/src/abi.zig`, after `fb_wav_write`)**

```zig
/// Mirrors FbWavInfo in flashback_core.h. `subtype` is the FbSubtype
/// wire value (0/1/2), the same one fb_wav_write takes.
pub const FbWavInfo = extern struct { rate: u32, channels: u16, subtype: u8, frames: u64 };

fn wavStatus(e: anyerror) FbStatus {
    return switch (e) {
        error.NotWave, error.MissingFmt, error.MissingData, error.Unsupported => .invalid_arg,
        error.OutOfRange => .out_of_range,
        else => .io_error,
    };
}

export fn fb_wav_info(path: [*:0]const u8, out: *FbWavInfo) FbStatus {
    var o = wav.open(std.mem.span(path)) catch |e| return wavStatus(e);
    defer o.file.close(wav.io);
    out.* = .{ .rate = o.info.rate, .channels = o.info.channels, .subtype = @intFromEnum(o.info.subtype), .frames = o.info.frames };
    return .ok;
}

/// `out` holds n_frames * channels floats; channels come from the file
/// (the host reads them with fb_wav_info first).
export fn fb_wav_read(path: [*:0]const u8, start_frame: u64, n_frames: usize, out: [*]f32) FbStatus {
    var o = wav.open(std.mem.span(path)) catch |e| return wavStatus(e);
    defer o.file.close(wav.io);
    wav.readFrames(o.file, o.info, start_frame, out[0 .. n_frames * o.info.channels]) catch |e| return wavStatus(e);
    return .ok;
}

/// `out` holds n_bins * channels FbPeakBin, out[bin * channels + ch] —
/// the same layout as fb_ring_peak_bins.
export fn fb_wav_peak_bins(path: [*:0]const u8, start_frame: u64, n_frames: u64, n_bins: usize, out: [*]peaks.PeakBin) FbStatus {
    if (n_bins == 0) return .invalid_arg;
    var o = wav.open(std.mem.span(path)) catch |e| return wavStatus(e);
    defer o.file.close(wav.io);
    peaks.peakBinsFile(o.file, o.info, start_frame, n_frames, n_bins, out[0 .. n_bins * o.info.channels]) catch |e| return wavStatus(e);
    return .ok;
}

test "fb_wav_info / fb_wav_read round-trip through the exports" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/abi.wav", .{tmp.sub_path}) catch unreachable;
    const in = [_]f32{ 0.1, -0.1, 0.2, -0.2 };
    try std.testing.expectEqual(FbStatus.ok, fb_wav_write(path, &in, 2, 48_000, 2, 0));
    var info: FbWavInfo = undefined;
    try std.testing.expectEqual(FbStatus.ok, fb_wav_info(path, &info));
    try std.testing.expectEqual(@as(u64, 2), info.frames);
    try std.testing.expectEqual(@as(u16, 2), info.channels);
    var out: [4]f32 = undefined;
    try std.testing.expectEqual(FbStatus.ok, fb_wav_read(path, 0, 2, &out));
    try std.testing.expectEqualSlices(f32, &in, &out);
    try std.testing.expectEqual(FbStatus.out_of_range, fb_wav_read(path, 1, 2, &out));
}

test "fb_wav_info maps a missing file to io_error and junk to invalid_arg" {
    var info: FbWavInfo = undefined;
    try std.testing.expectEqual(FbStatus.io_error, fb_wav_info(".zig-cache/tmp/nope.wav", &info));
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    try tmp.dir.writeFile(std.testing.io, .{ .sub_path = "junk.wav", .data = "not a wave file at all" });
    var pb: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/junk.wav", .{tmp.sub_path}) catch unreachable;
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_info(path, &info));
}
```

Add `const peaks = @import("peaks.zig");` next to `abi.zig`'s other imports.

- [ ] **Step 4: Header (`core/include/flashback_core.h`, after line 74's `fb_wav_write`)**

```c
typedef struct FbWavInfo { uint32_t rate; uint16_t channels; uint8_t subtype; uint64_t frames; } FbWavInfo;
/* Reader side of wav.zig. FB_INVALID_ARG: not RIFF/WAVE, no fmt/data, or
 * an unsupported format (PCM32, float64, ...). FB_OUT_OF_RANGE: the span
 * runs past the frames the FILE holds (a crash-truncated file reports its
 * true prefix, not the header's claim). FB_IO_ERROR: OS errors. */
FbStatus fb_wav_info(const char *path, FbWavInfo *out);
/* out holds n_frames * channels floats (channels from fb_wav_info). */
FbStatus fb_wav_read(const char *path, uint64_t start_frame, size_t n_frames, float *out);
/* out holds n_bins * channels FbPeakBin, out[bin * channels + ch]. */
FbStatus fb_wav_peak_bins(const char *path, uint64_t start_frame, uint64_t n_frames, size_t n_bins, FbPeakBin *out);
```

- [ ] **Step 5: Python (`flashback_sampler/core/native.py`)**

Add the structure next to `FbPeakBin`:

```python
class FbWavInfo(C.Structure):
    _fields_ = [("rate", C.c_uint32), ("channels", C.c_uint16), ("subtype", C.c_uint8), ("frames", C.c_uint64)]
```

In `_declare`, after the `fb_wav_write` lines:

```python
    lib.fb_wav_info.argtypes = [C.c_char_p, C.POINTER(FbWavInfo)]
    lib.fb_wav_info.restype = C.c_int
    lib.fb_wav_read.argtypes = [C.c_char_p, C.c_uint64, C.c_size_t, f32p]
    lib.fb_wav_read.restype = C.c_int
    lib.fb_wav_peak_bins.argtypes = [C.c_char_p, C.c_uint64, C.c_uint64, C.c_size_t, C.POINTER(FbPeakBin)]
    lib.fb_wav_peak_bins.restype = C.c_int
```

After `wav_write`:

```python
def _wav_raise(status: int, path) -> None:
    """One status → exception rule for the three readers."""
    if status == _OK:
        return
    if status == _IO_ERROR:
        raise FileNotFoundError(f"cannot open {path}")
    if status == _INVALID_ARG:
        raise ValueError(f"not a supported WAV file: {path}")
    if status == _OUT_OF_RANGE:
        raise ValueError(f"span runs past the end of {path}")
    raise RuntimeError(f"wav reader failed with status {status} on {path}")


def _require_lib() -> C.CDLL:
    lib = load()
    if lib is None:
        raise RuntimeError("flashback_core library not available")
    return lib


def wav_info(path) -> FbWavInfo:
    lib = _require_lib()
    info = FbWavInfo()
    _wav_raise(lib.fb_wav_info(str(path).encode("utf-8"), C.byref(info)), path)
    return info


def wav_read(path, start_frame: int, n_frames: int) -> np.ndarray:
    """(n_frames, channels) float32 decoded by the Zig reader."""
    lib = _require_lib()
    info = wav_info(path)
    out = np.zeros((int(n_frames), int(info.channels)), dtype=np.float32)
    _wav_raise(lib.fb_wav_read(str(path).encode("utf-8"), int(start_frame), int(n_frames), _as_f32p(out)), path)
    return out


def wav_peak_bins(path, start_frame: int, n_frames: int, n_bins: int) -> np.ndarray:
    """(n_bins, 2, channels) float32 — the get_peak_bins layout, from a file."""
    lib = _require_lib()
    info = wav_info(path)
    out = np.zeros((int(n_bins), int(info.channels), 2), dtype=np.float32)
    _wav_raise(lib.fb_wav_peak_bins(
        str(path).encode("utf-8"), int(start_frame), int(n_frames), int(n_bins),
        out.ctypes.data_as(C.POINTER(FbPeakBin)),
    ), path)
    return np.ascontiguousarray(out.transpose(0, 2, 1))
```

Replace the `load()` / `raise RuntimeError` pair inside `wav_write` with `lib = _require_lib()` (same behaviour, one helper).

- [ ] **Step 6: Build and run both suites**

```bash
zig build --build-file core/build.zig -Doptimize=ReleaseSafe
zig build --build-file core/build.zig test --summary all
python -m pytest tests/unit/test_wav_read.py tests/unit/test_native_smoke.py -q
```

Expected: `170/170 tests passed`; `6 passed` in `test_wav_read.py`; smoke unchanged.

- [ ] **Step 7: Mutation check through the ABI**

In `wavStatus` map `error.OutOfRange` to `.io_error`. Expected: `test_wav_read_errors` reddens (FileNotFoundError instead of ValueError on the short read) and the Zig round-trip test reddens. Revert.

- [ ] **Step 8: fmt + commit**

```bash
zig fmt core/src/abi.zig
git add core/src/abi.zig core/include/flashback_core.h flashback_sampler/core/native.py tests/unit/test_wav_read.py
git commit -m "feat: fb_wav_info / fb_wav_read / fb_wav_peak_bins + Python wrappers and oracle tests"
```

### Task g7: Gates, docs, PR g

**Files:**
- Modify: `README.md` / `PLATFORM.md` only if they list the ABI surface (grep `fb_wav_write` in `*.md`; add the three readers beside it).

- [ ] **Step 1: Full local gate**

```bash
zig fmt --check core/src
zig build --build-file core/build.zig test --summary all
zig build --build-file core/build.zig -Doptimize=ReleaseSafe
zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=x86_64-linux-gnu
zig build --build-file core/build.zig -Doptimize=ReleaseSafe -Dtarget=aarch64-macos
python -m pytest tests/unit -q -m "not audio_hw and not perf"
```

Expected: fmt clean; `170/170`; three builds green; `516 passed`.

- [ ] **Step 2: Whole-branch review** (one combined inline `/simplify` + `/code-review` pass at **medium**, per the owner's review rule). Fix findings; re-run Step 1.

- [ ] **Step 3: Push + PR**

```bash
git push -u origin feat/zig-wav-read
gh pr create --base dev --title "PR g: Zig WAV reader + peaks.zig" --body-file .superpowers/pr-g.md
```

PR body (write `.superpowers/pr-g.md`, gitignored dir): `Closes #NN` (the sub-issue); what shipped; counts before/after; the "Zig concepts in this PR" section (positional reads through `std.Io`, error-set unions mapped once at the ABI, one reducer shared by three callers via slices, `inline for` over an enum tuple in the round-trip test); "Deviations": P1 (no markers param in g), P2 (chunk walk reads the file).

- [ ] **Step 4: Tracker**

Comment on #53: PR g open, counts, link. Owner merges; tick the epic box; close the sub-issue by hand if `Closes` did not fire.

---
## PR h — `Checkout.zig`, `Scratch.zig`, the cache, manifests, adoption

**Branch:** `feat/zig-scratch` from `dev` (after PR g merged). **Target:** `dev`. **Spec sections:** "Data model", "PR h", "Error handling". Baseline after g: Zig 170 / pytest 516. **Task → count map (Zig):** h0 +0 = 170 · h1 +6 = 176 · h2 +5 = 181 · h3 +6 = 187 · h4 +6 = 193 · h5 +3 = 196 · h6 +3 = 199. pytest: h6 +4 · h7 (rewrite of `test_checkout.py`, net ≈ +6) · h8 +8 · h9 +6 · h10 (window, net ≈ +4) → ≈ 544; the hand-off states the real number.

**Thread rules for this PR (restated at every task that touches them):**
- `Scratch.mutex` guards: the FIFO links (`queue_next`, `queue_head/tail`), the LRU links (`lru_prev/next`, `lru_head/tail`), `resident_bytes`, `budget_bytes`, and every `Checkout.job` / `Checkout.pinned` / `Checkout.frames` read or write **except** inside a running job (the worker owns `frames` while `co.job == .load`, and reads it while `co.job == .write`).
- `Checkout.write_state` is an atomic; the ABI reads it without the mutex.
- The worker never holds the mutex during file I/O.
- An ABI call that reads `frames` on the control thread calls `Scratch.waitLoad(co)` first (blocks only while a `.load` job for that checkout is queued or running; never waits on a `.write`).

### Task h0: Branch, sub-issue, `wav.write_mutex`

**Files:**
- Modify: `core/src/wav.zig` (next to `pub const io`)
- Modify: `core/src/abi.zig:32-57` (`wav_write_io`, `wav_write_mutex`, `fb_wav_write`)

**Interfaces:**
- Produces: `wav.write_mutex: std.Io.Mutex` — every `wav.writeFile` / `wav.copyRange` caller locks it with `wav.io`.

- [ ] **Step 1: Branch + baseline**

```bash
git checkout dev
git pull
git checkout -b feat/zig-scratch
zig build --build-file core/build.zig test --summary all
python -m pytest tests/unit -q -m "not audio_hw and not perf"
```

Expected: `170/170`; `516 passed`. Record the real numbers on the sub-issue.

- [ ] **Step 2: Sub-issue**

```bash
gh issue create --title "PR h: Checkout.zig + Scratch.zig — scratch to disk, RAM as cache, adoption" --body "Sub-issue of #53. Spec: docs/superpowers/specs/2026-08-30-checkout-persistence-design.md (Data model, PR h). Plan Tasks h0-h12. Checkout.audio (numpy) is deleted; Python holds handles. Includes the owner-at-the-machine measurement (Task h11) that sets DEFAULT_CHECKOUT_CACHE_MB."
```

Add `- [ ] #NN PR h` to #53's task list.

- [ ] **Step 3: Move the mutex**

In `core/src/wav.zig`, directly after `pub const io = ...;`:

```zig
/// Serialises every writer in this file (`writeFile`, `copyRange`).
/// `global_single_threaded` is documented as not supporting concurrency
/// (see `writeFile`'s doc comment); rather than trace every syscall
/// wrapper for shared state, one lock makes the question moot. Callers
/// lock it: `wav.write_mutex.lockUncancelable(wav.io)` /
/// `defer wav.write_mutex.unlock(wav.io)`. Never taken on an audio
/// thread. Lives here (not in abi.zig) because Scratch.zig must lock
/// it too and Scratch must not import abi.
pub var write_mutex: std.Io.Mutex = .init;
```

In `core/src/abi.zig`: delete `const wav_write_io = ...;` and `var wav_write_mutex: std.Io.Mutex = .init;` together with the comment block above them (lines 32-57); keep a two-line pointer: `// fb_wav_write serialises through wav.write_mutex — see wav.zig.` In `fb_wav_write` replace the two lock lines with:

```zig
    wav.write_mutex.lockUncancelable(wav.io);
    defer wav.write_mutex.unlock(wav.io);
```

- [ ] **Step 4: Build, test, commit**

```bash
zig build --build-file core/build.zig test --summary all
zig fmt core/src/wav.zig core/src/abi.zig
git add core/src/wav.zig core/src/abi.zig
git commit -m "refactor(core): wav.write_mutex — one lock for every wav writer, owned by wav.zig"
```

Expected: `170/170` (no new tests; the move is covered by the existing `fb_wav_write` tests).

### Task h1: `Checkout.zig` — create from ring, adopt, slice, destroy

**Files:**
- Create: `core/src/Checkout.zig`
- Modify: `core/src/root.zig` (re-export)

**Interfaces:**
- Consumes: `Ring.read(abs_start, out) ReadError!void`, `Ring.max_write_frames`, `ring.sample_rate`, `ring.channels`.
- Produces: `Checkout.WriteState` (`queued=0, writing=1, written=2, failed=3, adopted=4`), `Checkout.Job` (`none, write, load`), `Checkout.max_path = 1024`, fields listed below, `Checkout.createFromRing(allocator, ring: *Ring, abs_start: u64, abs_end: u64, path: []const u8) !*Checkout`, `Checkout.adopt(allocator, path, start_frame: u64, n_frames: u64, rate: u32, channels: u16) !*Checkout`, `Checkout.slice(allocator, parent: *const Checkout, start: u64, n: u64) !*Checkout`, `Checkout.destroy(self) void`, `Checkout.path(self) []const u8`, `Checkout.residentBytes(self) u64`.

- [ ] **Step 1: Write the file with its tests**

`core/src/Checkout.zig`:

```zig
//! Checkout.zig — one checkout: the ONE RAM copy of its frames (or none,
//! once evicted) plus where the same audio lives on disk:
//! `(path, start_frame, n_frames)`. A root owns its file at (0, all); a
//! slice references its parent's file. Python decides lifetimes (which
//! file is deleted when); this file holds bytes and moves them.
//!
//! Concurrency: `write_state` is atomic (the ABI polls it without a
//! lock). `job`, `pinned`, the list links and `frames` are guarded by
//! Scratch.mutex — see Scratch.zig's "Thread rules". Nothing here locks;
//! Scratch is the only caller that mutates a checkout after creation.
const std = @import("std");
const Ring = @import("Ring.zig");
const wav = @import("wav.zig");
const peaks = @import("peaks.zig");
const Playback = @import("Playback.zig");

const Checkout = @This();

/// Backed by u8 so the wire value is stable across Zig versions — the
/// Python host reads it from FbCheckoutInfo.
pub const WriteState = enum(u8) { queued = 0, writing = 1, written = 2, failed = 3, adopted = 4 };
/// What the FIFO link means while this checkout sits in Scratch's queue.
pub const Job = enum(u8) { none, write, load };
pub const max_path = 1024;

allocator: std.mem.Allocator,
/// The one RAM copy. null once evicted (a root after its write) or never
/// held (a slice, an adopted file).
frames: ?[]f32,
path_buf: [max_path]u8,
path_len: usize,
/// Offset into the file. 0 for a root; a slice's offset into its parent.
start_frame: u64,
n_frames: u64,
rate: u32,
channels: u16,
write_state: std.atomic.Value(WriteState),
job: Job,
pinned: bool,
queue_next: ?*Checkout,
lru_prev: ?*Checkout,
lru_next: ?*Checkout,

fn create(allocator: std.mem.Allocator, p: []const u8, start_frame: u64, n_frames: u64, rate: u32, channels: u16, frames: ?[]f32, ws: WriteState) !*Checkout {
    if (p.len >= max_path) return error.PathTooLong;
    const self = try allocator.create(Checkout);
    self.* = .{
        .allocator = allocator,
        .frames = frames,
        .path_buf = undefined,
        .path_len = p.len,
        .start_frame = start_frame,
        .n_frames = n_frames,
        .rate = rate,
        .channels = channels,
        .write_state = std.atomic.Value(WriteState).init(ws),
        .job = .none,
        .pinned = false,
        .queue_next = null,
        .lru_prev = null,
        .lru_next = null,
    };
    @memcpy(self.path_buf[0..p.len], p);
    return self;
}

pub fn path(self: *const Checkout) []const u8 {
    return self.path_buf[0..self.path_len];
}

pub fn residentBytes(self: *const Checkout) u64 {
    return if (self.frames) |f| @as(u64, f.len) * 4 else 0;
}

/// A root: copy `[abs_start, abs_end)` out of the ring into a fresh
/// allocation. Read in `Ring.max_write_frames` pieces so a torn piece
/// retries the piece (Ring.read retries three times internally), not
/// the clip. Any piece that fails frees the allocation and returns the
/// ring's error — nothing half-built escapes.
pub fn createFromRing(allocator: std.mem.Allocator, ring: *Ring, abs_start: u64, abs_end: u64, p: []const u8) !*Checkout {
    if (abs_end <= abs_start) return error.InvalidArgument;
    const n = abs_end - abs_start;
    const chans: u64 = ring.channels;
    const frames = try allocator.alloc(f32, @intCast(n * chans));
    errdefer allocator.free(frames);
    var done: u64 = 0;
    while (done < n) {
        const take = @min(n - done, Ring.max_write_frames);
        const a: usize = @intCast(done * chans);
        const b: usize = @intCast((done + take) * chans);
        try ring.read(abs_start + done, frames[a..b]);
        done += take;
    }
    return create(allocator, p, 0, n, ring.sample_rate, ring.channels, frames, .queued);
}

/// A file that already exists on disk (adoption at launch). No frames
/// are read here; the first use loads or streams them.
pub fn adopt(allocator: std.mem.Allocator, p: []const u8, start_frame: u64, n_frames: u64, rate: u32, channels: u16) !*Checkout {
    return create(allocator, p, start_frame, n_frames, rate, channels, null, .adopted);
}

/// A reference into `parent`'s file. Never owns frames of its own and
/// never writes; `adopted` because there is nothing to write.
pub fn slice(allocator: std.mem.Allocator, parent: *const Checkout, start: u64, n: u64) !*Checkout {
    if (n == 0 or start + n > parent.n_frames) return error.InvalidArgument;
    return create(allocator, parent.path(), parent.start_frame + start, n, parent.rate, parent.channels, null, .adopted);
}

pub fn destroy(self: *Checkout) void {
    if (self.frames) |f| self.allocator.free(f);
    self.allocator.destroy(self);
}

test "createFromRing copies the exact span and starts queued" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var in: [2000]f32 = undefined; // 1000 stereo frames: L = i, R = -i
    for (0..1000) |i| {
        in[i * 2] = @floatFromInt(i);
        in[i * 2 + 1] = -@as(f32, @floatFromInt(i));
    }
    ring.write(&in);
    const co = try createFromRing(std.testing.allocator, &ring, 100, 110, "x.wav");
    defer co.destroy();
    try std.testing.expectEqual(@as(u64, 10), co.n_frames);
    try std.testing.expectEqual(@as(u64, 0), co.start_frame);
    try std.testing.expectEqual(@as(u32, 1000), co.rate);
    try std.testing.expectEqual(WriteState.queued, co.write_state.load(.acquire));
    try std.testing.expectEqualSlices(u8, "x.wav", co.path());
    try std.testing.expectEqual(@as(u64, 80), co.residentBytes());
    try std.testing.expectEqual(@as(f32, 100), co.frames.?[0]);
    try std.testing.expectEqual(@as(f32, -109), co.frames.?[19]);
}

test "createFromRing reads a span longer than max_write_frames in pieces" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    const n: usize = @intCast(Ring.max_write_frames * 2 + 7);
    var in: [n]f32 = undefined;
    for (&in, 0..) |*s, i| s.* = @floatFromInt(i);
    ring.write(&in);
    const co = try createFromRing(std.testing.allocator, &ring, 0, n, "p.wav");
    defer co.destroy();
    try std.testing.expectEqual(@as(f32, @floatFromInt(n - 1)), co.frames.?[n - 1]);
    try std.testing.expectEqual(@as(f32, @floatFromInt(Ring.max_write_frames)), co.frames.?[@intCast(Ring.max_write_frames)]);
}

test "createFromRing: an unwritten span is OutOfRange and leaks nothing" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2, 3 });
    try std.testing.expectError(error.OutOfRange, createFromRing(std.testing.allocator, &ring, 1, 5, "o.wav"));
    try std.testing.expectError(error.InvalidArgument, createFromRing(std.testing.allocator, &ring, 3, 3, "o.wav"));
}

test "adopt holds no frames and is adopted" {
    const co = try adopt(std.testing.allocator, "a.wav", 5, 40, 44_100, 2);
    defer co.destroy();
    try std.testing.expectEqual(@as(?[]f32, null), co.frames);
    try std.testing.expectEqual(@as(u64, 0), co.residentBytes());
    try std.testing.expectEqual(WriteState.adopted, co.write_state.load(.acquire));
    try std.testing.expectEqual(@as(u64, 5), co.start_frame);
}

test "slice references the parent's file at the parent's offset" {
    const parent = try adopt(std.testing.allocator, "parent.wav", 100, 50, 48_000, 2);
    defer parent.destroy();
    const s = try slice(std.testing.allocator, parent, 10, 20);
    defer s.destroy();
    try std.testing.expectEqualSlices(u8, "parent.wav", s.path());
    try std.testing.expectEqual(@as(u64, 110), s.start_frame);
    try std.testing.expectEqual(@as(u64, 20), s.n_frames);
    try std.testing.expectEqual(@as(?[]f32, null), s.frames);
    try std.testing.expectError(error.InvalidArgument, slice(std.testing.allocator, parent, 40, 11));
    try std.testing.expectError(error.InvalidArgument, slice(std.testing.allocator, parent, 0, 0));
}

test "a path at max_path is rejected" {
    const long = [_]u8{'a'} ** max_path;
    try std.testing.expectError(error.PathTooLong, adopt(std.testing.allocator, &long, 0, 1, 8_000, 1));
}
```

- [ ] **Step 2: Re-export and run**

In `core/src/root.zig` after `pub const Mixer = ...;`:

```zig
pub const Checkout = @import("Checkout.zig");
```

Run: `zig build --build-file core/build.zig test --summary all`
Expected: `176/176` (170 + 6). If the count did not rise, the re-export is missing (Global Constraints).

- [ ] **Step 3: Mutation check**

In `createFromRing` replace `take = @min(n - done, Ring.max_write_frames)` with `take = n - done` (one read). Expected: the pieces test still passes (Ring.read accepts any length) — this mutation is NOT detected by design; the piecewise read is a torn-read policy, not a correctness change. Instead mutate `ring.read(abs_start + done, ...)` to `ring.read(abs_start, ...)`: the pieces test reddens (frame 4096 reads 0). Revert.

- [ ] **Step 4: fmt + commit**

```bash
zig fmt core/src/Checkout.zig core/src/root.zig
git add core/src/Checkout.zig core/src/root.zig
git commit -m "feat(core): Checkout.zig — one RAM copy + (file, start, n); create/adopt/slice"
```

### Task h2: `Playback.ClipSource` + `Checkout.load/evict/peakBins/source`

**Files:**
- Modify: `core/src/Playback.zig:84-114` (`bind`)
- Modify: `core/src/abi.zig` (`fb_playback_bind` wraps `.frames`)
- Modify: `core/src/Checkout.zig`

**Interfaces:**
- Produces: `Playback.ClipSource = union(enum) { frames: []const f32, file: struct { path: []const u8, start_frame: u64, n_frames: u64 } }`, `Playback.bind(self, src: ClipSource, rate: u32, channels: u16) !void`; `Checkout.load(self) !void`, `Checkout.evict(self) void`, `Checkout.peakBins(self, n_bins: usize, out: []peaks.PeakBin) !void`, `Checkout.source(self, start: u64, n: u64) Playback.ClipSource`.

- [ ] **Step 1: Failing Playback tests (append to `core/src/Playback.zig`)**

Look at the existing tests around `Playback.zig:300-370` for `test_spec` and the `FakeBackend` setup; reuse them.

```zig
test "bind from a file range reads the same clip as bind from frames" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    var in: [40]f32 = undefined; // 20 stereo frames
    for (&in, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i)) * 0.01;
    const path = std.fmt.bufPrint(&pb, ".zig-cache/tmp/{s}/clip.wav", .{tmp.sub_path}) catch unreachable;
    try wav.writeFile(path, &in, 48_000, 2, .float32);

    var fake = FakeBackend.init(&.{});
    var p = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer p.deinit();
    try p.bind(.{ .file = .{ .path = path, .start_frame = 5, .n_frames = 10 } }, 48_000, 2);
    try std.testing.expectEqual(@as(u64, 10), p.clip_frames.load(.acquire));
    try std.testing.expectEqualSlices(f32, in[10..30], p.clip);
    try p.bind(.{ .frames = in[10..30] }, 48_000, 2);
    try std.testing.expectEqualSlices(f32, in[10..30], p.clip);
}

test "bind from a missing file fails and keeps the previous clip" {
    var fake = FakeBackend.init(&.{});
    var p = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer p.deinit();
    const in = [_]f32{ 1, 2, 3, 4 };
    try p.bind(.{ .frames = &in }, 48_000, 2);
    try std.testing.expectError(error.FileNotFound, p.bind(.{ .file = .{ .path = ".zig-cache/tmp/none.wav", .start_frame = 0, .n_frames = 2 } }, 48_000, 2));
    try std.testing.expectEqualSlices(f32, &in, p.clip);
    try std.testing.expectEqual(@as(u64, 2), p.clip_frames.load(.acquire));
}
```

Add `const wav = @import("wav.zig");` to `Playback.zig`'s imports.

- [ ] **Step 2: Run to see them fail**

Expected: compile error (`bind` takes a slice; `.file` is not a slice).

- [ ] **Step 3: Implement `ClipSource` and the new `bind`**

Replace `pub fn bind(self: *Playback, frames: []const f32, rate: u32, channels: u16) !void { ... }` in `core/src/Playback.zig` with:

```zig
/// Where a clip comes from. `frames` is copied (the caller's slice may
/// die the moment bind returns); `file` is read straight into the new
/// clip buffer — one copy either way, never a second RAM copy of the
/// clip. Checkout.source() picks the arm from its residency.
pub const ClipSource = union(enum) {
    frames: []const f32,
    file: struct { path: []const u8, start_frame: u64, n_frames: u64 },
};

/// Control thread. The ONLY place the clip is allocated or freed. The
/// previous clip survives a failed bind (a missing file, a short read):
/// the new buffer is filled BEFORE the old one is released.
pub fn bind(self: *Playback, src: ClipSource, rate: u32, channels: u16) !void {
    // Clause order is load-bearing: `channels == 0` must come first, or
    // the modulo below divides by zero.
    const n_samples: usize = switch (src) {
        .frames => |f| f.len,
        .file => |f| @intCast(f.n_frames * channels),
    };
    if (channels == 0 or channels > 2 or rate == 0 or n_samples % channels != 0) return error.InvalidArgument;
    const copy = try self.allocator.alloc(f32, n_samples);
    errdefer self.allocator.free(copy);
    switch (src) {
        .frames => |f| @memcpy(copy, f),
        .file => |f| {
            var o = try wav.open(f.path);
            defer o.file.close(wav.io);
            try wav.readFrames(o.file, o.info, f.start_frame, copy);
        },
    }
    // Handshake with fill(): clear `playing`, then wait for any copy in
    // flight. fill() raises in_copy BEFORE it reads `playing`, so once we
    // observe in_copy == false after storing playing = false, no copy can
    // start on the old clip. seq_cst on both sides makes the two stores
    // and two loads globally ordered (Dekker's pattern).
    self.playing.store(false, .seq_cst);
    while (self.in_copy.load(.seq_cst)) std.Thread.yield() catch {};
    self.allocator.free(self.clip);
    self.clip = copy;
    self.cursor.store(0, .release);
    self.clip_frames.store(n_samples / channels, .release);
    if (rate != self.rate or channels != self.channels) {
        self.rate = rate;
        self.channels = channels;
        // The thread reopens the stream at the new format on its next
        // wake; no stream is opened here (bind may run before play).
        self.reopen.store(true, .release);
    }
}
```

Every existing `p.bind(&in, ...)` / `pb.bind(frames, ...)` call in `Playback.zig`'s tests becomes `p.bind(.{ .frames = &in }, ...)`. Grep: `grep -n "\.bind(" core/src/Playback.zig core/src/abi.zig`.

In `core/src/abi.zig`, `fb_playback_bind` becomes:

```zig
export fn fb_playback_bind(pb: *Playback, frames: [*]const f32, n_frames: usize, rate: u32, channels: u16) FbStatus {
    if (channels == 0) return .invalid_arg;
    pb.bind(.{ .frames = frames[0 .. n_frames * channels] }, rate, channels) catch |e| return switch (e) {
        error.InvalidArgument => .invalid_arg,
        error.OutOfMemory => .out_of_memory,
        else => .io_error,
    };
    return .ok;
}
```

- [ ] **Step 4: Failing Checkout tests (append to `core/src/Checkout.zig`)**

```zig
fn tmpPath(buf: []u8, tmp: *const std.testing.TmpDir, name: []const u8) []const u8 {
    return std.fmt.bufPrint(buf, ".zig-cache/tmp/{s}/{s}", .{ tmp.sub_path, name }) catch unreachable;
}

test "load reads the file range into frames; evict frees them" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    var in: [20]f32 = undefined; // 10 stereo frames
    for (&in, 0..) |*s, i| s.* = @floatFromInt(i);
    const path = tmpPath(&pb, &tmp, "l.wav");
    try wav.writeFile(path, &in, 8_000, 2, .float32);
    const co = try adopt(std.testing.allocator, path, 2, 5, 8_000, 2);
    defer co.destroy();
    try co.load();
    try std.testing.expectEqual(@as(u64, 40), co.residentBytes());
    try std.testing.expectEqualSlices(f32, in[4..14], co.frames.?);
    try co.load(); // idempotent: no second allocation (testing.allocator would report a leak)
    co.evict();
    try std.testing.expectEqual(@as(?[]f32, null), co.frames);
    try std.testing.expectEqual(@as(u64, 0), co.residentBytes());
}

test "peakBins from RAM equals peakBins from the file" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    var in: [200]f32 = undefined; // 100 stereo frames
    for (&in, 0..) |*s, i| s.* = @as(f32, @floatFromInt((i * 37) % 101)) / 101.0 - 0.5;
    const path = tmpPath(&pb, &tmp, "p.wav");
    try wav.writeFile(path, &in, 8_000, 2, .float32);
    const co = try adopt(std.testing.allocator, path, 10, 80, 8_000, 2);
    defer co.destroy();
    var from_file: [9 * 2]peaks.PeakBin = undefined;
    try co.peakBins(9, &from_file);
    try co.load();
    var from_ram: [9 * 2]peaks.PeakBin = undefined;
    try co.peakBins(9, &from_ram);
    for (from_file, from_ram) |a, b| {
        try std.testing.expectEqual(b.min, a.min);
        try std.testing.expectEqual(b.max, a.max);
    }
    // and both equal the flat reducer on the same slice
    var flat: [9 * 2]peaks.PeakBin = undefined;
    peaks.peakBinsFlat(in[20..180], 2, 9, &flat);
    for (flat, from_ram) |a, b| try std.testing.expectEqual(a.max, b.max);
}

test "source: resident gives a frames sub-slice, evicted gives a file range at the parent offset" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 0, 1, 2, 3, 4, 5, 6, 7 }; // 8 mono frames
    const path = tmpPath(&pb, &tmp, "s.wav");
    try wav.writeFile(path, &in, 8_000, 1, .float32);
    const co = try adopt(std.testing.allocator, path, 2, 5, 8_000, 1); // frames 2..7
    defer co.destroy();
    switch (co.source(1, 3)) {
        .file => |f| {
            try std.testing.expectEqual(@as(u64, 3), f.start_frame);
            try std.testing.expectEqual(@as(u64, 3), f.n_frames);
            try std.testing.expectEqualSlices(u8, path, f.path);
        },
        .frames => return error.TestUnexpectedResult,
    }
    try co.load();
    switch (co.source(1, 3)) {
        .frames => |f| try std.testing.expectEqualSlices(f32, &[_]f32{ 3, 4, 5 }, f),
        .file => return error.TestUnexpectedResult,
    }
}
```

- [ ] **Step 5: Implement in `Checkout.zig`**

```zig
/// Read this checkout's range from its file into a fresh allocation.
/// Idempotent: resident frames stay. Called by the writer thread for a
/// `.load` job, or by tests.
pub fn load(self: *Checkout) !void {
    if (self.frames != null) return;
    var o = try wav.open(self.path());
    defer o.file.close(wav.io);
    const buf = try self.allocator.alloc(f32, @intCast(self.n_frames * self.channels));
    errdefer self.allocator.free(buf);
    try wav.readFrames(o.file, o.info, self.start_frame, buf);
    self.frames = buf;
}

/// Drop the RAM copy. The caller (Scratch) has checked write_state and
/// pin; the audio lives on in the file.
pub fn evict(self: *Checkout) void {
    if (self.frames) |f| {
        self.allocator.free(f);
        self.frames = null;
    }
}

/// Bins for the deck: from RAM when resident, streamed from the file
/// otherwise. `out.len == n_bins * channels`. Same reducer both ways,
/// so the bins are identical whichever path served them.
pub fn peakBins(self: *Checkout, n_bins: usize, out: []peaks.PeakBin) !void {
    if (self.frames) |f| {
        peaks.peakBinsFlat(f, self.channels, n_bins, out);
        return;
    }
    var o = try wav.open(self.path());
    defer o.file.close(wav.io);
    try peaks.peakBinsFile(o.file, o.info, self.start_frame, self.n_frames, n_bins, out);
}

/// `[start, start + n)` of this checkout as a Playback.ClipSource: a
/// sub-slice of the RAM copy, or a file range at the checkout's
/// offset. The caller has validated `start + n <= n_frames`.
pub fn source(self: *const Checkout, start: u64, n: u64) Playback.ClipSource {
    const chans: u64 = self.channels;
    if (self.frames) |f| {
        const a: usize = @intCast(start * chans);
        const b: usize = @intCast((start + n) * chans);
        return .{ .frames = f[a..b] };
    }
    return .{ .file = .{ .path = self.path(), .start_frame = self.start_frame + start, .n_frames = n } };
}
```

- [ ] **Step 6: Run**

Expected: `181/181` (176 + 5: 2 Playback + 3 Checkout).

- [ ] **Step 7: Mutation checks**

(a) In `Playback.bind` move `self.allocator.free(self.clip); self.clip = copy;` above the `switch (src)` fill. Expected: "keeps the previous clip" reddens. Revert.
(b) In `Checkout.source` drop `self.start_frame +`. Expected: the source test reddens (start 1 ≠ 3). Revert.

- [ ] **Step 8: fmt + commit**

```bash
zig fmt core/src/Playback.zig core/src/Checkout.zig core/src/abi.zig
git add core/src/Playback.zig core/src/Checkout.zig core/src/abi.zig
git commit -m "feat(core): Playback.bind(ClipSource); Checkout load/evict/peakBins/source"
```

### Task h3: `Scratch.zig` — the writer thread and the FIFO

**Files:**
- Create: `core/src/Scratch.zig`
- Modify: `core/src/root.zig`

**Interfaces:**
- Produces: `Scratch.WriteFn`, `Scratch.init(budget_bytes: u64) Scratch`, `start(self) !void`, `stop(self) void`, `submit(self, co: *Checkout, job: Checkout.Job) void`, `waitLoad(self, co) void`, `waitJob(self, co) void` (test helper: waits for `co.job == .none`), `write_fn` field. LRU/pin/touch/budget come in Task h4 but the fields are declared here.

- [ ] **Step 1: Write the file with its tests**

`core/src/Scratch.zig`:

```zig
//! Scratch.zig — the scratch writer thread and the RAM cache, one per
//! process. Two job kinds ride one intrusive FIFO: `.write` streams a
//! fresh checkout's RAM copy to `<path>.part` and renames it to
//! `<path>`; `.load` reads an evicted checkout back into RAM (preload on
//! select). The same struct holds the LRU byte cache (Task h4).
//!
//! Thread rules (see the plan's PR h header): `mutex` guards the FIFO
//! links, the LRU links, `resident_bytes`, `budget_bytes`, and every
//! `Checkout.job` / `pinned` / `frames` access on the control thread.
//! The worker owns `co.frames` while `co.job == .load` and only reads
//! it while `co.job == .write`. The worker never holds `mutex` during
//! file I/O. Zero allocation after `start`: the lists are intrusive and
//! the file buffers are wav.zig's stack buffers.
//!
//! Zig 0.16: blocking primitives live under std.Io (`std.Io.Mutex`,
//! `std.Io.Condition`) and take the Io they block on; the singleton
//! `wav.io` is a real futex underneath (see abi.zig's note on the
//! wav_write mutex, now wav.write_mutex).
const std = @import("std");
const wav = @import("wav.zig");
const Checkout = @import("Checkout.zig");

const Scratch = @This();
const io = wav.io;

/// The writer seam: production writes a float32 WAV through wav.writeFile
/// under wav.write_mutex; tests inject a slow or failing writer.
pub const WriteFn = *const fn (path: []const u8, frames: []const f32, rate: u32, channels: u16) anyerror!void;
pub const max_part_path = Checkout.max_path + 5; // ".part"

mutex: std.Io.Mutex = .init,
cond: std.Io.Condition = .init,
queue_head: ?*Checkout = null,
queue_tail: ?*Checkout = null,
lru_head: ?*Checkout = null, // most recently used
lru_tail: ?*Checkout = null, // next to evict
resident_bytes: u64 = 0,
budget_bytes: u64,
thread: ?std.Thread = null,
stop_flag: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
write_fn: WriteFn = &defaultWrite,

pub fn init(budget_bytes: u64) Scratch {
    return .{ .budget_bytes = budget_bytes };
}

/// Control thread. The scope that spawns joins (`stop`).
pub fn start(self: *Scratch) !void {
    if (self.thread != null) return error.AlreadyRunning;
    self.stop_flag.store(false, .monotonic);
    self.thread = try std.Thread.spawn(.{}, run, .{self});
}

/// Control thread. The worker finishes every queued job before it
/// exits, so a quit never leaves a `.part` behind unless the process
/// dies. Blocks for the drain.
pub fn stop(self: *Scratch) void {
    const t = self.thread orelse return;
    self.stop_flag.store(true, .release);
    self.mutex.lockUncancelable(io);
    self.cond.broadcast(io);
    self.mutex.unlock(io);
    t.join();
    self.thread = null;
}

/// Queue `job` for `co`. A checkout already queued is left alone. A
/// `.write` submission is the moment a root becomes resident in the
/// cache's eyes: its bytes are counted and it is linked at the LRU head.
pub fn submit(self: *Scratch, co: *Checkout, job: Checkout.Job) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    self.submitLocked(co, job);
}

fn submitLocked(self: *Scratch, co: *Checkout, job: Checkout.Job) void {
    if (co.job != .none or job == .none) return;
    co.job = job;
    co.queue_next = null;
    if (self.queue_tail) |t| t.queue_next = co else self.queue_head = co;
    self.queue_tail = co;
    if (job == .write) self.lruInsertHeadLocked(co);
    self.cond.broadcast(io);
}

/// Block while a `.load` job for `co` is queued or running. Never waits
/// on a `.write` (the write only reads `frames`; a bind right after a
/// checkout must not wait for a gigabyte to hit the disk).
pub fn waitLoad(self: *Scratch, co: *Checkout) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    while (co.job == .load) self.cond.waitUncancelable(io, &self.mutex);
}

/// Block until `co` has no job at all. Tests and `forget` use it.
pub fn waitJob(self: *Scratch, co: *Checkout) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    while (co.job != .none) self.cond.waitUncancelable(io, &self.mutex);
}

fn run(self: *Scratch) void {
    while (true) {
        self.mutex.lockUncancelable(io);
        while (self.queue_head == null and !self.stop_flag.load(.acquire)) {
            self.cond.waitUncancelable(io, &self.mutex);
        }
        const co = self.queue_head orelse {
            // stop requested and nothing left: the drain is complete.
            self.mutex.unlock(io);
            return;
        };
        self.queue_head = co.queue_next;
        if (self.queue_head == null) self.queue_tail = null;
        co.queue_next = null;
        const job = co.job;
        self.mutex.unlock(io);

        switch (job) {
            .write => self.doWrite(co),
            .load => self.doLoad(co),
            .none => unreachable, // submitLocked never queues .none
        }

        self.mutex.lockUncancelable(io);
        co.job = .none;
        self.cond.broadcast(io);
        self.evictOverBudgetLocked();
        self.mutex.unlock(io);
    }
}

/// Stream the RAM copy to `<path>.part`, then rename to `<path>`. The
/// rename is the "complete" signal a crash cannot fake: a `.part` on
/// disk at launch is by definition partial. Any failure marks the
/// checkout `failed`; it stays resident and unevictable (Task h4 skips
/// non-written entries), so the audio is never lost to a full disk.
fn doWrite(self: *Scratch, co: *Checkout) void {
    co.write_state.store(.writing, .release);
    const frames = co.frames orelse {
        co.write_state.store(.failed, .release);
        return;
    };
    var pb: [max_part_path]u8 = undefined;
    const part = std.fmt.bufPrint(&pb, "{s}.part", .{co.path()}) catch unreachable;
    self.write_fn(part, frames, co.rate, co.channels) catch {
        co.write_state.store(.failed, .release);
        return;
    };
    std.Io.Dir.cwd().rename(part, std.Io.Dir.cwd(), co.path(), io) catch {
        co.write_state.store(.failed, .release);
        return;
    };
    co.write_state.store(.written, .release);
}

/// Read the file back into RAM. A failure leaves the checkout evicted;
/// the fallback bind reads the file itself. Accounting happens under
/// the mutex once the bytes exist.
fn doLoad(self: *Scratch, co: *Checkout) void {
    co.load() catch return;
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    if (co.frames != null and !self.lruLinkedLocked(co)) self.lruInsertHeadLocked(co);
}

fn defaultWrite(path: []const u8, frames: []const f32, rate: u32, channels: u16) anyerror!void {
    wav.write_mutex.lockUncancelable(wav.io);
    defer wav.write_mutex.unlock(wav.io);
    try wav.writeFile(path, frames, rate, channels, .float32);
}

// ---- LRU (Task h4 fills in pin/touch/budget; the list ops live here) ----

fn lruLinkedLocked(self: *Scratch, co: *Checkout) bool {
    return co.lru_prev != null or co.lru_next != null or self.lru_head == co;
}

fn lruInsertHeadLocked(self: *Scratch, co: *Checkout) void {
    co.lru_prev = null;
    co.lru_next = self.lru_head;
    if (self.lru_head) |h| h.lru_prev = co else self.lru_tail = co;
    self.lru_head = co;
    self.resident_bytes += co.residentBytes();
}

fn lruRemoveLocked(self: *Scratch, co: *Checkout) void {
    if (!self.lruLinkedLocked(co)) return;
    if (co.lru_prev) |p| p.lru_next = co.lru_next else self.lru_head = co.lru_next;
    if (co.lru_next) |n| n.lru_prev = co.lru_prev else self.lru_tail = co.lru_prev;
    co.lru_prev = null;
    co.lru_next = null;
    self.resident_bytes -= co.residentBytes();
}

fn evictOverBudgetLocked(self: *Scratch) void {
    _ = self; // Task h4
}

// ---- tests ----

const Ring = @import("Ring.zig");

fn tmpPath(buf: []u8, tmp: *const std.testing.TmpDir, name: []const u8) []const u8 {
    return std.fmt.bufPrint(buf, ".zig-cache/tmp/{s}/{s}", .{ tmp.sub_path, name }) catch unreachable;
}

fn testRoot(tmp: *const std.testing.TmpDir, pb: []u8, name: []const u8, ring: *Ring, n: u64) !*Checkout {
    return Checkout.createFromRing(std.testing.allocator, ring, 0, n, tmpPath(pb, tmp, name));
}

/// Test writer: records the order of paths it was asked to write, and
/// writes a real file so the rename has something to move.
const Recorder = struct {
    var order: [8]u8 = undefined; // last byte of each path's stem, in write order
    var count: usize = 0;
    var fail_next: bool = false;
    var park: std.atomic.Value(bool) = std.atomic.Value(bool).init(false);

    fn reset() void {
        count = 0;
        fail_next = false;
        park.store(false, .monotonic);
    }
    fn write(path: []const u8, frames: []const f32, rate: u32, channels: u16) anyerror!void {
        while (park.load(.acquire)) std.Thread.yield() catch {};
        if (fail_next) {
            fail_next = false;
            return error.DiskFull;
        }
        // path ends in "<x>.wav.part": record <x>
        order[count] = path[path.len - 10];
        count += 1;
        try wav.writeFile(path, frames, rate, channels, .float32);
    }
};

test "a queued write lands as <path>: .part gone, written state, bytes intact" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    const in = [_]f32{ 1, 2, 3, 4 };
    ring.write(&in);
    var s = Scratch.init(1 << 30);
    var pb: [64]u8 = undefined;
    const co = try testRoot(&tmp, &pb, "a.wav", &ring, 4);
    defer co.destroy();
    try s.start();
    s.submit(co, .write);
    s.waitJob(co);
    s.stop();
    try std.testing.expectEqual(Checkout.WriteState.written, co.write_state.load(.acquire));
    try std.testing.expectError(error.FileNotFound, tmp.dir.statFile(std.testing.io, "a.wav.part", .{}));
    var o = try wav.open(co.path());
    defer o.file.close(wav.io);
    var out: [4]f32 = undefined;
    try wav.readFrames(o.file, o.info, 0, &out);
    try std.testing.expectEqualSlices(f32, &in, &out);
}

test "jobs run FIFO and stop drains the queue" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2, 3 });
    Recorder.reset();
    var s = Scratch.init(1 << 30);
    s.write_fn = &Recorder.write;
    var p1: [64]u8 = undefined;
    var p2: [64]u8 = undefined;
    var p3: [64]u8 = undefined;
    const a = try testRoot(&tmp, &p1, "1.wav", &ring, 3);
    defer a.destroy();
    const b = try testRoot(&tmp, &p2, "2.wav", &ring, 3);
    defer b.destroy();
    const c = try testRoot(&tmp, &p3, "3.wav", &ring, 3);
    defer c.destroy();
    Recorder.park.store(true, .release); // hold the worker so all three queue up
    try s.start();
    s.submit(a, .write);
    s.submit(b, .write);
    s.submit(c, .write);
    Recorder.park.store(false, .release);
    s.stop(); // must not return before all three are written
    try std.testing.expectEqual(@as(usize, 3), Recorder.count);
    try std.testing.expectEqualSlices(u8, "123", Recorder.order[0..3]);
    try std.testing.expectEqual(Checkout.WriteState.written, c.write_state.load(.acquire));
}

test "a failing writer marks the checkout failed and keeps its frames" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2 });
    Recorder.reset();
    Recorder.fail_next = true;
    var s = Scratch.init(1 << 30);
    s.write_fn = &Recorder.write;
    var pb: [64]u8 = undefined;
    const co = try testRoot(&tmp, &pb, "f.wav", &ring, 2);
    defer co.destroy();
    try s.start();
    s.submit(co, .write);
    s.waitJob(co);
    s.stop();
    try std.testing.expectEqual(Checkout.WriteState.failed, co.write_state.load(.acquire));
    try std.testing.expect(co.frames != null);
}

test "an unwritable path (missing directory) marks failed without a panic" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{1});
    var s = Scratch.init(1 << 30);
    const co = try Checkout.createFromRing(std.testing.allocator, &ring, 0, 1, ".zig-cache/tmp/no-such-dir/x.wav");
    defer co.destroy();
    try s.start();
    s.submit(co, .write);
    s.waitJob(co);
    s.stop();
    try std.testing.expectEqual(Checkout.WriteState.failed, co.write_state.load(.acquire));
}

test "a load job reads an evicted checkout back and waitLoad returns once it is resident" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 5, 6, 7 };
    const path = tmpPath(&pb, &tmp, "ld.wav");
    try wav.writeFile(path, &in, 8_000, 1, .float32);
    const co = try Checkout.adopt(std.testing.allocator, path, 0, 3, 8_000, 1);
    defer co.destroy();
    var s = Scratch.init(1 << 30);
    try s.start();
    s.submit(co, .load);
    s.waitLoad(co);
    s.stop();
    try std.testing.expectEqualSlices(f32, &in, co.frames.?);
    try std.testing.expectEqual(@as(u64, 12), s.resident_bytes);
}

test "submit ignores a checkout that is already queued; start twice is AlreadyRunning" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{1});
    var s = Scratch.init(1 << 30);
    const co = try Checkout.createFromRing(std.testing.allocator, &ring, 0, 1, "q.wav");
    defer co.destroy();
    s.submit(co, .write);
    s.submit(co, .load); // ignored: job is already .write
    try std.testing.expectEqual(Checkout.Job.write, co.job);
    try std.testing.expectEqual(@as(?*Checkout, null), co.queue_next);
    try std.testing.expectEqual(@as(u64, 4), s.resident_bytes);
    try s.start();
    try std.testing.expectError(error.AlreadyRunning, s.start());
    s.stop();
}
```

- [ ] **Step 2: Re-export and run**

In `core/src/root.zig` after the `Checkout` line: `pub const Scratch = @import("Scratch.zig");`

Run: `zig build --build-file core/build.zig test --summary all`
Expected: `187/187` (181 + 6). If the "q.wav" test's write fails because the cwd has no such directory — that is fine: the test only checks state after `stop`, and `failed` is acceptable there; the assertion is on `job`/`queue_next`/`resident_bytes` before `start`.

If `Dir.rename` with a cwd-relative `.zig-cache/...` path fails on Windows with `error.FileNotFound`, check that both arguments are given as sub-paths of `Dir.cwd()` (they are) and that the `.part` file was created by the writer (`Recorder.write` writes it). Do not change to absolute paths in the test; the Python tests in Task h6 cover absolute paths.

- [ ] **Step 3: Mutation checks**

(a) In `run` change the wait predicate to `while (self.queue_head == null)` (ignore stop). Expected: every test that calls `stop` hangs — abort with Ctrl-C after 10 s; that IS the finding. Revert.
(b) In `run` return immediately when `stop_flag` is set (before popping). Expected: the FIFO/drain test reddens (count 0 or < 3). Revert.
(c) In `doWrite` skip the `rename`. Expected: the first test reddens (`a.wav.part` exists, `wav.open(co.path())` FileNotFound). Revert.
(d) In `submitLocked` append at the head instead of the tail. Expected: the FIFO test reddens ("321"). Revert.

- [ ] **Step 4: fmt + commit**

```bash
zig fmt core/src/Scratch.zig core/src/root.zig
git add core/src/Scratch.zig core/src/root.zig
git commit -m "feat(core): Scratch.zig — writer thread, intrusive FIFO, .part rename, load jobs"
```

### Task h4: `Scratch` cache — LRU, pin, touch, budget, forget

**Files:**
- Modify: `core/src/Scratch.zig`

**Interfaces:**
- Produces: `pin(self, co, on: bool) void` (on = pin + preload when not resident), `touch(self, co) void`, `setBudget(self, bytes: u64) void`, `forget(self, co) void` (waits for `co`'s job, unlinks, subtracts; the caller then destroys), `residentBytes(self) u64`.

- [ ] **Step 1: Failing tests (append to `core/src/Scratch.zig`)**

```zig
/// Two written roots in a tmp dir, both resident, no thread running.
const Pair = struct {
    tmp: std.testing.TmpDir,
    ring: Ring,
    a: *Checkout,
    b: *Checkout,
    pa: [64]u8 = undefined,
    pb: [64]u8 = undefined,

    fn init(self: *Pair) !void {
        self.tmp = std.testing.tmpDir(.{});
        self.ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
        var in: [10]f32 = undefined;
        for (&in, 0..) |*s, i| s.* = @floatFromInt(i);
        self.ring.write(&in);
        self.a = try Checkout.createFromRing(std.testing.allocator, &self.ring, 0, 4, tmpPath(&self.pa, &self.tmp, "a.wav"));
        self.b = try Checkout.createFromRing(std.testing.allocator, &self.ring, 4, 10, tmpPath(&self.pb, &self.tmp, "b.wav"));
    }
    fn writeBoth(self: *Pair, s: *Scratch) !void {
        try s.start();
        s.submit(self.a, .write);
        s.submit(self.b, .write);
        s.stop();
    }
    fn deinit(self: *Pair) void {
        self.a.destroy();
        self.b.destroy();
        self.ring.deinit();
        self.tmp.cleanup();
    }
};

test "budget 0 drops every written root after its write; bytes read 0" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(0);
    try p.writeBoth(&s);
    try std.testing.expectEqual(@as(?[]f32, null), p.a.frames);
    try std.testing.expectEqual(@as(?[]f32, null), p.b.frames);
    try std.testing.expectEqual(@as(u64, 0), s.residentBytes());
}

test "eviction is LRU: touch moves to the head; the tail goes first" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(1 << 30);
    try p.writeBoth(&s); // both resident: a (16 B) then b (24 B) at the head
    s.touch(p.a); // a is now most recent
    s.setBudget(20); // 40 > 20: evict the tail = b
    try std.testing.expect(p.a.frames != null);
    try std.testing.expectEqual(@as(?[]f32, null), p.b.frames);
    try std.testing.expectEqual(@as(u64, 16), s.residentBytes());
}

test "a pinned checkout survives budget 0; unpin evicts it" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(1 << 30);
    try p.writeBoth(&s);
    s.pin(p.a, true);
    s.setBudget(0);
    try std.testing.expect(p.a.frames != null);
    try std.testing.expectEqual(@as(?[]f32, null), p.b.frames);
    s.pin(p.a, false);
    try std.testing.expectEqual(@as(?[]f32, null), p.a.frames);
    try std.testing.expectEqual(@as(u64, 0), s.residentBytes());
}

test "a checkout that is not yet written is never evicted" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(0);
    // no thread: a and b stay .queued
    s.submit(p.a, .write);
    s.setBudget(0);
    try std.testing.expect(p.a.frames != null);
    try std.testing.expectEqual(@as(u64, 16), s.residentBytes());
}

test "pin on an evicted checkout preloads it (budget 0 keeps it while pinned)" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(0);
    try p.writeBoth(&s); // both evicted
    try s.start();
    s.pin(p.b, true);
    s.waitLoad(p.b);
    s.stop();
    try std.testing.expectEqualSlices(f32, &[_]f32{ 4, 5, 6, 7, 8, 9 }, p.b.frames.?);
    try std.testing.expectEqual(@as(u64, 24), s.residentBytes());
}

test "forget unlinks and stops counting; destroy afterwards is clean" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(1 << 30);
    try p.writeBoth(&s);
    s.forget(p.a);
    try std.testing.expectEqual(@as(u64, 24), s.residentBytes());
    try std.testing.expectEqual(p.b, s.lru_head.?);
    try std.testing.expectEqual(p.b, s.lru_tail.?);
    s.forget(p.b);
    try std.testing.expectEqual(@as(u64, 0), s.residentBytes());
    try std.testing.expectEqual(@as(?*Checkout, null), s.lru_head);
}
```

- [ ] **Step 2: Run to see them fail**

Expected: compile errors for `touch`, `setBudget`, `pin`, `forget`, `residentBytes`.

- [ ] **Step 3: Implement (replace the `evictOverBudgetLocked` stub and add the public API)**

```zig
/// Pin = "the UI is looking at this one". Pinned entries are never
/// evicted, and pinning an evicted checkout queues its preload so PLAY
/// finds it resident. Unpinning re-checks the budget at once.
pub fn pin(self: *Scratch, co: *Checkout, on: bool) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    co.pinned = on;
    if (on) {
        if (co.frames == null) self.submitLocked(co, .load);
    } else {
        self.evictOverBudgetLocked();
    }
}

/// Record a use: move to the LRU head, then trim to budget.
pub fn touch(self: *Scratch, co: *Checkout) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    if (co.frames != null) {
        self.lruRemoveLocked(co);
        self.lruInsertHeadLocked(co);
    }
    self.evictOverBudgetLocked();
}

pub fn setBudget(self: *Scratch, bytes: u64) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    self.budget_bytes = bytes;
    self.evictOverBudgetLocked();
}

/// Take `co` out of the cache before the caller destroys it. Waits for
/// any job on it first (a write in flight must finish before its frames
/// are freed).
pub fn forget(self: *Scratch, co: *Checkout) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    while (co.job != .none) self.cond.waitUncancelable(io, &self.mutex);
    self.lruRemoveLocked(co);
}

pub fn residentBytes(self: *Scratch) u64 {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    return self.resident_bytes;
}

/// Walk from the LRU tail while over budget. Skips pinned entries,
/// entries with a job in flight, and entries whose audio is not yet safe
/// on disk (`queued`/`writing`/`failed`) — evicting those would lose the
/// only copy.
fn evictOverBudgetLocked(self: *Scratch) void {
    var cur = self.lru_tail;
    while (self.resident_bytes > self.budget_bytes) {
        const co = cur orelse return;
        cur = co.lru_prev;
        if (co.pinned or co.job != .none) continue;
        const ws = co.write_state.load(.acquire);
        if (ws != .written and ws != .adopted) continue;
        self.lruRemoveLocked(co);
        co.evict();
    }
}
```

- [ ] **Step 4: Run**

Expected: `193/193` (187 + 6).

- [ ] **Step 5: Mutation checks (one per clause of the eviction guard)**

(a) Remove `co.pinned or` → "pinned survives" reddens. Revert.
(b) Remove the `ws != .written and ws != .adopted` check → "not yet written is never evicted" reddens. Revert.
(c) In `touch` skip the move-to-head → the LRU test reddens (a evicted instead of b). Revert.
(d) In `forget` skip `lruRemoveLocked` → the forget test reddens (24 ≠ 40 after the first forget... expected 24, would read 40). Revert.

- [ ] **Step 6: fmt + commit**

```bash
zig fmt core/src/Scratch.zig
git add core/src/Scratch.zig
git commit -m "feat(core): Scratch cache — LRU under a byte budget, pin + preload, forget"
```

### Task h5: Concurrency proof — a Python-side `fb_wav_write` and the writer serialise

**Files:**
- Modify: `core/src/Scratch.zig` (tests only)

This task pins the spec's "Risks" item about `global_single_threaded` from a second thread: two writers through `wav.write_mutex` must serialise, not deadlock, and both files must be intact.

- [ ] **Step 1: Failing test (append to `core/src/Scratch.zig`)**

```zig
test "the writer thread and a control-thread wav.writeFile serialise through wav.write_mutex" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    const n: usize = 40_000; // stereo frames: 320 KB, several chunk iterations
    var in: [n * 2]f32 = undefined;
    for (&in, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i % 1000)) / 1000.0;
    ring.write(&in);
    var s = Scratch.init(1 << 30);
    var pa: [64]u8 = undefined;
    var pb: [64]u8 = undefined;
    const co = try Checkout.createFromRing(std.testing.allocator, &ring, 0, n, tmpPath(&pa, &tmp, "w.wav"));
    defer co.destroy();
    try s.start();
    s.submit(co, .write);
    // Race a second writer from this thread through the same mutex.
    const other = tmpPath(&pb, &tmp, "c.wav");
    {
        wav.write_mutex.lockUncancelable(wav.io);
        defer wav.write_mutex.unlock(wav.io);
        try wav.writeFile(other, in[0 .. n * 2], 48_000, 2, .float32);
    }
    s.waitJob(co);
    s.stop();
    try std.testing.expectEqual(Checkout.WriteState.written, co.write_state.load(.acquire));
    inline for (.{ "w.wav", "c.wav" }) |name| {
        var pp: [64]u8 = undefined;
        var o = try wav.open(tmpPath(&pp, &tmp, name));
        defer o.file.close(wav.io);
        try std.testing.expectEqual(@as(u64, n), o.info.frames);
        var tail: [4]f32 = undefined;
        try wav.readFrames(o.file, o.info, n - 2, &tail);
        try std.testing.expectEqualSlices(f32, in[n * 2 - 4 ..], &tail);
    }
}
```

Also add two `Checkout.zig` tests that pin `residentBytes` after `evict` + `load` round trips are leak-free under `std.testing.allocator` (the allocator reports leaks as test failures):

```zig
test "load after evict allocates exactly once more (no leak under testing.allocator)" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3, 4 };
    const path = tmpPath(&pb, &tmp, "e.wav");
    try wav.writeFile(path, &in, 8_000, 2, .float32);
    const co = try adopt(std.testing.allocator, path, 0, 2, 8_000, 2);
    defer co.destroy();
    try co.load();
    co.evict();
    try co.load();
    try std.testing.expectEqual(@as(u64, 16), co.residentBytes());
}

test "destroy frees resident frames (leak-checked)" {
    const co = try adopt(std.testing.allocator, "d.wav", 0, 1, 8_000, 1);
    co.frames = try std.testing.allocator.alloc(f32, 1);
    co.destroy();
}
```

- [ ] **Step 2: Run**

Expected: `196/196` (193 + 3). A hang here (> 10 s) is the finding the spec asked for: record it on the sub-issue before touching anything.

- [ ] **Step 3: Commit**

```bash
zig fmt core/src/Scratch.zig core/src/Checkout.zig
git add core/src/Scratch.zig core/src/Checkout.zig
git commit -m "test(core): writer thread + control-thread writeFile serialise; leak checks"
```

### Task h6: ABI + header + `native.NativeScratch`

**Files:**
- Modify: `core/src/abi.zig` (append after the playback exports)
- Modify: `core/include/flashback_core.h`
- Modify: `flashback_sampler/core/native.py`
- Create: `tests/unit/test_scratch.py`

**Interfaces (C):**

```c
typedef struct FbScratch FbScratch;   /* opaque */
typedef struct FbCheckout FbCheckout; /* opaque */
typedef struct FbCheckoutInfo { uint32_t rate; uint16_t channels; uint8_t write_state; uint64_t n_frames; uint64_t start_frame; uint64_t resident_bytes; } FbCheckoutInfo;
FbScratch  *fb_scratch_create(uint64_t budget_bytes, FbStatus *status);
FbStatus    fb_scratch_start(FbScratch *);
void        fb_scratch_stop(FbScratch *);
void        fb_scratch_destroy(FbScratch *);            /* stops first */
void        fb_scratch_set_budget(FbScratch *, uint64_t bytes);
uint64_t    fb_scratch_resident_bytes(FbScratch *);
FbCheckout *fb_checkout_create(FbScratch *, FbRing *, uint64_t abs_start, uint64_t abs_end, const char *path, FbStatus *status);
FbCheckout *fb_checkout_slice(FbScratch *, const FbCheckout *parent, uint64_t start, uint64_t n, FbStatus *status);
FbCheckout *fb_checkout_open(FbScratch *, const char *path, uint64_t start_frame, uint64_t n_frames, FbStatus *status);
void        fb_checkout_info(FbScratch *, const FbCheckout *, FbCheckoutInfo *out);
FbStatus    fb_checkout_peak_bins(FbScratch *, FbCheckout *, size_t n_bins, FbPeakBin *out);
void        fb_checkout_pin(FbScratch *, FbCheckout *, uint8_t on);
FbStatus    fb_checkout_export(FbScratch *, FbCheckout *, const char *dst, uint64_t start, uint64_t n, FbSubtype);
void        fb_checkout_destroy(FbScratch *, FbCheckout *);
FbStatus    fb_playback_bind_checkout(FbPlayback *, FbScratch *, FbCheckout *, uint64_t start, uint64_t n);
```

**Interfaces (Python):** `native.FbCheckoutInfo`, `native.WRITE_STATES = {0: "queued", 1: "writing", 2: "written", 3: "failed", 4: "adopted"}`, `class NativeScratch` with `__init__(budget_bytes: int)`, `start()`, `stop()`, `close()`, `set_budget(bytes)`, `resident_bytes` (property), `checkout_create(ring: NativeAudioCircularBuffer, abs_start, abs_end, path) -> int`, `checkout_slice(parent: int, start, n) -> int`, `checkout_open(path, start_frame, n_frames) -> int`, `checkout_info(h) -> FbCheckoutInfo`, `checkout_peak_bins(h, n_bins) -> np.ndarray (n_bins, 2, channels)`, `checkout_pin(h, on: bool)`, `checkout_export(h, dst, start, n, subtype: str)`, `checkout_destroy(h)`, `handle` (property, the raw pointer). `NativeAudioCircularBuffer.handle` property (returns `self._h`). `NativeScrubPlayer.bind_checkout(scratch: NativeScratch, h: int, start: int, n: int, sample_rate: int, channels: int)`.

- [ ] **Step 1: Failing Python tests — `tests/unit/test_scratch.py`**

```python
"""NativeScratch + checkout handles over the real library: create from a
ring, background write, adoption, slice, export, peak bins, pin/evict."""
from __future__ import annotations

import time

import numpy as np
import pytest

from flashback_sampler.core import native
from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch
from tests.fixtures.wavread import read_wav


def _wait_state(scratch, h, want: str, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if native.WRITE_STATES[scratch.checkout_info(h).write_state] == want:
            return
        time.sleep(0.005)
    raise AssertionError(f"checkout never reached {want}")


@pytest.fixture
def ring():
    buf = NativeAudioCircularBuffer(duration_seconds=2.0, sample_rate=1000, channels=2)
    audio = np.zeros((1500, 2), dtype=np.float32)
    audio[:, 0] = np.arange(1500, dtype=np.float32)
    audio[:, 1] = -audio[:, 0]
    buf.write(audio)
    yield buf
    buf.close()


@pytest.fixture
def scratch():
    s = NativeScratch(budget_bytes=1 << 30)
    s.start()
    yield s
    s.close()


def test_create_writes_the_span_to_disk_and_reports_written(ring, scratch, tmp_path):
    p = tmp_path / "a.wav"
    h = scratch.checkout_create(ring, 100, 110, p)
    info = scratch.checkout_info(h)
    assert (info.rate, info.channels, info.n_frames, info.start_frame) == (1000, 2, 10, 0)
    assert info.resident_bytes == 10 * 2 * 4
    _wait_state(scratch, h, "written")
    assert p.exists() and not p.with_suffix(".wav.part").exists()
    audio, wi = read_wav(p)
    assert wi.frames == 10
    assert audio[0, 0] == 100.0 and audio[9, 1] == -109.0
    scratch.checkout_destroy(h)


def test_create_rejects_a_lapped_or_inverted_span(ring, scratch, tmp_path):
    with pytest.raises(RuntimeError, match="out_of_range|overwritten"):
        scratch.checkout_create(ring, 1400, 1600, tmp_path / "x.wav")
    with pytest.raises(ValueError):
        scratch.checkout_create(ring, 10, 10, tmp_path / "y.wav")


def test_budget_zero_evicts_after_write_and_peak_bins_still_work(ring, scratch, tmp_path):
    scratch.set_budget(0)
    h = scratch.checkout_create(ring, 0, 1000, tmp_path / "b.wav")
    before = scratch.checkout_peak_bins(h, 10)
    _wait_state(scratch, h, "written")
    scratch.checkout_pin(h, False)  # any touch trims to budget
    assert scratch.checkout_info(h).resident_bytes == 0
    after = scratch.checkout_peak_bins(h, 10)  # streamed from the file
    np.testing.assert_array_equal(before, after)
    assert after.shape == (10, 2, 2)
    assert after[0, 0, 0] == 0.0 and after[0, 1, 0] == 99.0
    scratch.checkout_destroy(h)


def test_pin_preloads_and_keeps_resident(ring, scratch, tmp_path):
    scratch.set_budget(0)
    h = scratch.checkout_create(ring, 0, 100, tmp_path / "c.wav")
    _wait_state(scratch, h, "written")
    scratch.checkout_pin(h, False)
    assert scratch.checkout_info(h).resident_bytes == 0
    scratch.checkout_pin(h, True)
    t0 = time.monotonic()
    while scratch.checkout_info(h).resident_bytes == 0 and time.monotonic() - t0 < 5:
        time.sleep(0.005)
    assert scratch.checkout_info(h).resident_bytes == 100 * 2 * 4
    assert scratch.resident_bytes == 800
    scratch.checkout_destroy(h)
    assert scratch.resident_bytes == 0


def test_open_adopts_a_file_and_slice_references_it(ring, scratch, tmp_path):
    p = tmp_path / "d.wav"
    h = scratch.checkout_create(ring, 200, 300, p)
    _wait_state(scratch, h, "written")
    scratch.checkout_destroy(h)
    a = scratch.checkout_open(p, 0, 100)
    assert native.WRITE_STATES[scratch.checkout_info(a).write_state] == "adopted"
    s = scratch.checkout_slice(a, 10, 20)
    si = scratch.checkout_info(s)
    assert (si.start_frame, si.n_frames) == (10, 20)
    bins = scratch.checkout_peak_bins(s, 2)
    assert bins[0, 0, 0] == 210.0  # frames 10..20 of the file = ring 210..220
    with pytest.raises(ValueError):
        scratch.checkout_slice(a, 95, 10)
    scratch.checkout_destroy(s)
    scratch.checkout_destroy(a)


def test_open_clamps_to_the_file_and_rejects_a_start_past_it(ring, scratch, tmp_path):
    p = tmp_path / "e.wav"
    h = scratch.checkout_create(ring, 0, 50, p)
    _wait_state(scratch, h, "written")
    scratch.checkout_destroy(h)
    a = scratch.checkout_open(p, 40, 1000)  # asks for more than the file holds
    assert scratch.checkout_info(a).n_frames == 10
    scratch.checkout_destroy(a)
    with pytest.raises(ValueError):
        scratch.checkout_open(p, 50, 1)
    with pytest.raises(FileNotFoundError):
        scratch.checkout_open(tmp_path / "nope.wav", 0, 1)


def test_export_from_ram_and_from_file_agree(ring, scratch, tmp_path):
    p = tmp_path / "f.wav"
    h = scratch.checkout_create(ring, 0, 300, p)
    out_ram = tmp_path / "ram.wav"
    scratch.checkout_export(h, out_ram, 100, 50, "PCM_16")  # before the write lands: from frames
    _wait_state(scratch, h, "written")
    scratch.set_budget(0)
    scratch.checkout_pin(h, False)
    out_file = tmp_path / "file.wav"
    scratch.checkout_export(h, out_file, 100, 50, "PCM_16")  # evicted: from the file
    a, ai = read_wav(out_ram)
    b, bi = read_wav(out_file)
    assert ai.subtype == bi.subtype == "PCM_16" and ai.frames == bi.frames == 50
    np.testing.assert_array_equal(a, b)
    scratch.checkout_destroy(h)


def test_bind_checkout_plays_the_range(ring, scratch, tmp_path, monkeypatch):
    from tests.unit.test_scrub_player import _FakePlaybackLib
    # The real library binds; a fake player would not exercise Zig. Use
    # the real player only for bind (no device open happens at bind).
    from flashback_sampler.core.scrub_player import NativeScrubPlayer
    h = scratch.checkout_create(ring, 0, 100, tmp_path / "g.wav")
    player = NativeScrubPlayer(1000, 2)
    try:
        player.bind_checkout(scratch, h, 10, 20, 1000, 2)
        assert player.source_length_samples == 20
    finally:
        player.close()
        scratch.checkout_destroy(h)
```

- [ ] **Step 2: Run to see them fail**

Run: `python -m pytest tests/unit/test_scratch.py -q`
Expected: `ImportError: cannot import name 'NativeScratch'`.

- [ ] **Step 3: Zig exports (append to `core/src/abi.zig`)**

Add imports `const Checkout = @import("Checkout.zig");` and `const Scratch = @import("Scratch.zig");`.

```zig
pub const FbCheckoutInfo = extern struct { rate: u32, channels: u16, write_state: u8, n_frames: u64, start_frame: u64, resident_bytes: u64 };

fn checkoutStatus(e: anyerror) FbStatus {
    return switch (e) {
        error.InvalidArgument, error.PathTooLong, error.NotWave, error.MissingFmt, error.MissingData, error.Unsupported => .invalid_arg,
        error.OutOfMemory => .out_of_memory,
        error.Overwritten => .overwritten,
        error.OutOfRange => .out_of_range,
        else => .io_error,
    };
}

export fn fb_scratch_create(budget_bytes: u64, status: ?*FbStatus) ?*Scratch {
    const s = allocator.create(Scratch) catch {
        if (status) |st| st.* = .out_of_memory;
        return null;
    };
    s.* = Scratch.init(budget_bytes);
    if (status) |st| st.* = .ok;
    return s;
}

export fn fb_scratch_start(s: *Scratch) FbStatus {
    s.start() catch return .io_error;
    return .ok;
}

export fn fb_scratch_stop(s: *Scratch) void {
    s.stop();
}

/// Stops (drains) first. Every checkout must already be destroyed; the
/// Python host destroys them before the scratch (AppState.shutdown).
export fn fb_scratch_destroy(s: *Scratch) void {
    s.stop();
    allocator.destroy(s);
}

export fn fb_scratch_set_budget(s: *Scratch, bytes: u64) void {
    s.setBudget(bytes);
}

export fn fb_scratch_resident_bytes(s: *Scratch) u64 {
    return s.residentBytes();
}

/// Copy the span out of the ring (a root) and queue its write.
export fn fb_checkout_create(s: *Scratch, ring: *Ring, abs_start: u64, abs_end: u64, path: [*:0]const u8, status: ?*FbStatus) ?*Checkout {
    const co = Checkout.createFromRing(allocator, ring, abs_start, abs_end, std.mem.span(path)) catch |e| {
        if (status) |st| st.* = checkoutStatus(e);
        return null;
    };
    s.submit(co, .write);
    if (status) |st| st.* = .ok;
    return co;
}

export fn fb_checkout_slice(s: *Scratch, parent: *const Checkout, start: u64, n: u64, status: ?*FbStatus) ?*Checkout {
    _ = s;
    const co = Checkout.slice(allocator, parent, start, n) catch |e| {
        if (status) |st| st.* = checkoutStatus(e);
        return null;
    };
    if (status) |st| st.* = .ok;
    return co;
}

/// Adoption: rate/channels come from the file; `n_frames` is clamped to
/// what the file holds past `start_frame` (a `.part` reports its true
/// prefix). A start at or past the end is invalid_arg.
export fn fb_checkout_open(s: *Scratch, path: [*:0]const u8, start_frame: u64, n_frames: u64, status: ?*FbStatus) ?*Checkout {
    _ = s;
    const p = std.mem.span(path);
    var o = wav.open(p) catch |e| {
        if (status) |st| st.* = checkoutStatus(e);
        return null;
    };
    o.file.close(wav.io);
    if (start_frame >= o.info.frames or n_frames == 0) {
        if (status) |st| st.* = .invalid_arg;
        return null;
    }
    const n = @min(n_frames, o.info.frames - start_frame);
    const co = Checkout.adopt(allocator, p, start_frame, n, o.info.rate, o.info.channels) catch |e| {
        if (status) |st| st.* = checkoutStatus(e);
        return null;
    };
    if (status) |st| st.* = .ok;
    return co;
}

export fn fb_checkout_info(s: *Scratch, co: *const Checkout, out: *FbCheckoutInfo) void {
    // frames is a two-word optional slice; read it under the cache lock
    // so a load landing on the worker cannot tear it.
    s.mutex.lockUncancelable(wav.io);
    defer s.mutex.unlock(wav.io);
    out.* = .{
        .rate = co.rate,
        .channels = co.channels,
        .write_state = @intFromEnum(co.write_state.load(.acquire)),
        .n_frames = co.n_frames,
        .start_frame = co.start_frame,
        .resident_bytes = co.residentBytes(),
    };
}

export fn fb_checkout_peak_bins(s: *Scratch, co: *Checkout, n_bins: usize, out: [*]peaks.PeakBin) FbStatus {
    if (n_bins == 0) return .invalid_arg;
    s.waitLoad(co);
    s.touch(co);
    co.peakBins(n_bins, out[0 .. n_bins * co.channels]) catch |e| return checkoutStatus(e);
    return .ok;
}

export fn fb_checkout_pin(s: *Scratch, co: *Checkout, on: u8) void {
    s.pin(co, on != 0);
}

/// Materialise `[start, start + n)` of the checkout into `dst`. From the
/// file once the audio is safe on disk (written/adopted — no reload of
/// an evicted clip), from the RAM copy before that.
export fn fb_checkout_export(s: *Scratch, co: *Checkout, dst: [*:0]const u8, start: u64, n: u64, subtype: c_int) FbStatus {
    if (subtype < 0 or subtype > 2) return .invalid_arg;
    if (n == 0 or start + n > co.n_frames) return .invalid_arg;
    const st: wav.Subtype = @enumFromInt(@as(u8, @intCast(subtype)));
    s.waitLoad(co);
    s.touch(co);
    wav.write_mutex.lockUncancelable(wav.io);
    defer wav.write_mutex.unlock(wav.io);
    const ws = co.write_state.load(.acquire);
    if (ws == .written or ws == .adopted) {
        wav.copyRange(co.path(), std.mem.span(dst), co.start_frame + start, n, st) catch |e| return checkoutStatus(e);
        return .ok;
    }
    const frames = co.frames orelse return .io_error;
    const chans: u64 = co.channels;
    const a: usize = @intCast(start * chans);
    const b: usize = @intCast((start + n) * chans);
    wav.writeFile(std.mem.span(dst), frames[a..b], co.rate, co.channels, st) catch |e| return checkoutStatus(e);
    return .ok;
}

export fn fb_checkout_destroy(s: *Scratch, co: *Checkout) void {
    s.forget(co);
    co.destroy();
}

export fn fb_playback_bind_checkout(pb: *Playback, s: *Scratch, co: *Checkout, start: u64, n: u64) FbStatus {
    if (n == 0 or start + n > co.n_frames) return .invalid_arg;
    s.waitLoad(co);
    s.touch(co);
    pb.bind(co.source(start, n), co.rate, co.channels) catch |e| return checkoutStatus(e);
    return .ok;
}

test "fb_scratch / fb_checkout: create, written, info, destroy" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/abi-co.wav", .{tmp.sub_path}) catch unreachable;
    const ring = fb_ring_create(1000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    fb_ring_write(ring, &[_]f32{ 1, 2, 3, 4 }, 4);
    var st: FbStatus = .io_error;
    const s = fb_scratch_create(1 << 20, &st) orelse return error.CreateFailed;
    defer fb_scratch_destroy(s);
    try std.testing.expectEqual(FbStatus.ok, fb_scratch_start(s));
    const co = fb_checkout_create(s, ring, 1, 3, path, &st) orelse return error.CreateFailed;
    try std.testing.expectEqual(FbStatus.ok, st);
    s.waitJob(co);
    var info: FbCheckoutInfo = undefined;
    fb_checkout_info(s, co, &info);
    try std.testing.expectEqual(@as(u8, 2), info.write_state); // written
    try std.testing.expectEqual(@as(u64, 2), info.n_frames);
    try std.testing.expectEqual(@as(u64, 8), info.resident_bytes);
    fb_checkout_destroy(s, co);
    try std.testing.expectEqual(@as(u64, 0), fb_scratch_resident_bytes(s));
}

test "fb_checkout_create reports overwritten/out_of_range/invalid_arg distinctly" {
    const ring = fb_ring_create(1000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    fb_ring_write(ring, &[_]f32{ 1, 2, 3 }, 3);
    const s = fb_scratch_create(0, null) orelse return error.CreateFailed;
    defer fb_scratch_destroy(s);
    var st: FbStatus = .ok;
    try std.testing.expectEqual(@as(?*Checkout, null), fb_checkout_create(s, ring, 1, 9, "x.wav", &st));
    try std.testing.expectEqual(FbStatus.out_of_range, st);
    try std.testing.expectEqual(@as(?*Checkout, null), fb_checkout_create(s, ring, 2, 2, "x.wav", &st));
    try std.testing.expectEqual(FbStatus.invalid_arg, st);
}

test "fb_checkout_export rejects a span past the checkout" {
    const ring = fb_ring_create(1000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    fb_ring_write(ring, &[_]f32{ 1, 2, 3 }, 3);
    const s = fb_scratch_create(1 << 20, null) orelse return error.CreateFailed;
    defer fb_scratch_destroy(s);
    const co = fb_checkout_create(s, ring, 0, 3, "never.wav", null) orelse return error.CreateFailed;
    defer fb_checkout_destroy(s, co);
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_checkout_export(s, co, "out.wav", 2, 2, 0));
}
```

- [ ] **Step 4: Header** — append the C block from this task's "Interfaces (C)" to `core/include/flashback_core.h` after the playback section, with the comment: `/* Checkout persistence (epic #53). write_state: 0 queued, 1 writing, 2 written, 3 failed, 4 adopted. A checkout must be destroyed before its scratch. */`

- [ ] **Step 5: Python (`flashback_sampler/core/native.py`)**

Structure + constants next to `FbWavInfo`:

```python
class FbCheckoutInfo(C.Structure):
    _fields_ = [
        ("rate", C.c_uint32), ("channels", C.c_uint16), ("write_state", C.c_uint8),
        ("n_frames", C.c_uint64), ("start_frame", C.c_uint64), ("resident_bytes", C.c_uint64),
    ]


# Mirrors Checkout.WriteState in core/src/Checkout.zig.
WRITE_STATES = {0: "queued", 1: "writing", 2: "written", 3: "failed", 4: "adopted"}
```

Declarations in `_declare` (after the playback block):

```python
    u64, vp = C.c_uint64, C.c_void_p
    lib.fb_scratch_create.argtypes = [u64, C.POINTER(C.c_int)]
    lib.fb_scratch_create.restype = vp
    lib.fb_scratch_start.argtypes = [vp]
    lib.fb_scratch_start.restype = C.c_int
    lib.fb_scratch_stop.argtypes = [vp]
    lib.fb_scratch_stop.restype = None
    lib.fb_scratch_destroy.argtypes = [vp]
    lib.fb_scratch_destroy.restype = None
    lib.fb_scratch_set_budget.argtypes = [vp, u64]
    lib.fb_scratch_set_budget.restype = None
    lib.fb_scratch_resident_bytes.argtypes = [vp]
    lib.fb_scratch_resident_bytes.restype = u64
    lib.fb_checkout_create.argtypes = [vp, vp, u64, u64, C.c_char_p, C.POINTER(C.c_int)]
    lib.fb_checkout_create.restype = vp
    lib.fb_checkout_slice.argtypes = [vp, vp, u64, u64, C.POINTER(C.c_int)]
    lib.fb_checkout_slice.restype = vp
    lib.fb_checkout_open.argtypes = [vp, C.c_char_p, u64, u64, C.POINTER(C.c_int)]
    lib.fb_checkout_open.restype = vp
    lib.fb_checkout_info.argtypes = [vp, vp, C.POINTER(FbCheckoutInfo)]
    lib.fb_checkout_info.restype = None
    lib.fb_checkout_peak_bins.argtypes = [vp, vp, C.c_size_t, C.POINTER(FbPeakBin)]
    lib.fb_checkout_peak_bins.restype = C.c_int
    lib.fb_checkout_pin.argtypes = [vp, vp, C.c_uint8]
    lib.fb_checkout_pin.restype = None
    lib.fb_checkout_export.argtypes = [vp, vp, C.c_char_p, u64, u64, C.c_int]
    lib.fb_checkout_export.restype = C.c_int
    lib.fb_checkout_destroy.argtypes = [vp, vp]
    lib.fb_checkout_destroy.restype = None
    lib.fb_playback_bind_checkout.argtypes = [vp, vp, vp, u64, u64]
    lib.fb_playback_bind_checkout.restype = C.c_int
```

Add to `NativeAudioCircularBuffer`:

```python
    @property
    def handle(self):
        """The raw Zig pointer, for calls that take the ring as an argument."""
        return self._h
```

After `NativeAudioCircularBuffer`:

```python
def _status_raise(status: int, what: str) -> None:
    """One FbStatus → exception rule for the checkout calls."""
    if status == _OK:
        return
    if status == _INVALID_ARG:
        raise ValueError(f"{what}: invalid_arg")
    if status == _OUT_OF_RANGE:
        raise RuntimeError(f"{what}: out_of_range (span not written yet)")
    if status == _OVERWRITTEN:
        raise RuntimeError(f"{what}: overwritten (the ring lapped the span)")
    if status == _OUT_OF_MEMORY:
        raise MemoryError(f"{what}: out_of_memory")
    if status == _IO_ERROR:
        raise FileNotFoundError(f"{what}: io_error")
    raise RuntimeError(f"{what}: status {status}")


class NativeScratch:
    """The process-wide scratch writer + RAM cache: a handle on a Zig
    `Scratch`. Checkout handles are plain ints (Zig pointers); the
    CheckoutManager owns their lifetime and destroys every one before
    `close()`."""

    def __init__(self, budget_bytes: int):
        self._lib = _require_lib()
        status = C.c_int(_OK)
        self._h = self._lib.fb_scratch_create(int(budget_bytes), C.byref(status))
        if not self._h:
            _status_raise(status.value, "fb_scratch_create")
        self._running = False

    @property
    def handle(self):
        return self._h

    def start(self) -> None:
        _status_raise(self._lib.fb_scratch_start(self._h), "fb_scratch_start")
        self._running = True

    def stop(self) -> None:
        if self._h:
            self._lib.fb_scratch_stop(self._h)
        self._running = False

    def close(self) -> None:
        if self._h:
            self._lib.fb_scratch_destroy(self._h)
            self._h = None

    def set_budget(self, budget_bytes: int) -> None:
        self._lib.fb_scratch_set_budget(self._h, int(budget_bytes))

    @property
    def resident_bytes(self) -> int:
        return int(self._lib.fb_scratch_resident_bytes(self._h))

    # -- checkouts ------------------------------------------------------
    def checkout_create(self, ring: NativeAudioCircularBuffer, abs_start: int, abs_end: int, path) -> int:
        status = C.c_int(_OK)
        h = self._lib.fb_checkout_create(self._h, ring.handle, int(abs_start), int(abs_end), str(path).encode("utf-8"), C.byref(status))
        if not h:
            _status_raise(status.value, "fb_checkout_create")
        return h

    def checkout_slice(self, parent: int, start: int, n: int) -> int:
        status = C.c_int(_OK)
        h = self._lib.fb_checkout_slice(self._h, parent, int(start), int(n), C.byref(status))
        if not h:
            _status_raise(status.value, "fb_checkout_slice")
        return h

    def checkout_open(self, path, start_frame: int, n_frames: int) -> int:
        status = C.c_int(_OK)
        h = self._lib.fb_checkout_open(self._h, str(path).encode("utf-8"), int(start_frame), int(n_frames), C.byref(status))
        if not h:
            _status_raise(status.value, f"fb_checkout_open {path}")
        return h

    def checkout_info(self, h: int) -> FbCheckoutInfo:
        info = FbCheckoutInfo()
        self._lib.fb_checkout_info(self._h, h, C.byref(info))
        return info

    def checkout_peak_bins(self, h: int, n_bins: int) -> np.ndarray:
        """(n_bins, 2, channels) float32 — the get_peak_bins layout."""
        channels = int(self.checkout_info(h).channels)
        out = np.zeros((int(n_bins), channels, 2), dtype=np.float32)
        _status_raise(self._lib.fb_checkout_peak_bins(self._h, h, int(n_bins), out.ctypes.data_as(C.POINTER(FbPeakBin))), "fb_checkout_peak_bins")
        return np.ascontiguousarray(out.transpose(0, 2, 1))

    def checkout_pin(self, h: int, on: bool) -> None:
        self._lib.fb_checkout_pin(self._h, h, 1 if on else 0)

    def checkout_export(self, h: int, dst, start: int, n: int, subtype: str) -> None:
        _status_raise(self._lib.fb_checkout_export(self._h, h, str(dst).encode("utf-8"), int(start), int(n), SUBTYPE_INTS[subtype]), "fb_checkout_export")

    def checkout_destroy(self, h: int) -> None:
        self._lib.fb_checkout_destroy(self._h, h)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
```

`flashback_sampler/core/scrub_player.py`, after `bind`:

```python
    def bind_checkout(self, scratch, h: int, start: int, n: int, sample_rate: int, channels: int) -> None:
        """Bind `[start, start + n)` of a checkout handle: Zig copies from
        the checkout's RAM copy or reads its file — no numpy round trip."""
        if not self._handle():
            return
        status = self._lib.fb_playback_bind_checkout(self._h, scratch.handle, h, int(start), int(n))
        if status == native._INVALID_ARG:
            raise ValueError(f"fb_playback_bind_checkout rejected span {start}+{n}")
        if status == native._OUT_OF_MEMORY:
            raise MemoryError("fb_playback_bind_checkout: could not allocate the clip")
        if status != native._OK:
            raise RuntimeError(f"fb_playback_bind_checkout failed with status {status}")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
```

- [ ] **Step 6: Build + run**

```bash
zig build --build-file core/build.zig -Doptimize=ReleaseSafe
zig build --build-file core/build.zig test --summary all
python -m pytest tests/unit/test_scratch.py tests/unit/test_scrub_player.py -q
```

Expected: `199/199`; `8 passed` in `test_scratch.py`. `test_bind_checkout_plays_the_range` opens no device (bind never opens a stream), so it runs without hardware.

- [ ] **Step 7: Mutation check through the ABI**

In `fb_checkout_export` swap the `.written/.adopted` branch to always use `frames`. Expected: `test_export_from_ram_and_from_file_agree` reddens with `io_error` (frames are null after eviction). Revert.

- [ ] **Step 8: fmt + commit**

```bash
zig fmt core/src/abi.zig
git add core/src/abi.zig core/include/flashback_core.h flashback_sampler/core/native.py flashback_sampler/core/scrub_player.py tests/unit/test_scratch.py
git commit -m "feat: fb_scratch_* / fb_checkout_* ABI, NativeScratch, bind_checkout"
```

### Task h7: `manifest.py` + `config.py` scratch prefs + test conftest

**Files:**
- Create: `flashback_sampler/core/manifest.py`
- Modify: `flashback_sampler/app/config.py` (append)
- Create: `tests/unit/conftest.py`
- Create: `tests/unit/test_manifest.py`
- Modify: `tests/unit/test_config.py` (append)

**Interfaces:**
- Produces: `manifest.Manifest` dataclass (`id, slot, rate, channels, abs_start, abs_end, created_at, parent, start_frame, n_frames, trim_in, trim_out, state, partial, bins`), `manifest.manifest_path(dir, id) -> Path`, `manifest.write_manifest(dir, m) -> Path` (atomic), `manifest.read_manifest(path) -> Manifest | None`, `manifest.scan(dir) -> list[Manifest]` (by `created_at`, roots before slices), `manifest.resolve_audio(dir, m) -> tuple[Path, bool] | None`, `manifest.bins_to_json(bins: dict[str, np.ndarray]) -> dict`, `manifest.bins_from_json(d, channels) -> dict[str, np.ndarray]`; `config.default_scratch_dir()`, `config.load_scratch_dir()`, `config.save_scratch_dir(p)`, `config.DEFAULT_CHECKOUT_CACHE_MB`, `config.load_checkout_cache_mb()`, `config.save_checkout_cache_mb(mb)`.

- [ ] **Step 1: Failing tests — `tests/unit/test_manifest.py`**

```python
from __future__ import annotations

import json

import numpy as np

from flashback_sampler.core.manifest import (
    Manifest, bins_from_json, bins_to_json, manifest_path, read_manifest,
    resolve_audio, scan, write_manifest,
)


def _m(**kw) -> Manifest:
    base = dict(id="abc123", slot="Main", rate=48_000, channels=2, abs_start=10, abs_end=110,
                created_at=1.0, parent=None, start_frame=0, n_frames=100, trim_in=0, trim_out=0,
                state="pending", partial=False, bins=None)
    base.update(kw)
    return Manifest(**base)


def test_write_then_read_round_trips_including_bins(tmp_path):
    bins = {"540": np.arange(540 * 2 * 2, dtype=np.float32).reshape(540, 2, 2) / 7.0}
    m = _m(bins=bins_to_json(bins))
    p = write_manifest(tmp_path, m)
    assert p == manifest_path(tmp_path, "abc123") == tmp_path / "abc123.json"
    back = read_manifest(p)
    assert back is not None
    assert back.id == "abc123" and back.n_frames == 100 and back.parent is None
    got = bins_from_json(back.bins, channels=2)
    np.testing.assert_array_equal(got["540"], bins["540"])


def test_write_is_atomic_and_leaves_no_tmp(tmp_path):
    write_manifest(tmp_path, _m())
    assert list(tmp_path.glob("*.tmp")) == []
    assert json.loads((tmp_path / "abc123.json").read_text())["id"] == "abc123"


def test_read_corrupt_or_wrong_shape_returns_none(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert read_manifest(p) is None
    p.write_text(json.dumps({"id": "x"}))  # missing fields
    assert read_manifest(p) is None
    p.write_text(json.dumps([1, 2]))
    assert read_manifest(p) is None


def test_scan_orders_roots_before_slices_and_by_created_at(tmp_path):
    write_manifest(tmp_path, _m(id="s1", parent="r2", created_at=0.5))
    write_manifest(tmp_path, _m(id="r2", created_at=2.0))
    write_manifest(tmp_path, _m(id="r1", created_at=1.0))
    (tmp_path / "junk.json").write_text("nope")
    (tmp_path / "other.txt").write_text("x")
    assert [m.id for m in scan(tmp_path)] == ["r1", "r2", "s1"]


def test_scan_on_a_missing_dir_is_empty(tmp_path):
    assert scan(tmp_path / "absent") == []


def test_resolve_audio_prefers_wav_then_adopts_part(tmp_path):
    m = _m(id="r1")
    assert resolve_audio(tmp_path, m) is None
    (tmp_path / "r1.wav.part").write_bytes(b"RIFF")
    got = resolve_audio(tmp_path, m)
    assert got == (tmp_path / "r1.wav", True)
    assert (tmp_path / "r1.wav").exists() and not (tmp_path / "r1.wav.part").exists()
    assert resolve_audio(tmp_path, m) == (tmp_path / "r1.wav", False)


def test_resolve_audio_keeps_wav_when_both_exist(tmp_path):
    m = _m(id="r1")
    (tmp_path / "r1.wav").write_bytes(b"RIFF")
    (tmp_path / "r1.wav.part").write_bytes(b"RIFF")
    assert resolve_audio(tmp_path, m) == (tmp_path / "r1.wav", False)
    assert (tmp_path / "r1.wav.part").exists()  # left for the user; never deleted here
```

Append to `tests/unit/test_config.py`:

```python
def test_scratch_dir_defaults_under_user_cache_and_roundtrips(tmp_path):
    from flashback_sampler.app import config
    d = config.default_scratch_dir()
    assert d.name == "scratch"
    assert config.load_scratch_dir(tmp_path / "c.json") == d
    config.save_scratch_dir(tmp_path / "s", tmp_path / "c.json")
    assert config.load_scratch_dir(tmp_path / "c.json") == tmp_path / "s"


def test_checkout_cache_mb_roundtrip_and_floor(tmp_path):
    from flashback_sampler.app import config
    p = tmp_path / "c.json"
    assert config.load_checkout_cache_mb(p) == config.DEFAULT_CHECKOUT_CACHE_MB
    config.save_checkout_cache_mb(512, p)
    assert config.load_checkout_cache_mb(p) == 512.0
    config.save_checkout_cache_mb(-3, p)
    assert config.load_checkout_cache_mb(p) == 0.0
```

- [ ] **Step 2: Run to see them fail**

`python -m pytest tests/unit/test_manifest.py tests/unit/test_config.py -q` → `ModuleNotFoundError: flashback_sampler.core.manifest`.

- [ ] **Step 3: `flashback_sampler/core/manifest.py`**

```python
"""Per-checkout manifest: the JSON sidecar next to a scratch WAV.

The manifest is what adoption reads at launch: identity, provenance
(slot, absolute ring range), the file range this checkout covers, its
parent for a slice, trim, state, and the deck's peak bins (so a launch
with gigabytes of scratch draws the deck without reading audio).

Pure Python, no Qt, no engine calls. Bins travel as flat float lists in
the numpy layout (n_bins, 2, channels); `bins_to_json` / `bins_from_json`
convert.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

import numpy as np

PART_SUFFIX = ".wav.part"


@dataclass
class Manifest:
    id: str
    slot: str
    rate: int
    channels: int
    abs_start: int
    abs_end: int
    created_at: float
    parent: Optional[str]
    start_frame: int
    n_frames: int
    trim_in: int
    trim_out: int
    state: str
    partial: bool
    bins: Optional[dict]  # {"540": [floats], "360": [floats]} or None


_FIELDS = {f.name for f in fields(Manifest)}


def manifest_path(scratch_dir: Path | str, checkout_id: str) -> Path:
    return Path(scratch_dir) / f"{checkout_id}.json"


def audio_path(scratch_dir: Path | str, checkout_id: str) -> Path:
    return Path(scratch_dir) / f"{checkout_id}.wav"


def write_manifest(scratch_dir: Path | str, m: Manifest) -> Path:
    """Atomic: temp file + replace, so a crash mid-write leaves the old
    manifest (or none), never a torn one."""
    p = manifest_path(scratch_dir, m.id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(asdict(m), f)
    tmp.replace(p)
    return p


def read_manifest(path: Path | str) -> Optional[Manifest]:
    """None for anything that is not a complete manifest — adoption
    skips it and leaves the file in place."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != _FIELDS:
        return None
    try:
        return Manifest(**data)
    except TypeError:
        return None


def scan(scratch_dir: Path | str) -> list[Manifest]:
    """Every readable manifest, roots first (a slice needs its parent
    adopted already), then by creation time."""
    d = Path(scratch_dir)
    if not d.is_dir():
        return []
    found = [m for m in (read_manifest(p) for p in d.glob("*.json")) if m is not None]
    found.sort(key=lambda m: (m.parent is not None, m.created_at))
    return found


def resolve_audio(scratch_dir: Path | str, m: Manifest) -> Optional[tuple[Path, bool]]:
    """(path, partial) for a root's audio. `<id>.wav` wins; a lone
    `<id>.wav.part` (crash mid-write) is renamed into place and flagged
    partial — the reader clamps to what it holds. None when neither
    exists. Never deletes anything."""
    wav = audio_path(scratch_dir, m.id)
    if wav.exists():
        return wav, False
    part = Path(scratch_dir) / f"{m.id}{PART_SUFFIX}"
    if part.exists():
        part.rename(wav)
        return wav, True
    return None


def bins_to_json(bins: dict[str, np.ndarray]) -> dict:
    return {k: np.asarray(v, dtype=np.float32).ravel().tolist() for k, v in bins.items()}


def bins_from_json(d: Optional[dict], channels: int) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for k, flat in (d or {}).items():
        n_bins = int(k)
        arr = np.asarray(flat, dtype=np.float32)
        if arr.size != n_bins * 2 * channels:
            continue
        out[k] = arr.reshape(n_bins, 2, channels)
    return out
```

- [ ] **Step 4: `config.py` (append)**

```python
SCRATCH_DIR_KEY = "scratch_dir"
CHECKOUT_CACHE_MB_KEY = "checkout_cache_mb"
# Provisional until plan Task h11 records the select→playable measurement
# on #53 and replaces this number (0 = only pinned and in-flight clips
# stay resident; every written root drops to disk).
DEFAULT_CHECKOUT_CACHE_MB = 0.0


def default_scratch_dir() -> Path:
    """App-owned temp for scratch WAVs + manifests. Separate from the
    user-facing export pool: the app deletes here (on discard), never
    there."""
    import platformdirs

    return Path(platformdirs.user_cache_dir("flashback-sampler", appauthor=False)) / "scratch"


def load_scratch_dir(path: Path | None = None) -> Path:
    raw = get_pref(SCRATCH_DIR_KEY, "", path)
    return Path(raw) if raw else default_scratch_dir()


def save_scratch_dir(scratch_dir: Path | str, path: Path | None = None) -> None:
    set_pref(SCRATCH_DIR_KEY, str(scratch_dir), path)


def load_checkout_cache_mb(path: Path | None = None) -> float:
    try:
        return max(0.0, float(get_pref(CHECKOUT_CACHE_MB_KEY, DEFAULT_CHECKOUT_CACHE_MB, path)))
    except (TypeError, ValueError):
        return DEFAULT_CHECKOUT_CACHE_MB


def save_checkout_cache_mb(mb: float, path: Path | None = None) -> None:
    set_pref(CHECKOUT_CACHE_MB_KEY, max(0.0, float(mb)), path)
```

- [ ] **Step 5: `tests/unit/conftest.py`**

```python
"""Every AppState in the unit suite gets its own scratch dir under
tmp_path — never the user's real cache dir. state.py reads the pref
through the module attribute (`app_config.load_scratch_dir()`), which is
what makes this monkeypatch take."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_scratch_dir(tmp_path, monkeypatch):
    from flashback_sampler.app import config

    monkeypatch.setattr(config, "load_scratch_dir", lambda path=None: tmp_path / "scratch")
```

- [ ] **Step 6: Run**

`python -m pytest tests/unit/test_manifest.py tests/unit/test_config.py -q` → all pass (7 + 2 new).

- [ ] **Step 7: Commit**

```bash
git add flashback_sampler/core/manifest.py flashback_sampler/app/config.py tests/unit/conftest.py tests/unit/test_manifest.py tests/unit/test_config.py
git commit -m "feat: checkout manifests (JSON sidecar), scratch dir + cache prefs, isolated scratch in tests"
```

### Task h8: `checkout.py` over handles — `Checkout`, `CheckoutManager`, refcounts, manifests

**Files:**
- Rewrite: `flashback_sampler/core/checkout.py`
- Modify: `flashback_sampler/core/capture_slot.py:111-145` (`from_quality_preset` takes `scratch`, `scratch_dir`; `max_total_ram_mb` removed)
- Modify: `flashback_sampler/core/drag_export.py:50-70` (`render_drag_file` uses `trim_range`)
- Rewrite: `tests/unit/test_checkout.py`
- Modify: `tests/unit/test_capture_slot.py`, `tests/unit/test_drag_export.py`

**Interfaces:**
- Produces:

```python
@dataclass
class Checkout:
    id: str; handle: int; path: Path; created_at: float; sample_rate: int; channels: int
    n_frames: int; start_frame: int; abs_sample_start: int; abs_sample_end: int
    parent_id: Optional[str] = None; trim_in_samples: int = 0; trim_out_samples: int = 0
    state: CheckoutState = "pending"; partial: bool = False
    bins: dict[str, np.ndarray] = field(default_factory=dict)   # keys "540", "360"
    duration_seconds -> float
    trim_range() -> tuple[int, int]        # (start, n) within the checkout; full when untrimmed
    has_trim() -> bool

class CheckoutManager:
    def __init__(self, buffer, scratch: NativeScratch, scratch_dir: Path | str, slot_name: str = "", max_active_checkouts: int = 16)
    create(duration_s, anchor="latest", anchor_offset_s=0.0) -> Checkout
    create_from_abs_range(abs_start, abs_end) -> Checkout
    list() -> list[Checkout]; get(id) -> Checkout
    save(id, target_path, fmt="WAV", trimmed=True, subtype=None, mark_saved=True) -> Path
    export_range(id, target_path, start, n, subtype="FLOAT") -> Path
    mark_saved(id); discard(id); discard_all(); close()
    set_trim(id, trim_in, trim_out); pin(id | None)
    peak_bins(id, n_bins) -> np.ndarray; write_state(id) -> str; resident_bytes(id) -> int
    adopt_root(m: Manifest, audio: Path, partial: bool) -> Checkout
    adopt_slice(m: Manifest, parent: Checkout) -> Checkout
    file_refcount(path) -> int
```

- [ ] **Step 1: Rewrite `tests/unit/test_checkout.py`**

Keep every behaviour the old file pinned; read audio back through `native.wav_read` on the scratch file. The file starts:

```python
"""Checkout workflow over Zig handles: create = copy the span out of
the ring into Zig + queue the scratch write; Python holds ids, states,
trims, manifests and per-file refcounts. Audio is asserted by reading
the scratch file back through the Zig reader."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from flashback_sampler.core import native
from flashback_sampler.core.checkout import Checkout, CheckoutManager
from flashback_sampler.core.manifest import manifest_path, read_manifest
from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch
from tests.fixtures.sine_source import ramp_block
from tests.fixtures.wavread import read_wav


@pytest.fixture
def scratch():
    s = NativeScratch(budget_bytes=1 << 30)
    s.start()
    yield s
    s.close()


def _mgr(scratch, tmp_path, seconds=2.0, rate=1000, channels=1, frames=1500, **kw):
    buf = NativeAudioCircularBuffer(duration_seconds=seconds, sample_rate=rate, channels=channels)
    if frames:
        buf.write(ramp_block(0, frames, channels=channels))
    return CheckoutManager(buffer=buf, scratch=scratch, scratch_dir=tmp_path, slot_name="Main", **kw)


def _wait_written(mgr, co, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if mgr.write_state(co.id) == "written":
            return
        time.sleep(0.005)
    raise AssertionError("scratch write never landed")


def _audio(mgr, co) -> np.ndarray:
    _wait_written(mgr, co)
    return native.wav_read(co.path, co.start_frame, co.n_frames)


def test_create_checkout_snapshots_latest_n_seconds(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)  # last 500 samples
    assert isinstance(co, Checkout)
    assert (co.n_frames, co.start_frame, co.channels, co.sample_rate) == (500, 0, 1, 1000)
    assert co.state == "pending"
    assert (co.abs_sample_start, co.abs_sample_end) == (1000, 1500)
    audio = _audio(mgr, co)
    assert audio.shape == (500, 1)
    assert audio[0, 0] == pytest.approx(1000.0) and audio[-1, 0] == pytest.approx(1499.0)
    assert co.path == tmp_path / f"{co.id}.wav"


def test_create_writes_a_manifest_with_bins_before_the_audio_lands(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)
    m = read_manifest(manifest_path(tmp_path, co.id))
    assert m is not None and m.slot == "Main" and m.n_frames == 500 and m.parent is None
    assert set(m.bins) == {"540", "360"}
    assert co.bins["540"].shape == (540, 2, 1) and co.bins["360"].shape == (360, 2, 1)
    assert co.bins["360"][0, 1, 0] == pytest.approx(1001.0)  # max of the first bin (frames 1000,1001 → 1001)


def test_checkout_id_is_unique(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=1.0, frames=500)
    assert mgr.create(duration_s=0.2).id != mgr.create(duration_s=0.2).id


def test_list_returns_all_active_checkouts(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=1.0, frames=800)
    a = mgr.create(duration_s=0.2)
    b = mgr.create(duration_s=0.3)
    assert [c.id for c in mgr.list()] == [a.id, b.id]


def test_checkout_anchor_offset_pulls_earlier_range(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5, anchor_offset_s=0.5)  # ends 500 samples ago: 500..999
    assert (co.abs_sample_start, co.abs_sample_end) == (500, 1000)
    audio = _audio(mgr, co)
    assert audio[0, 0] == pytest.approx(500.0) and audio[-1, 0] == pytest.approx(999.0)


def test_checkout_anchor_offset_clamped_when_past_buffered(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=2.0, frames=800)  # only 0.8 s buffered
    co = mgr.create(duration_s=0.5, anchor_offset_s=5.0)
    # offset clamps to buffered - 1 sample; the span is whatever remains before it
    assert co.abs_sample_end == 1 and co.n_frames == 1


def test_checkout_duration_clamped_to_available(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=1.0, frames=300)
    co = mgr.create(duration_s=5.0)
    assert co.n_frames == 300 and co.abs_sample_start == 0


def test_checkout_anchor_offset_rejects_negative(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    with pytest.raises(ValueError):
        mgr.create(duration_s=0.1, anchor_offset_s=-1.0)
    with pytest.raises(ValueError):
        mgr.create(duration_s=0.0)


def test_create_from_abs_range_pulls_exact_samples(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create_from_abs_range(200, 260)
    audio = _audio(mgr, co)
    assert audio.shape == (60, 1) and audio[0, 0] == pytest.approx(200.0)


def test_create_from_abs_range_rejects_inverted_past_head_and_overwritten(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=1.0, frames=1500)  # capacity 1000; 500..1500 live
    with pytest.raises(ValueError):
        mgr.create_from_abs_range(10, 10)
    with pytest.raises(RuntimeError, match="past current head"):
        mgr.create_from_abs_range(1400, 1600)
    with pytest.raises(RuntimeError, match="overwritten"):
        mgr.create_from_abs_range(100, 200)


def test_max_active_cap_refuses_new_checkouts(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, max_active_checkouts=2)
    mgr.create(duration_s=0.1)
    mgr.create(duration_s=0.1)
    with pytest.raises(RuntimeError, match="Maximum active checkouts"):
        mgr.create(duration_s=0.1)


def test_checkout_create_does_not_stall_writer(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=2.0, rate=48_000, channels=2, frames=0)
    buf = mgr._buffer  # noqa: SLF001
    stop = threading.Event()
    written = [0]

    def writer():
        block = np.zeros((4096, 2), dtype=np.float32)
        while not stop.is_set():
            buf.write(block)
            written[0] += 4096
    t = threading.Thread(target=writer, daemon=True)
    t.start()
    time.sleep(0.05)
    for _ in range(5):
        mgr.create(duration_s=0.5)
    stop.set()
    t.join()
    assert written[0] > 4096 * 5


def test_save_as_wav_writes_correct_samples(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)
    out = mgr.save(co.id, tmp_path / "out" / "clip.wav")
    audio, info = read_wav(out)
    assert info.frames == 500 and info.subtype == "FLOAT"
    assert audio[0, 0] == pytest.approx(1000.0)
    assert mgr.get(co.id).state == "saved"


def test_save_trimmed_uses_the_trim_and_updates_the_manifest(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)
    mgr.set_trim(co.id, 100, 300)
    assert co.trim_range() == (100, 200) and co.has_trim()
    out = mgr.save(co.id, tmp_path / "t.wav", trimmed=True, subtype="PCM_16", mark_saved=False)
    audio, info = read_wav(out)
    assert info.frames == 200 and info.subtype == "PCM_16"
    assert mgr.get(co.id).state == "pending"
    m = read_manifest(manifest_path(tmp_path, co.id))
    assert (m.trim_in, m.trim_out) == (100, 300)
    full = mgr.save(co.id, tmp_path / "f.wav", trimmed=False)
    assert read_wav(full)[1].frames == 500


def test_save_validation(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.1)
    with pytest.raises(ValueError):
        mgr.save(co.id, tmp_path / "x.flac", fmt="FLAC")
    with pytest.raises(ValueError):
        mgr.save(co.id, tmp_path / "x.wav", subtype="PCM_8")
    with pytest.raises(KeyError):
        mgr.save("nope", tmp_path / "x.wav")
    with pytest.raises(KeyError):
        mgr.mark_saved("nope")
    with pytest.raises(KeyError):
        mgr.discard("nope")


def test_mark_saved_sets_state(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.1)
    mgr.mark_saved(co.id)
    assert mgr.get(co.id).state == "saved"
    m = read_manifest(manifest_path(tmp_path, co.id))
    assert m.state == "saved"


def test_discard_removes_manifest_and_wav(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.1)
    _wait_written(mgr, co)
    mgr.discard(co.id)
    assert mgr.list() == []
    assert not manifest_path(tmp_path, co.id).exists() and not co.path.exists()


def test_discard_before_the_write_lands_still_cleans_up(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path, seconds=2.0, rate=48_000, channels=2, frames=96_000)
    co = mgr.create(duration_s=2.0)  # 768 KB: the write is still queued or running
    mgr.discard(co.id)  # destroy waits for the job, then the file goes
    assert not co.path.exists() and not (tmp_path / f"{co.id}.wav.part").exists()


def test_flushing_buffer_does_not_invalidate_existing_checkouts(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)
    mgr._buffer.flush()  # noqa: SLF001
    audio = _audio(mgr, co)
    assert audio[0, 0] == pytest.approx(1000.0)


def test_pin_and_peak_bins_and_resident_bytes(scratch, tmp_path):
    scratch.set_budget(0)
    mgr = _mgr(scratch, tmp_path)
    co = mgr.create(duration_s=0.5)
    _wait_written(mgr, co)
    mgr.pin(None)  # any touch trims to budget 0
    assert mgr.resident_bytes(co.id) == 0
    bins = mgr.peak_bins(co.id, 10)  # streamed from the file
    assert bins.shape == (10, 2, 1)
    np.testing.assert_array_equal(bins, native.wav_peak_bins(co.path, 0, 500, 10))
    mgr.pin(co.id)
    t0 = time.monotonic()
    while mgr.resident_bytes(co.id) == 0 and time.monotonic() - t0 < 5:
        time.sleep(0.005)
    assert mgr.resident_bytes(co.id) == 500 * 4


def test_adopt_root_and_slice_share_one_file_with_a_refcount(scratch, tmp_path):
    from flashback_sampler.core.manifest import Manifest, write_manifest
    mgr = _mgr(scratch, tmp_path)
    root = mgr.create(duration_s=0.5)
    _wait_written(mgr, root)
    m_root = read_manifest(manifest_path(tmp_path, root.id))
    mgr.close()  # handles gone, files stay
    mgr2 = _mgr(scratch, tmp_path, frames=0)
    a = mgr2.adopt_root(m_root, root.path, partial=False)
    assert a.id == root.id and a.n_frames == 500 and mgr2.write_state(a.id) == "adopted"
    assert a.bins["540"].shape == (540, 2, 1)  # from the manifest, no audio read
    m_slice = Manifest(id="slice1", slot="Main", rate=1000, channels=1, abs_start=1100, abs_end=1200,
                       created_at=2.0, parent=root.id, start_frame=100, n_frames=100, trim_in=0, trim_out=0,
                       state="saved", partial=False, bins=None)
    write_manifest(tmp_path, m_slice)
    s = mgr2.adopt_slice(m_slice, a)
    assert s.path == a.path and s.start_frame == 100 and s.parent_id == root.id
    assert s.bins["360"].shape == (360, 2, 1)  # computed once, from the file
    assert mgr2.file_refcount(a.path) == 2
    mgr2.discard(a.id)
    assert a.path.exists() and mgr2.file_refcount(a.path) == 1
    mgr2.discard(s.id)
    assert not a.path.exists() and mgr2.file_refcount(a.path) == 0


def test_adopt_root_partial_clamps_to_the_file(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    root = mgr.create(duration_s=0.5)
    _wait_written(mgr, root)
    m = read_manifest(manifest_path(tmp_path, root.id))
    mgr.close()
    # chop the file to 200 frames
    data = root.path.read_bytes()
    root.path.write_bytes(data[:44 + 200 * 4])
    mgr2 = _mgr(scratch, tmp_path, frames=0)
    a = mgr2.adopt_root(m, root.path, partial=True)
    assert a.n_frames == 200 and a.partial is True
    assert read_manifest(manifest_path(tmp_path, a.id)).partial is True
```

- [ ] **Step 2: Run to see it fail**

`python -m pytest tests/unit/test_checkout.py -q` → `TypeError: CheckoutManager.__init__() got an unexpected keyword argument 'scratch'`.

- [ ] **Step 3: Rewrite `flashback_sampler/core/checkout.py`**

```python
"""
Checkout workflow — pull immutable snapshots of the live ring buffer.

Mental model (user-provided): a DJ with one turntable still spinning,
pulling a record off the rack to audition. The ring keeps writing
throughout. Each Checkout is a frozen copy of a span of the ring.

Where the audio lives (epic #53): in Zig. `create` copies the span out
of the ring into a Zig-owned buffer and queues its scratch write; the
scratch file `<scratch_dir>/<id>.wav` is the checkout from then on, and
the RAM copy is a cache the engine manages under a byte budget. Python
never holds samples: this module holds ids, states, trims, per-file
refcounts and the JSON manifests that adoption reads at launch.

A slice is a reference into its parent's file — `(path, start_frame,
n_frames)` with `parent_id` set. A file lives while any checkout in
this manager references it (`_file_refs`); the last discard deletes it.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import numpy as np

from flashback_sampler.core import native
from flashback_sampler.core.manifest import (
    Manifest, audio_path, bins_from_json, bins_to_json, manifest_path, write_manifest,
)
from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch


CheckoutState = Literal["pending", "ready", "saved", "discarded"]
CheckoutFormat = Literal["WAV"]
CheckoutSubtype = Literal["FLOAT", "PCM_24", "PCM_16"]

# FLOAT keeps the float32 scratch bit-perfect on disk.
_DEFAULT_SUBTYPE = "FLOAT"
_VALID_SUBTYPES: tuple[str, ...] = ("FLOAT", "PCM_24", "PCM_16")
# The deck draws two bin resolutions per checkout: the radial ring (540)
# and the clip panel (360). Computed once at create (from the RAM copy)
# and stored in the manifest so adoption never reads audio for them.
BIN_COUNTS: tuple[int, ...] = (540, 360)


@dataclass
class Checkout:
    """A frozen span of ring audio. `handle` is the Zig `*Checkout`;
    `path`/`start_frame`/`n_frames` say where the same audio lives on
    disk. `abs_sample_*` are the ring's absolute sample positions at
    creation time (display metadata)."""

    id: str
    handle: int
    path: Path
    created_at: float  # monotonic
    sample_rate: int
    channels: int
    n_frames: int
    start_frame: int
    abs_sample_start: int
    abs_sample_end: int
    parent_id: Optional[str] = None
    trim_in_samples: int = 0
    trim_out_samples: int = 0
    state: CheckoutState = "pending"
    partial: bool = False
    bins: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return self.n_frames / self.sample_rate

    def has_trim(self) -> bool:
        return self.trim_in_samples > 0 or (0 < self.trim_out_samples < self.n_frames)

    def trim_range(self) -> tuple[int, int]:
        """(start, n) within the checkout: the trim, or the whole clip."""
        n = self.n_frames
        start = max(0, min(self.trim_in_samples, n))
        end = n if self.trim_out_samples <= 0 else max(start, min(self.trim_out_samples, n))
        return start, end - start


class CheckoutManager:
    """
    Creates, tracks, saves and discards Checkouts for one slot. A single
    CheckoutManager per CaptureSlot; all share one NativeScratch (the
    process-wide writer + cache). Public operations are thread-safe.
    """

    _VALID_FORMATS: tuple[str, ...] = ("WAV",)

    def __init__(
        self,
        buffer: NativeAudioCircularBuffer,
        scratch: NativeScratch,
        scratch_dir: Path | str,
        slot_name: str = "",
        max_active_checkouts: int = 16,
    ):
        self._buffer = buffer
        self._scratch = scratch
        self._scratch_dir = Path(scratch_dir)
        self._slot_name = slot_name
        self._max_active = int(max_active_checkouts)
        self._lock = threading.Lock()
        self._checkouts: dict[str, Checkout] = {}
        self._file_refs: dict[Path, int] = {}
        self._pinned_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create(self, duration_s: float, anchor: str = "latest", anchor_offset_s: float = 0.0) -> Checkout:
        """Snapshot `duration_s` seconds ending `anchor_offset_s` ago
        (0 = now). Both clamp to what the ring holds: the offset to
        `buffered - 1 sample`, the start to the readable window."""
        if anchor != "latest":
            raise NotImplementedError(f"anchor={anchor!r} not yet supported")
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if anchor_offset_s < 0:
            raise ValueError("anchor_offset_s must be non-negative")
        buf = self._buffer
        sr = buf.sample_rate
        buffered_s = buf.buffered_seconds
        effective_offset_s = min(float(anchor_offset_s), max(0.0, buffered_s - 1.0 / sr))
        total = buf.total_written
        abs_end = total - int(effective_offset_s * sr)
        oldest = max(0, total - buf.buffer_size)
        abs_start = max(oldest, abs_end - int(duration_s * sr))
        if abs_end <= abs_start:
            raise RuntimeError("nothing buffered yet")
        return self.create_from_abs_range(abs_start, abs_end)

    def create_from_abs_range(self, abs_start: int, abs_end: int) -> Checkout:
        """Commit the exact absolute span `[abs_start, abs_end)` (the
        drag-select path). Raises RuntimeError when the span is past the
        head, already overwritten, or the count cap is hit."""
        if abs_end <= abs_start:
            raise ValueError(f"abs_end must be greater than abs_start ({abs_end} <= {abs_start})")
        buf = self._buffer
        total = buf.total_written
        if abs_end > total:
            raise RuntimeError(f"requested range extends past current head (abs_end={abs_end}, total_written={total})")
        if total - abs_start > buf.buffer_size:
            raise RuntimeError("requested range has already been overwritten")
        with self._lock:
            if len(self._checkouts) >= self._max_active:
                raise RuntimeError(f"Maximum active checkouts reached ({self._max_active})")
            cid = uuid.uuid4().hex[:12]
            path = audio_path(self._scratch_dir, cid)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                handle = self._scratch.checkout_create(buf, int(abs_start), int(abs_end), path)
            except RuntimeError as e:
                # overwritten / out_of_range from the engine: same wording the UI already reports
                raise RuntimeError(f"could not read requested range; {e}") from e
            co = Checkout(
                id=cid, handle=handle, path=path, created_at=time.monotonic(),
                sample_rate=buf.sample_rate, channels=buf.channels,
                n_frames=int(abs_end - abs_start), start_frame=0,
                abs_sample_start=int(abs_start), abs_sample_end=int(abs_end),
            )
            co.bins = {str(n): self._scratch.checkout_peak_bins(handle, n) for n in BIN_COUNTS}
            self._register(co)
        return co

    def _register(self, co: Checkout) -> None:
        """Lock held. Track the checkout, count its file, write its manifest."""
        self._checkouts[co.id] = co
        self._file_refs[co.path] = self._file_refs.get(co.path, 0) + 1
        self._write_manifest(co)

    def _write_manifest(self, co: Checkout) -> None:
        write_manifest(self._scratch_dir, Manifest(
            id=co.id, slot=self._slot_name, rate=co.sample_rate, channels=co.channels,
            abs_start=co.abs_sample_start, abs_end=co.abs_sample_end, created_at=time.time(),
            parent=co.parent_id, start_frame=co.start_frame, n_frames=co.n_frames,
            trim_in=co.trim_in_samples, trim_out=co.trim_out_samples, state=co.state,
            partial=co.partial, bins=bins_to_json(co.bins) if co.bins else None,
        ))

    # ------------------------------------------------------------------
    # Adoption (launch)
    # ------------------------------------------------------------------

    def adopt_root(self, m: Manifest, audio: Path, partial: bool) -> Checkout:
        """A root whose file already exists. Frame count comes from the
        file (a partial file reports its true prefix); bins from the
        manifest when present, else computed once from the file."""
        handle = self._scratch.checkout_open(audio, 0, max(1, int(m.n_frames)))
        info = self._scratch.checkout_info(handle)
        co = Checkout(
            id=m.id, handle=handle, path=Path(audio), created_at=time.monotonic(),
            sample_rate=int(info.rate), channels=int(info.channels),
            n_frames=int(info.n_frames), start_frame=0,
            abs_sample_start=int(m.abs_start), abs_sample_end=int(m.abs_end),
            trim_in_samples=int(m.trim_in), trim_out_samples=int(m.trim_out),
            state=m.state if m.state in ("pending", "ready", "saved") else "pending",
            partial=bool(partial or m.partial),
        )
        co.bins = bins_from_json(m.bins, co.channels)
        if set(co.bins) != {str(n) for n in BIN_COUNTS}:
            co.bins = {str(n): self._scratch.checkout_peak_bins(handle, n) for n in BIN_COUNTS}
        with self._lock:
            self._register(co)
        return co

    def adopt_slice(self, m: Manifest, parent: Checkout) -> Checkout:
        """A slice of an adopted parent in THIS manager."""
        handle = self._scratch.checkout_slice(parent.handle, int(m.start_frame), int(m.n_frames))
        co = Checkout(
            id=m.id, handle=handle, path=parent.path, created_at=time.monotonic(),
            sample_rate=parent.sample_rate, channels=parent.channels,
            n_frames=int(m.n_frames), start_frame=int(m.start_frame),
            abs_sample_start=int(m.abs_start), abs_sample_end=int(m.abs_end),
            parent_id=parent.id, trim_in_samples=int(m.trim_in), trim_out_samples=int(m.trim_out),
            state=m.state if m.state in ("pending", "ready", "saved") else "saved",
        )
        co.bins = bins_from_json(m.bins, co.channels)
        if set(co.bins) != {str(n) for n in BIN_COUNTS}:
            co.bins = {str(n): self._scratch.checkout_peak_bins(handle, n) for n in BIN_COUNTS}
        with self._lock:
            self._register(co)
        return co

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list(self) -> list[Checkout]:
        with self._lock:
            return list(self._checkouts.values())

    def get(self, checkout_id: str) -> Checkout:
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            return self._checkouts[checkout_id]

    def write_state(self, checkout_id: str) -> str:
        return native.WRITE_STATES[self._scratch.checkout_info(self.get(checkout_id).handle).write_state]

    def resident_bytes(self, checkout_id: str) -> int:
        return int(self._scratch.checkout_info(self.get(checkout_id).handle).resident_bytes)

    def peak_bins(self, checkout_id: str, n_bins: int) -> np.ndarray:
        return self._scratch.checkout_peak_bins(self.get(checkout_id).handle, n_bins)

    def file_refcount(self, path: Path | str) -> int:
        with self._lock:
            return self._file_refs.get(Path(path), 0)

    # ------------------------------------------------------------------
    # UI state
    # ------------------------------------------------------------------

    def set_trim(self, checkout_id: str, trim_in: int, trim_out: int) -> None:
        co = self.get(checkout_id)
        with self._lock:
            co.trim_in_samples = max(0, int(trim_in))
            co.trim_out_samples = max(co.trim_in_samples, int(trim_out)) if trim_out > 0 else 0
            self._write_manifest(co)

    def pin(self, checkout_id: Optional[str]) -> None:
        """The selected clip: pinned (never evicted) and preloaded. One
        at a time per manager; None unpins."""
        with self._lock:
            prev = self._pinned_id
            self._pinned_id = checkout_id
        if prev and prev != checkout_id:
            try:
                self._scratch.checkout_pin(self.get(prev).handle, False)
            except KeyError:
                pass
        if checkout_id is not None:
            self._scratch.checkout_pin(self.get(checkout_id).handle, True)

    # ------------------------------------------------------------------
    # Save / discard
    # ------------------------------------------------------------------

    def export_range(self, checkout_id: str, target_path: Path | str, start: int, n: int, subtype: str = _DEFAULT_SUBTYPE) -> Path:
        """Materialise `[start, start + n)` of the checkout into a WAV.
        Zig reads the scratch file (or the RAM copy while the write is
        still in flight) — no audio crosses into Python."""
        if subtype not in _VALID_SUBTYPES:
            raise ValueError(f"Unsupported subtype {subtype!r}; must be one of {_VALID_SUBTYPES}")
        co = self.get(checkout_id)
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._scratch.checkout_export(co.handle, target, int(start), int(n), subtype)
        return target

    def save(
        self,
        checkout_id: str,
        target_path: Path | str,
        fmt: CheckoutFormat = "WAV",
        trimmed: bool = True,
        subtype: CheckoutSubtype | None = None,
        mark_saved: bool = True,
    ) -> Path:
        fmt = fmt.upper()  # type: ignore[assignment]
        if fmt not in self._VALID_FORMATS:
            raise ValueError(f"Unsupported format {fmt!r}; must be one of {self._VALID_FORMATS}")
        co = self.get(checkout_id)
        start, n = co.trim_range() if trimmed else (0, co.n_frames)
        target = self.export_range(checkout_id, target_path, start, n, subtype or _DEFAULT_SUBTYPE)
        if mark_saved:
            self.mark_saved(checkout_id)
        return target

    def mark_saved(self, checkout_id: str) -> None:
        co = self.get(checkout_id)
        with self._lock:
            co.state = "saved"
            self._write_manifest(co)

    def discard(self, checkout_id: str) -> None:
        """Destroy the handle, delete the manifest, delete the WAV when
        no other checkout references it."""
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            co = self._checkouts.pop(checkout_id)
            co.state = "discarded"
            if self._pinned_id == checkout_id:
                self._pinned_id = None
            self._scratch.checkout_destroy(co.handle)  # waits for any job on it
            manifest_path(self._scratch_dir, co.id).unlink(missing_ok=True)
            refs = self._file_refs.get(co.path, 0) - 1
            if refs <= 0:
                self._file_refs.pop(co.path, None)
                co.path.unlink(missing_ok=True)
                Path(f"{co.path}.part").unlink(missing_ok=True)
            else:
                self._file_refs[co.path] = refs

    def discard_all(self) -> None:
        for co in self.list():
            self.discard(co.id)

    def close(self) -> None:
        """Release every handle and keep every file: the next launch
        adopts them. Shutdown path."""
        with self._lock:
            for co in self._checkouts.values():
                self._scratch.checkout_destroy(co.handle)
            self._checkouts.clear()
            self._file_refs.clear()
            self._pinned_id = None
```

- [ ] **Step 4: `capture_slot.py` and `drag_export.py`**

`from_quality_preset(cls, preset, name="", max_active_checkouts=16, *, scratch, scratch_dir)`: build the manager as `CheckoutManager(buffer=buf, scratch=scratch, scratch_dir=scratch_dir, slot_name=name or preset.name, max_active_checkouts=max_active_checkouts)`; delete the `max_total_ram_mb` parameter and the `ram_bytes` docstring's "(excluding checkouts — those are tracked separately by the CheckoutManager's cap)" clause → "(excluding checkouts — the scratch cache accounts for those)".

`drag_export.render_drag_file`: replace the body's audio lines with

```python
    co = manager.get(checkout_id)
    start, n = co.trim_range() if trimmed else (0, co.n_frames)
    duration_s = n / co.sample_rate
    when = now or datetime.now()
    pool = Path(pool_dir)
    pool.mkdir(parents=True, exist_ok=True)
    target = resolve_collision(pool / drag_filename(source_name, when, duration_s))
    manager.export_range(checkout_id, target, start, n, bit_depth)
    return target
```

- [ ] **Step 5: Test fixtures for slot and drag tests**

`tests/unit/test_capture_slot.py`: add

```python
@pytest.fixture
def scratch():
    from flashback_sampler.core.native import NativeScratch
    s = NativeScratch(budget_bytes=1 << 30)
    yield s
    s.close()


def _slot(scratch, tmp_path, preset, name=""):
    return CaptureSlot.from_quality_preset(preset, name=name, scratch=scratch, scratch_dir=tmp_path)
```

and rewrite every `CaptureSlot.from_quality_preset(...)` call as `_slot(scratch, tmp_path, ...)`, adding `scratch, tmp_path` to each test's parameters (13 sites, listed at `test_capture_slot.py:24,40,46,47,52,59,65,71,86,103,111,120,135`).

`tests/unit/test_drag_export.py`: `_mgr_with_checkout(scratch, tmp_path)` builds `CheckoutManager(buffer=buf, scratch=scratch, scratch_dir=tmp_path / "scratch")`; the three `render_drag_file` tests take `scratch, tmp_path`; the trim test uses `mgr.set_trim(co.id, 100, 300)`. Add the `scratch` fixture (same body as above, with `s.start()`).

- [ ] **Step 6: Run**

```bash
python -m pytest tests/unit/test_checkout.py tests/unit/test_capture_slot.py tests/unit/test_drag_export.py -q
```

Expected: all pass (`test_checkout.py`: 22). The rest of the suite is red until Task h9 (AppState) — expected at this step.

- [ ] **Step 7: Mutation checks**

(a) In `discard` drop the `refs <= 0` branch's `unlink` → `test_discard_removes_manifest_and_wav` and the refcount test redden. Revert.
(b) In `discard` delete the file unconditionally → the refcount test reddens (`a.path.exists()` after the first discard). Revert.
(c) In `create` replace `oldest` with 0 → the past-head/overwritten test still passes (create_from_abs_range guards it) but `test_checkout_duration_clamped_to_available` reddens only if `total - buffer_size > 0`; add `frames=1500` there? No — `seconds=1.0, frames=300` keeps oldest = 0. Instead mutate `create_from_abs_range`'s `total - abs_start > buf.buffer_size` to `>=`: `test_create_from_abs_range_rejects_...` stays red-correct, and `test_checkout_duration_clamped_to_available` still passes. Record: the availability clamp in `create` is pinned by the anchor-offset tests (500..1000 exact), which redden if `oldest` is mis-set to `total - buffer_size + 1`. Verify that mutation reddens; revert.

- [ ] **Step 8: Commit**

```bash
git add flashback_sampler/core/checkout.py flashback_sampler/core/capture_slot.py flashback_sampler/core/drag_export.py tests/unit/test_checkout.py tests/unit/test_capture_slot.py tests/unit/test_drag_export.py
git commit -m "feat: CheckoutManager over Zig handles — scratch on create, manifests, refcounted files, adoption"
```

### Task h9: `AppState` — owns the scratch, adopts at launch, RAM accounting

**Files:**
- Modify: `flashback_sampler/app/state.py` (`__init__`, `add_slot`, `total_project_ram_bytes`, `apply_checkout_caps`, `remove_slot`, `shutdown`; new `adopt_scratch`)
- Modify: `tests/unit/test_app_state.py`

**Interfaces:**
- Produces: `AppState(buffer_seconds, sample_rate, channels, scratch_dir: Path | None = None, checkout_cache_mb: float | None = None)`, `AppState.scratch: NativeScratch`, `AppState.scratch_dir: Path`, `AppState.adopt_scratch() -> list[Checkout]`, `AppState.add_slot(preset, name="", max_active_checkouts=16, capture_spec=None, armed=True)`, `AppState.apply_checkout_caps(max_active=None)`.

- [ ] **Step 1: Failing tests (append to `tests/unit/test_app_state.py`; edit the two named tests)**

Edit `test_total_project_ram_includes_checkouts` (line 253-265): replace the last assertion with

```python
    # The checkout's RAM copy lives in the scratch cache, counted once per process.
    assert st.total_project_ram_bytes() == buf_bytes + st.scratch.resident_bytes
    assert st.scratch.resident_bytes == 500 * 4
```

Edit `test_apply_checkout_caps_scoped_to_active_slot` (line 223): call `st.apply_checkout_caps(max_active=3)` and drop the `_max_ram_bytes` assertion.

Edit the playback test at line 400-425: replace `assert co.audio.shape == (500, 1)` with `assert co.n_frames == 500`, and the fake lib + bind with

```python
    fake_lib = type("L", (), {
        "fb_playback_create": staticmethod(lambda d, r, c: 0xF00D),
        "fb_playback_bind_checkout": staticmethod(lambda h, s, co, start, n: seen.update(start=start, n=n) or 0),
        "fb_playback_play": staticmethod(lambda h: seen.update(played=True) or 0),
        "fb_playback_destroy": staticmethod(lambda h: None),
    })()
    monkeypatch.setattr(native, "load", lambda: fake_lib)
    st.scrub_player.bind_checkout(st.scratch, co.handle, 0, co.n_frames, co.sample_rate, co.channels)
    st.scrub_player.play()
    assert seen == dict(start=0, n=500, played=True)
```

Append:

```python
def _written(st, co, timeout=5.0):
    import time
    t0 = time.monotonic()
    mgr = st.slots[0].checkout_manager
    while time.monotonic() - t0 < timeout:
        if mgr.write_state(co.id) == "written":
            return
        time.sleep(0.005)
    raise AssertionError("never written")


def test_state_uses_the_configured_scratch_dir_and_starts_the_writer(tmp_path):
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path / "s")
    assert st.scratch_dir == tmp_path / "s" and st.scratch_dir.is_dir()
    st.buffer.write(np.zeros((1000, 1), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.1)
    _written(st, co)
    assert (tmp_path / "s" / f"{co.id}.wav").exists()
    st.shutdown()


def test_adoption_restores_checkouts_into_a_matching_slot(tmp_path):
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.arange(1000, dtype=np.float32).reshape(-1, 1))
    co = st.checkout_manager.create(duration_s=0.5)
    st.checkout_manager.set_trim(co.id, 10, 20)
    st.checkout_manager.mark_saved(co.id)
    _written(st, co)
    st.shutdown()  # files stay

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    cos = st2.slots[0].checkout_manager.list()
    assert [c.id for c in cos] == [co.id]
    back = cos[0]
    assert (back.n_frames, back.trim_in_samples, back.trim_out_samples, back.state) == (500, 10, 20, "saved")
    assert back.bins["540"].shape == (540, 2, 1)
    assert len(st2.slots) == 1
    st2.shutdown()


def test_adoption_makes_an_unarmed_slot_for_a_foreign_rate(tmp_path):
    st = AppState(buffer_seconds=1.0, sample_rate=2000, channels=2, scratch_dir=tmp_path)
    st.buffer.write(np.zeros((2000, 2), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.2)
    _written(st, co)
    st.shutdown()

    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    assert len(st2.slots) == 2
    adopted = st2.slots[1]
    assert (adopted.sample_rate, adopted.channels, adopted.armed) == (2000, 2, False)
    assert adopted.name == "Main"  # the manifest's slot name
    assert [c.id for c in adopted.checkout_manager.list()] == [co.id]
    st2.shutdown()


def test_adoption_takes_a_part_file_as_partial_and_skips_junk(tmp_path):
    from flashback_sampler.core.manifest import Manifest, write_manifest
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.zeros((1000, 1), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.5)
    _written(st, co)
    st.shutdown()
    p = tmp_path / f"{co.id}.wav"
    data = p.read_bytes()
    (tmp_path / f"{co.id}.wav.part").write_bytes(data[:44 + 100 * 4])
    p.unlink()
    # a manifest with no audio at all, and a corrupt one
    write_manifest(tmp_path, Manifest(id="ghost", slot="Main", rate=1000, channels=1, abs_start=0, abs_end=1,
                                      created_at=0.0, parent=None, start_frame=0, n_frames=1, trim_in=0, trim_out=0,
                                      state="pending", partial=False, bins=None))
    (tmp_path / "bad.json").write_text("{")
    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    cos = st2.slots[0].checkout_manager.list()
    assert [c.id for c in cos] == [co.id]
    assert cos[0].partial is True and cos[0].n_frames == 100
    assert (tmp_path / "ghost.json").exists() and (tmp_path / "bad.json").exists()  # left in place
    st2.shutdown()


def test_adoption_of_a_slice_needs_its_parent(tmp_path):
    from flashback_sampler.core.manifest import Manifest, write_manifest
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    st.buffer.write(np.zeros((1000, 1), dtype=np.float32))
    co = st.checkout_manager.create(duration_s=0.5)
    _written(st, co)
    st.shutdown()
    write_manifest(tmp_path, Manifest(id="sl", slot="Main", rate=1000, channels=1, abs_start=0, abs_end=1,
                                      created_at=9.0, parent=co.id, start_frame=100, n_frames=50, trim_in=0, trim_out=0,
                                      state="saved", partial=False, bins=None))
    write_manifest(tmp_path, Manifest(id="orphan", slot="Main", rate=1000, channels=1, abs_start=0, abs_end=1,
                                      created_at=9.5, parent="missing", start_frame=0, n_frames=5, trim_in=0, trim_out=0,
                                      state="saved", partial=False, bins=None))
    st2 = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    ids = [c.id for c in st2.slots[0].checkout_manager.list()]
    assert ids == [co.id, "sl"]
    sl = st2.slots[0].checkout_manager.get("sl")
    assert sl.parent_id == co.id and sl.start_frame == 100 and sl.path == st2.slots[0].checkout_manager.get(co.id).path
    st2.shutdown()


def test_remove_slot_discards_its_checkouts_and_files(tmp_path):
    from flashback_sampler.core.quality_presets import preset_by_name
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1, scratch_dir=tmp_path)
    slot = st.add_slot(preset_by_name("SCRATCH"))
    slot.buffer.write(np.zeros((16_000, 1), dtype=np.float32))
    co = slot.checkout_manager.create(duration_s=0.1)
    st.remove_slot(1)
    assert not co.path.exists() and not (tmp_path / f"{co.id}.json").exists()
    st.shutdown()
```

- [ ] **Step 2: Run to see them fail**

`python -m pytest tests/unit/test_app_state.py -q` → `TypeError` on `scratch_dir`.

- [ ] **Step 3: Implement in `flashback_sampler/app/state.py`**

Imports: add `from pathlib import Path`, `from flashback_sampler.app import config as app_config`, `from flashback_sampler.core.checkout import Checkout`, `from flashback_sampler.core.manifest import resolve_audio, scan`, `from flashback_sampler.core.native import NativeScratch`.

`__init__` signature: `def __init__(self, buffer_seconds=DEFAULT_BUFFER_SECONDS, sample_rate=DEFAULT_SAMPLE_RATE, channels=DEFAULT_CHANNELS, scratch_dir: Path | None = None, checkout_cache_mb: float | None = None)`. Before the slot list:

```python
        # ── Scratch: the process-wide writer thread + RAM cache ─────────
        # One per AppState; every slot's CheckoutManager writes through
        # it. Checkouts scratch to <scratch_dir>/<id>.wav on creation
        # (epic #53). Budget in bytes; 0 = only pinned/in-flight stay.
        self.scratch_dir = Path(scratch_dir) if scratch_dir is not None else app_config.load_scratch_dir()
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        cache_mb = app_config.load_checkout_cache_mb() if checkout_cache_mb is None else float(checkout_cache_mb)
        self.scratch = NativeScratch(budget_bytes=int(cache_mb * 1024 * 1024))
        self.scratch.start()
```

The initial slot: `CaptureSlot.from_quality_preset(initial_preset, name="Main", max_active_checkouts=16, scratch=self.scratch, scratch_dir=self.scratch_dir)`. At the end of `__init__` (after `scrub_player`): `self.adopt_scratch()`.

`add_slot(self, preset, name="", max_active_checkouts=16, capture_spec=None, armed=True)`: drop `max_total_ram_mb`; pass `scratch=self.scratch, scratch_dir=self.scratch_dir`; after `slot.capture_spec = capture_spec` add `slot.armed = armed`.

```python
    def adopt_scratch(self) -> list[Checkout]:
        """Adopt every manifest in the scratch dir: a root goes to the
        first slot with its rate and channels, else to a new unarmed slot
        named from the manifest (60 s ring — the smallest that still
        plays); a slice goes where its parent went. Anything unreadable
        or without audio is skipped and left on disk. Crash and quit
        take this same path."""
        adopted: list[Checkout] = []
        where: dict[str, CaptureSlot] = {}
        for m in scan(self.scratch_dir):
            if m.parent is None:
                found = resolve_audio(self.scratch_dir, m)
                if found is None:
                    continue
                audio, partial = found
                slot = next((s for s in self.slots if s.sample_rate == m.rate and s.channels == m.channels), None)
                if slot is None:
                    slot = self.add_slot(
                        QualityPreset(name="ADOPTED", sample_rate=int(m.rate), channels=int(m.channels),
                                      buffer_seconds=60.0, description="Slot recreated for adopted checkouts"),
                        name=m.slot or "Adopted", armed=False,
                    )
                try:
                    co = slot.checkout_manager.adopt_root(m, audio, partial)
                except (OSError, ValueError, RuntimeError):
                    continue
            else:
                slot = where.get(m.parent)
                if slot is None:
                    continue
                try:
                    co = slot.checkout_manager.adopt_slice(m, slot.checkout_manager.get(m.parent))
                except (KeyError, ValueError, RuntimeError):
                    continue
            where[co.id] = slot
            adopted.append(co)
        return adopted
```

`total_project_ram_bytes`: rings as today + `self.scratch.resident_bytes` once, outside the slot loop; delete the per-checkout loop and update the docstring ("PLUS the scratch cache's resident bytes — every checkout's RAM copy, counted once per process").

`apply_checkout_caps(self, max_active=None)`: drop `max_ram_mb`.

`remove_slot`: before `slot.buffer.close()`: `slot.checkout_manager.discard_all()`; replace the "Checkouts hold copies..." comment with "A removed slot's checkouts go with it: manifests and files deleted."

`shutdown`: after stopping captures, `for slot in self.slots: slot.checkout_manager.close()` then `self.scratch.close()` (drains the writer) before `scrub_player.close()`.

Also `rebuild_buffer` (`state.py:372-400`): unchanged (`_buffer` swap still valid).

- [ ] **Step 4: Run the whole unit suite**

`python -m pytest tests/unit -q -m "not audio_hw and not perf" -x` — expect failures only in `test_turntable_window.py` (Task h10). Everything else green.

- [ ] **Step 5: Mutation checks**

(a) In `adopt_scratch` drop the `where` bookkeeping (always `continue` for slices) → the slice test reddens. Revert.
(b) In `adopt_scratch` match slots on rate only → the foreign-rate test still passes (channels differ too)… so mutate to "always the first slot" → reddens (`len(st2.slots) == 1`). Revert.

- [ ] **Step 6: Commit**

```bash
git add flashback_sampler/app/state.py tests/unit/test_app_state.py
git commit -m "feat(app): AppState owns the scratch writer, adopts the scratch dir at launch, counts resident bytes"
```

### Task h10: Window — bins from handles, pin on select, bind_checkout, no numpy audio

**Files:**
- Modify: `flashback_sampler/app/turntable_window.py` (sites listed below)
- Modify: `flashback_sampler/app/preferences_dialog.py` (scratch dir row)
- Modify: `tests/unit/test_turntable_window.py`, `tests/unit/test_scrub_player.py`, `tests/unit/test_preferences_dialog.py`

- [ ] **Step 1: Failing/updated tests**

`tests/unit/test_scrub_player.py` `_FakePlaybackLib.__getattr__`: add before the `fb_playback_play` branch:

```python
            if name == "fb_playback_bind_checkout":
                _h, _s, co, start, n = a
                assert _h is not None, "fb_playback_bind_checkout called with a closed/None handle"
                self.bound_checkout = (co, start, n)
                if self.bind_status == 0:
                    self.state = self.state[:3] + (n, self.state[4])
                return self.bind_status
```

and `self.bound_checkout = None` in `__init__`.

`tests/unit/test_turntable_window.py`:
- Every `co.audio.shape[0]` → `co.n_frames` (lines 607, 705, 938, 941, 966, 969).
- `test_play_click_binds_the_checkout_at_its_rate_and_plays`: replace `_arr, n, rate, ch = fake.bound; assert (n, rate, ch) == (...)` with `assert fake.bound_checkout == (co.handle, 0, co.n_frames)` and `assert state.scrub_player.sample_rate == co.sample_rate`.
- `test_clip_drag_out_uses_trimmed_range`: set the trim with `mgr.set_trim(co.id, n // 4, n // 2)`.
- Add:

```python
def test_selecting_a_clip_pins_it_and_bins_come_from_the_handle(qapp, state):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.active_slot.checkout_manager
        a = mgr.create(duration_s=0.2)
        b = mgr.create(duration_s=0.3)
        win._refresh_clip_side(auto_select_newest=True)
        assert mgr._pinned_id == b.id  # noqa: SLF001
        win.clip_turntable.select_track(0)
        win._refresh_clip_side()
        assert mgr._pinned_id == a.id  # noqa: SLF001
        assert win._clip_bins_cache[a.id]["panel_bins"].shape == (360, 2, state.channels)
        assert win._clip_bins_cache[a.id]["ring_amp"].shape == (540,)
    finally:
        win.close()


def test_play_with_a_trim_binds_the_trim_range(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    try:
        co = _checkout(state)
        state.checkout_manager.set_trim(co.id, 100, 300)
        win._tick()
        fake.state = (0, 0, 0, 0, 48_000)
        win._on_play_clip_clicked()
        assert fake.bound_checkout == (co.handle, 100, 200)
    finally:
        win.close()


def test_buffer_drag_at_the_count_cap_evicts_the_oldest_saved(qapp, state, monkeypatch, tmp_path):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        state.apply_checkout_caps(max_active=1)
        win._export_pool_dir = tmp_path
        monkeypatch.setattr("flashback_sampler.app.turntable_window.perform_file_drag", lambda w, p: True)
        win._on_buffer_drag_out(0.0, 0.5)
        first = state.checkout_manager.list()[0].id
        win._on_buffer_drag_out(0.0, 0.5)
        cos = state.checkout_manager.list()
        assert len(cos) == 1 and cos[0].id != first and cos[0].state == "saved"
    finally:
        win.close()
```

`tests/unit/test_preferences_dialog.py`: add

```python
def test_scratch_dir_row_reports_a_pick(qapp, monkeypatch):
    seen = []
    dlg = PreferencesDialog(show_notifications=True, on_notifications_changed=lambda c: None,
                            scratch_dir="C:/old", on_scratch_dir_changed=seen.append)
    assert dlg.scratch_dir_edit.text() == "C:/old"
    monkeypatch.setattr("flashback_sampler.app.preferences_dialog.QFileDialog.getExistingDirectory",
                        staticmethod(lambda *a, **k: "C:/new"))
    dlg.scratch_dir_btn.click()
    assert seen == ["C:/new"] and dlg.scratch_dir_edit.text() == "C:/new"
```

(Read `tests/unit/test_preferences_dialog.py`'s existing export-dir test and mirror its monkeypatch style exactly.)

- [ ] **Step 2: Run to see them fail**

`python -m pytest tests/unit/test_turntable_window.py tests/unit/test_preferences_dialog.py -q` → failures on `.audio`, `bound_checkout`, `scratch_dir`.

- [ ] **Step 3: Window edits (`flashback_sampler/app/turntable_window.py`)**

1. Delete `_peak_bins_from_audio` (lines 84-107) and the `import numpy as np` only if no other use remains (grep `np.` first — `_refresh_clip_side` still uses `np.clip`; keep).
2. `on_clip_sel` (596-608): `n = co.n_frames`; replace the two direct field writes with `self._state.active_slot.checkout_manager.set_trim(co.id, max(0, int(start * n)), max(int(start * n), int(end * n)))`. `on_clip_clear` (610-616): `...set_trim(co.id, 0, 0)`. `_apply_clip_trim` (981-987): same. The context-menu `_clear` (in `_on_clip_panel_context_menu`): same.
3. `_checkout_has_trim` (810-819): `return co.has_trim()`.
4. `_on_play_clip_clicked` (911-948): replace the `audio = ...; player.bind(audio, co.sample_rate)` pair with

```python
        start, n = co.trim_range() if has_trim else (0, co.n_frames)
        try:
            player.bind_checkout(self._state.scratch, co.handle, start, n, co.sample_rate, co.channels)
            player.play()
```

5. `_nudge_clip_trim_span` / `_nudge_clip_trim_shift` (954, 970): `co.n_frames == 0`.
6. `_refresh_clip_side` (1118-1130): `bins = co.bins["540"]` (no compute). `_display_clip_in_panel` (1139-1145): `bins = co.bins["360"]`; after `self.clip_panel.set_clip_id(...)` add `self._state.active_slot.checkout_manager.pin(co.id)`. In the no-checkouts branch (around 1098-1116) add `self._state.active_slot.checkout_manager.pin(None)`. Also in `_display_clip_in_panel`: `if mgr.write_state(co.id) == "failed": self.statusBar().showMessage(f"Scratch write failed: {co.id[:6].upper()} (clip kept in RAM)", 6000)` — the spec's disk-full behaviour.
7. Playhead (1698-1712): `co.n_frames` twice.
8. `_on_buffer_drag_out` (1300-1330): the retry loop's `at_cap` becomes `"Maximum active checkouts" in str(e)`; delete the `or "RAM cap" in str(e)` clause and the comment's RAM sentence. `_evict_oldest_saved_checkout` stays (count cap; P5).
9. Preferences wiring (`turntable_window.py:334-335`, `544-549`, and where `PreferencesDialog(` is constructed — grep): add `self._scratch_dir_pref = str(load_scratch_dir())`, pass `scratch_dir=self._scratch_dir_pref, on_scratch_dir_changed=self._set_scratch_dir`, and

```python
    def _set_scratch_dir(self, path_str: str) -> None:
        save_scratch_dir(path_str)
        self._scratch_dir_pref = path_str
        self.statusBar().showMessage("Scratch folder applies at next launch", 4000)
```

with `load_scratch_dir, save_scratch_dir` added to the `config` import.

`preferences_dialog.py`: add parameters `scratch_dir: str = ""`, `on_scratch_dir_changed: Callable[[str], None] | None = None`; after the Export section add a `<b>Scratch</b>` label, a read-only `self.scratch_dir_edit = QLineEdit(scratch_dir)`, `self.scratch_dir_btn = QPushButton("Browse…")` wired exactly like `_pick_export_dir` (dialog title "Scratch folder"), and a hint label "Checkouts are written here as they are made. Applies at next launch." (same style string as the existing hint).

- [ ] **Step 4: Run the full suite**

`python -m pytest tests/unit -q -m "not audio_hw and not perf"` → all green (≈ 544). `grep -n "\.audio\b\|_peak_bins_from_audio\|trimmed_audio" flashback_sampler tests -r --include=*.py` → no hits.

- [ ] **Step 5: App smoke (owner or executor at the machine)**

`python -m flashback_sampler.app.main`: check out a span, watch `<cache>/scratch/<id>.wav` appear, select it, PLAY, trim, drag to Explorer, discard → file gone; quit, relaunch → the deck shows the surviving checkouts.

- [ ] **Step 6: Commit**

```bash
git add flashback_sampler/app/turntable_window.py flashback_sampler/app/preferences_dialog.py tests/unit/test_turntable_window.py tests/unit/test_scrub_player.py tests/unit/test_preferences_dialog.py
git commit -m "feat(app): deck draws from checkout handles, pin on select, bind_checkout, scratch dir preference"
```

### Task h11: Measurement (owner at the machine) → `DEFAULT_CHECKOUT_CACHE_MB`

**Files:**
- Create: `tools/measure_reload.py` (a script, not a test)
- Modify: `flashback_sampler/app/config.py` (`DEFAULT_CHECKOUT_CACHE_MB`)

- [ ] **Step 1: The script**

```python
"""Select→playable measurement (spec: PR h, ruling 3). Prints the time
from pin (preload queued) to resident, and the fallback bind-from-file
time, for a clip of the given length/rate on the configured scratch
disk. Run: python tools/measure_reload.py 192000 900 ; and 48000 180."""
import sys
import time
from pathlib import Path

import numpy as np

from flashback_sampler.app import config
from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch
from flashback_sampler.core.scrub_player import NativeScrubPlayer


def main(rate: int, seconds: int) -> None:
    scratch_dir = config.load_scratch_dir() / "measure"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    buf = NativeAudioCircularBuffer(duration_seconds=seconds, sample_rate=rate, channels=2)
    block = (np.random.default_rng(1).standard_normal((4096, 2)) * 0.1).astype(np.float32)
    for _ in range(seconds * rate // 4096 + 1):
        buf.write(block)
    s = NativeScratch(budget_bytes=0)
    s.start()
    path = scratch_dir / "measure.wav"
    t0 = time.perf_counter()
    h = s.checkout_create(buf, buf.total_written - seconds * rate, buf.total_written, path)
    t_copy = time.perf_counter() - t0
    while s.checkout_info(h).write_state != 2:
        time.sleep(0.01)
    t_written = time.perf_counter() - t0
    s.checkout_pin(h, False)  # trims to budget 0 → evicted
    assert s.checkout_info(h).resident_bytes == 0
    t1 = time.perf_counter()
    s.checkout_pin(h, True)  # preload
    while s.checkout_info(h).resident_bytes == 0:
        time.sleep(0.001)
    t_preload = time.perf_counter() - t1
    s.checkout_pin(h, False)
    player = NativeScrubPlayer(rate, 2)
    t2 = time.perf_counter()
    player.bind_checkout(s, h, 0, seconds * rate, rate, 2)  # fallback: from file, on this thread
    t_bind_file = time.perf_counter() - t2
    mb = seconds * rate * 2 * 4 / 2**20
    print(f"{rate} Hz x {seconds} s stereo = {mb:.0f} MB on {scratch_dir}")
    print(f"copy from ring {t_copy*1000:.0f} ms | written after {t_written*1000:.0f} ms | preload {t_preload*1000:.0f} ms | bind from file {t_bind_file*1000:.0f} ms")
    player.close()
    s.checkout_destroy(h)
    s.close()
    path.unlink(missing_ok=True)


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]))
```

- [ ] **Step 2: Owner runs** `python tools/measure_reload.py 192000 900` and `python tools/measure_reload.py 48000 180` and pastes both lines on the PR h sub-issue with the scratch disk named.

- [ ] **Step 3: Decide and record.** If the 48 kHz/180 s preload is under the owner's "feels instant" bound (proposed 100 ms), `DEFAULT_CHECKOUT_CACHE_MB` stays `0.0` and the comment becomes "Measured 2026-MM-DD on <disk>: 180 s @ 48 kHz preloads in N ms, 900 s @ 192 kHz in M ms — the RAM cache holds only pinned/in-flight clips". Otherwise set it to `2 * (180 * 48000 * 2 * 4) / 2**20` ≈ 132 MB (two such clips), with the same comment. Edit the spec's "Measurement task" paragraph with the numbers (small edit, per CLAUDE.md).

- [ ] **Step 4: Commit**

```bash
git add tools/measure_reload.py flashback_sampler/app/config.py docs/superpowers/specs/2026-08-30-checkout-persistence-design.md
git commit -m "chore: select→playable measurement + DEFAULT_CHECKOUT_CACHE_MB from it"
```

### Task h12: Sequester, gates, docs, PR h

- [ ] **Step 1: Sequester** — nothing is deleted whole in this PR (`checkout.py` is rewritten in place; `_peak_bins_from_audio` is a function). Confirm with `git status`; no `_ToRemove/` entries expected.

- [ ] **Step 2: Docs** — `README.md`/`PLATFORM.md`: add one paragraph "Checkouts scratch to `%LOCALAPPDATA%\flashback-sampler\Cache\scratch` (Preferences → Scratch) as float32 WAV at the capture rate; the app adopts that folder at launch, so a crash or quit keeps every checkout. Discard deletes the file." Update any sentence that says checkouts are RAM-only (grep `in RAM`, `in-RAM`).

- [ ] **Step 3: Full gate** (same block as Task g7 Step 1). Expected: `199/199`; three builds; ≈ 544 pytest.

- [ ] **Step 4: Whole-branch review** — one combined inline `/simplify` + `/code-review` at **high** (this PR touches lifetime and threading). Fix findings; re-run Step 3.

- [ ] **Step 5: Push + PR** — `feat/zig-scratch` → `dev`; body: `Closes #NN`; counts; "Zig concepts": intrusive lists, `std.Io.Mutex`/`Condition` producer-consumer, control-thread-owned worker with drain-on-stop, tagged-union `ClipSource`, atomic enum state polled across the ABI; "Deviations": P5 (count-cap eviction kept), P7 (no `last_use`), P8 (Python owns refcounts), the adopted slot's 60 s ring; the measurement numbers. Owner: app smoke (Task h10 Step 5), measurement (Task h11), merge, tick the epic box.

---
## PR i — slices as references, markers, handles, the Ableton spike

**Branch:** `feat/slices-handles` from `dev` (after PR h merged). **Target:** `dev`. **Spec section:** "PR i". Baseline after h: Zig 199 / pytest ≈ 544. **Task → count map:** i1 +4 Zig = 203 · i3 +9 pytest · i4 +5 pytest · i5 (conditional) +3 pytest.

**Drag-out shapes after this PR (one export primitive, three callers):**

| Gesture | Checkout minted | File exported | Markers |
|---|---|---|---|
| Clip deck, trimmed band dragged | a **slice** `(parent file, trim_in, n)`, state `saved` | the whole slice plus up to `drag_handle_mb` of parent audio before/after | at the slice |
| Clip deck, full clip dragged | none (parent marked `saved` on accept, as today) | the whole checkout | none |
| Buffer deck, selection dragged | a **root** = selection ± half (clamped to the ring), trim = selection, `saved` on accept | the whole root | at the trim |

The root case needs no slice: its trim IS the segment, and dragging the trim edges is "come back for more". The slice case exists so a parent can be re-trimmed and dragged again while the earlier segment survives on the deck (ruling 5).

**Plan choices (PR i):**

| # | Choice | Why |
|---|---|---|
| P13 | `CheckoutManager.slice` waits for the parent's write to land (`written`/`adopted`, 30 s timeout) before minting. | A slice has no RAM copy; its bins and audio come from the parent's FILE. Minting before the file exists would fail on the first `peak_bins`. The wait is milliseconds in practice; the drag already shows a wait cursor. |
| P14 | The `.alc` sidecar (Task i5) is written only if the spike (Task i2) answers yes, and its template comes from the XML the spike captured. | The format is undocumented; the plan cannot pre-write fields it has not seen. The task gives the procedure, not invented tag names. |
| P15 | An odd `data` chunk length gets its RIFF pad byte in `copyRange` when markers follow. | pcm_24 mono spans are odd-sized; the next chunk must start word-aligned or Reaper/Logic will not find the markers. |

### Task i0: Branch, sub-issue

- [ ] **Step 1:** `git checkout dev; git pull; git checkout -b feat/slices-handles`; verify baselines (Zig 199, pytest count from the PR h hand-off).
- [ ] **Step 2:** `gh issue create --title "PR i: slices as references, drag-out with handles + markers, Ableton spike" --body "Sub-issue of #53. Spec: PR i. Plan Tasks i0-i6. Task i2 is the owner-at-the-machine spike whose result decides whether Task i5 (.alc sidecar) ships."`; add the box to #53.

### Task i1: `wav.Markers` — `cue `, `smpl`, `LIST/adtl` after the data chunk

**Files:**
- Modify: `core/src/wav.zig` (`copyRange` signature + `Markers`)
- Modify: `core/src/abi.zig` (`fb_checkout_export` gains `markers: ?*const FbMarkers`), `core/include/flashback_core.h`
- Modify: `flashback_sampler/core/native.py` (`checkout_export(..., markers=None)`)

**Interfaces:**
- Produces: `wav.Markers { slice_start: u64, slice_end: u64 }` (frames relative to the exported file, end exclusive), `wav.Markers.byte_len = 186`, `wav.copyRange(src, dst, start_frame, n_frames, st, markers: ?Markers) !void`; C `typedef struct FbMarkers { uint64_t slice_start; uint64_t slice_end; } FbMarkers;` and `FbStatus fb_checkout_export(FbScratch *, FbCheckout *, const char *dst, uint64_t start, uint64_t n, FbSubtype, const FbMarkers *markers /* nullable */)`; Python `NativeScratch.checkout_export(h, dst, start, n, subtype, markers: tuple[int, int] | None = None)`, `CheckoutManager.export_range(..., markers=None)`.

- [ ] **Step 1: Failing tests (append to `core/src/wav.zig`)**

```zig
/// Test helper: walk `bytes` from offset 12 and return the body offset
/// of the first chunk with id `id`, or null.
fn findChunk(bytes: []const u8, id: *const [4]u8) ?usize {
    var pos: usize = 12;
    while (pos + 8 <= bytes.len) {
        const size: usize = std.mem.readInt(u32, bytes[pos + 4 ..][0..4], .little);
        if (std.mem.eql(u8, bytes[pos .. pos + 4], id)) return pos + 8;
        pos += 8 + size + (size & 1);
    }
    return null;
}

test "copyRange with markers: cue, smpl and LIST/adtl follow data; RIFF size covers them; open still reads frames" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    var in: [40]f32 = undefined; // 20 stereo frames
    for (&in, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i)) * 0.01;
    const src = tmpWritePath(&pa, &tmp, "m-src.wav");
    try writeFile(src, &in, 48_000, 2, .float32);
    const dst = tmpWritePath(&pd, &tmp, "m-dst.wav");
    try copyRange(src, dst, 2, 15, .float32, .{ .slice_start = 3, .slice_end = 8 });
    var buf: [header_len + 15 * 8 + Markers.byte_len]u8 = undefined;
    const got = try tmp.dir.readFile(std.testing.io, "m-dst.wav", &buf);
    try std.testing.expectEqual(buf.len, got.len);
    try std.testing.expectEqual(@as(u32, @intCast(got.len - 8)), std.mem.readInt(u32, got[4..8], .little));
    const cue = findChunk(got, "cue ") orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(@as(u32, 2), std.mem.readInt(u32, got[cue..][0..4], .little));
    try std.testing.expectEqual(@as(u32, 3), std.mem.readInt(u32, got[cue + 4 + 20 ..][0..4], .little)); // point 1 sample offset
    try std.testing.expectEqual(@as(u32, 8), std.mem.readInt(u32, got[cue + 4 + 24 + 20 ..][0..4], .little)); // point 2
    try std.testing.expectEqualSlices(u8, "data", got[cue + 4 + 8 ..][0..4]);
    const smpl = findChunk(got, "smpl") orelse return error.TestUnexpectedResult;
    try std.testing.expectEqual(@as(u32, 1_000_000_000 / 48_000), std.mem.readInt(u32, got[smpl + 8 ..][0..4], .little)); // sample period ns
    try std.testing.expectEqual(@as(u32, 1), std.mem.readInt(u32, got[smpl + 28 ..][0..4], .little)); // one loop
    try std.testing.expectEqual(@as(u32, 3), std.mem.readInt(u32, got[smpl + 36 + 8 ..][0..4], .little)); // loop start
    try std.testing.expectEqual(@as(u32, 7), std.mem.readInt(u32, got[smpl + 36 + 12 ..][0..4], .little)); // loop end, inclusive
    const list = findChunk(got, "LIST") orelse return error.TestUnexpectedResult;
    try std.testing.expectEqualSlices(u8, "adtl", got[list..][0..4]);
    try std.testing.expectEqualSlices(u8, "labl", got[list + 4 ..][0..4]);
    try std.testing.expectEqualSlices(u8, "slice start\x00", got[list + 4 + 8 + 4 ..][0..12]);
    // the reader stops at data and is not confused by what follows
    var o = try open(dst);
    defer o.file.close(io);
    try std.testing.expectEqual(@as(u64, 15), o.info.frames);
}

test "copyRange with markers pads an odd data chunk before the marker chunks" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    const in = [_]f32{ 0.1, 0.2, 0.3, 0.4, 0.5 }; // 5 mono frames
    const src = tmpWritePath(&pa, &tmp, "odd-src.wav");
    try writeFile(src, &in, 8_000, 1, .float32);
    const dst = tmpWritePath(&pd, &tmp, "odd-dst.wav");
    try copyRange(src, dst, 0, 5, .pcm_24, .{ .slice_start = 1, .slice_end = 2 }); // data = 15 bytes, odd
    var buf: [header_len + 15 + 1 + Markers.byte_len]u8 = undefined;
    const got = try tmp.dir.readFile(std.testing.io, "odd-dst.wav", &buf);
    try std.testing.expectEqual(buf.len, got.len);
    try std.testing.expectEqual(@as(u8, 0), got[header_len + 15]); // the pad byte
    try std.testing.expectEqual(header_len + 16 + 8, findChunk(got, "cue ").?);
    try std.testing.expectEqual(@as(u32, @intCast(got.len - 8)), std.mem.readInt(u32, got[4..8], .little));
}

test "copyRange without markers is unchanged: no chunk after data" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3 };
    const src = tmpWritePath(&pa, &tmp, "n-src.wav");
    try writeFile(src, &in, 8_000, 1, .float32);
    const dst = tmpWritePath(&pd, &tmp, "n-dst.wav");
    try copyRange(src, dst, 0, 3, .float32, null);
    var buf: [header_len + 12]u8 = undefined;
    const got = try tmp.dir.readFile(std.testing.io, "n-dst.wav", &buf);
    try std.testing.expectEqual(buf.len, got.len);
}

test "Markers rejects an inverted or out-of-file slice" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3 };
    const src = tmpWritePath(&pa, &tmp, "b-src.wav");
    try writeFile(src, &in, 8_000, 1, .float32);
    const dst = tmpWritePath(&pd, &tmp, "b-dst.wav");
    try std.testing.expectError(error.InvalidArgument, copyRange(src, dst, 0, 3, .float32, .{ .slice_start = 2, .slice_end = 2 }));
    try std.testing.expectError(error.InvalidArgument, copyRange(src, dst, 0, 3, .float32, .{ .slice_start = 0, .slice_end = 4 }));
}
```

Update the three PR g `copyRange` tests to pass `null` as the last argument.

- [ ] **Step 2: Run to see them fail** — compile error: `copyRange` takes 5 arguments.

- [ ] **Step 3: Implement**

Insert before `copyRange`:

```zig
/// Where the slice sits inside an exported file, as three standard
/// RIFF chunks DAWs read as markers: `cue ` (two points), `smpl` (one
/// loop, start..end-1 inclusive, the sampler convention) and
/// `LIST/adtl` labels. Frames are relative to the exported file. No
/// DAW we know of turns these into clip start/end on drop (spec table);
/// they are the portable, harmless half of "drag the edge out".
pub const Markers = struct {
    slice_start: u64,
    slice_end: u64, // exclusive

    /// cue (8 + 4 + 2*24) + smpl (8 + 36 + 24) + LIST (8 + 4 + 24 + 22).
    pub const byte_len: usize = 60 + 68 + 58;

    fn validate(self: Markers, n_frames: u64) error{InvalidArgument}!void {
        if (self.slice_end <= self.slice_start or self.slice_end > n_frames) return error.InvalidArgument;
        if (self.slice_end > std.math.maxInt(u32)) return error.InvalidArgument;
    }

    fn write(self: Markers, out: *[byte_len]u8, rate: u32) void {
        const s: u32 = @intCast(self.slice_start);
        const e: u32 = @intCast(self.slice_end);
        var w: usize = 0;
        // cue
        @memcpy(out[w .. w + 4], "cue ");
        std.mem.writeInt(u32, out[w + 4 ..][0..4], 52, .little);
        std.mem.writeInt(u32, out[w + 8 ..][0..4], 2, .little);
        w += 12;
        for ([_]u32{ s, e }, 1..) |pos, id| {
            std.mem.writeInt(u32, out[w..][0..4], @intCast(id), .little); // dwName
            std.mem.writeInt(u32, out[w + 4 ..][0..4], pos, .little); // dwPosition (play order)
            @memcpy(out[w + 8 .. w + 12], "data"); // fccChunk
            std.mem.writeInt(u32, out[w + 12 ..][0..4], 0, .little); // dwChunkStart
            std.mem.writeInt(u32, out[w + 16 ..][0..4], 0, .little); // dwBlockStart
            std.mem.writeInt(u32, out[w + 20 ..][0..4], pos, .little); // dwSampleOffset
            w += 24;
        }
        // smpl
        @memcpy(out[w .. w + 4], "smpl");
        std.mem.writeInt(u32, out[w + 4 ..][0..4], 60, .little);
        const fields = [_]u32{ 0, 0, 1_000_000_000 / rate, 60, 0, 0, 0, 1, 0 };
        for (fields, 0..) |v, i| std.mem.writeInt(u32, out[w + 8 + i * 4 ..][0..4], v, .little);
        w += 8 + 36;
        const loop = [_]u32{ 1, 0, s, e - 1, 0, 0 }; // id, forward, start, end (inclusive), fraction, play count
        for (loop, 0..) |v, i| std.mem.writeInt(u32, out[w + i * 4 ..][0..4], v, .little);
        w += 24;
        // LIST/adtl
        @memcpy(out[w .. w + 4], "LIST");
        std.mem.writeInt(u32, out[w + 4 ..][0..4], 50, .little);
        @memcpy(out[w + 8 .. w + 12], "adtl");
        w += 12;
        @memcpy(out[w .. w + 4], "labl");
        std.mem.writeInt(u32, out[w + 4 ..][0..4], 16, .little);
        std.mem.writeInt(u32, out[w + 8 ..][0..4], 1, .little);
        @memcpy(out[w + 12 .. w + 24], "slice start\x00");
        w += 24;
        @memcpy(out[w .. w + 4], "labl");
        std.mem.writeInt(u32, out[w + 4 ..][0..4], 14, .little);
        std.mem.writeInt(u32, out[w + 8 ..][0..4], 2, .little);
        @memcpy(out[w + 12 .. w + 22], "slice end\x00");
        w += 22;
        std.debug.assert(w == byte_len);
    }
};
```

`copyRange` becomes `pub fn copyRange(src, dst, start_frame, n_frames, st, markers: ?Markers) !void`: after the `OutOfRange` check add `if (markers) |m| try m.validate(n_frames);`; compute `const data_len: u32 = @intCast(data_len_wide); const pad: u32 = if (markers != null) data_len & 1 else 0; const extra: u32 = if (markers != null) @intCast(Markers.byte_len) else 0;` and after `writeHeader` patch the RIFF size: `std.mem.writeInt(u32, header[4..8], 36 + data_len + pad + extra, .little);`. After the copy loop:

```zig
    if (markers) |m| {
        if (pad == 1) try out.writeStreamingAll(io, &[_]u8{0});
        var mb: [Markers.byte_len]u8 = undefined;
        m.write(&mb, o.info.rate);
        try out.writeStreamingAll(io, &mb);
    }
```

ABI: `pub const FbMarkers = extern struct { slice_start: u64, slice_end: u64 };`, `fb_checkout_export(..., subtype: c_int, markers: ?*const FbMarkers)`: build `const mk: ?wav.Markers = if (markers) |m| .{ .slice_start = m.slice_start, .slice_end = m.slice_end } else null;` and pass `mk` to `copyRange`. The RAM branch (`writeFile`) does not support markers: when `mk != null` and the audio is not yet on disk, wait for it — `s.waitJob(co)` before reading `write_state` (P13's rule at the engine edge; a `failed` state then returns `.io_error`). Header: add the struct and the parameter with `/* nullable */`. Python: `lib.fb_checkout_export.argtypes = [vp, vp, C.c_char_p, u64, u64, C.c_int, C.POINTER(FbMarkers)]`, `class FbMarkers(C.Structure): _fields_ = [("slice_start", C.c_uint64), ("slice_end", C.c_uint64)]`, `checkout_export(self, h, dst, start, n, subtype, markers=None)` passes `C.byref(FbMarkers(*markers))` or `None`. `CheckoutManager.export_range(..., markers: tuple[int, int] | None = None)` forwards it. Add the Zig ABI test:

```zig
test "fb_checkout_export with markers waits for the scratch write and emits cue" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/mk.wav", .{tmp.sub_path}) catch unreachable;
    const dst = std.fmt.bufPrintZ(&pd, ".zig-cache/tmp/{s}/mk-out.wav", .{tmp.sub_path}) catch unreachable;
    const ring = fb_ring_create(1000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    fb_ring_write(ring, &[_]f32{ 1, 2, 3, 4, 5, 6 }, 6);
    const s = fb_scratch_create(1 << 20, null) orelse return error.CreateFailed;
    defer fb_scratch_destroy(s);
    try std.testing.expectEqual(FbStatus.ok, fb_scratch_start(s));
    const co = fb_checkout_create(s, ring, 0, 6, path, null) orelse return error.CreateFailed;
    defer fb_checkout_destroy(s, co);
    const mk = FbMarkers{ .slice_start = 1, .slice_end = 3 };
    try std.testing.expectEqual(FbStatus.ok, fb_checkout_export(s, co, dst, 0, 6, 0, &mk));
    var buf: [header_len_plus]u8 = undefined;
    _ = &buf;
    var o = try wav.open(std.mem.span(dst));
    defer o.file.close(wav.io);
    try std.testing.expectEqual(@as(u64, 6), o.info.frames);
    try std.testing.expectEqual(@as(u64, 44 + 24 + wav.Markers.byte_len), try o.file.length(wav.io));
}
```

(Remove the two `buf` lines and `header_len_plus` — they are not needed; the length check is the assertion. Keep the test at 5 assertions as written minus those lines.)

- [ ] **Step 4: Run** — `203/203`; `python -m pytest tests/unit/test_scratch.py tests/unit/test_checkout.py -q` green (signatures with default `markers=None`).

- [ ] **Step 5: Mutation checks** — (a) drop the pad byte → the odd-data test reddens. (b) write `e` instead of `e - 1` for the loop end → the first test reddens (7 ≠ 8). Revert both.

- [ ] **Step 6:** `zig fmt`, commit `feat(core): wav.Markers — cue/smpl/adtl after data; fb_checkout_export takes markers`.

### Task i2: The Ableton spike (owner at the machine)

**Files:**
- Create: `tools/spike_markers.py`
- Modify (record): `docs/superpowers/specs/2026-08-30-checkout-persistence-design.md` (DAW table), #53 comment.

- [ ] **Step 1: The executor writes `tools/spike_markers.py`**

```python
"""Build the two spike files for the DAW marker test (spec PR i, Task i2):
  spike.wav  — 30 s stereo at 48 kHz with cue/smpl markers at 10-15 s
Run: python tools/spike_markers.py <out_dir>"""
import sys
from pathlib import Path

import numpy as np

from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rate, seconds = 48_000, 30
    buf = NativeAudioCircularBuffer(duration_seconds=seconds, sample_rate=rate, channels=2)
    t = np.arange(seconds * rate) / rate
    # a tone that changes pitch every 5 s so the slice is audible by ear
    tone = 0.3 * np.sin(2 * np.pi * (220 + 55 * (t // 5)) * t).astype(np.float32)
    buf.write(np.stack([tone, tone], axis=1))
    s = NativeScratch(budget_bytes=1 << 30)
    s.start()
    h = s.checkout_create(buf, 0, seconds * rate, out_dir / "spike-scratch.wav")
    s.checkout_export(h, out_dir / "spike.wav", 0, seconds * rate, "PCM_24", markers=(10 * rate, 15 * rate))
    s.checkout_destroy(h)
    s.close()
    (out_dir / "spike-scratch.wav").unlink(missing_ok=True)
    print(out_dir / "spike.wav")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
```

- [ ] **Step 2: Owner procedure (record every answer on #53)**

1. `python tools/spike_markers.py C:\spike` → `C:\spike\spike.wav`.
2. **WAV markers:** drag `spike.wav` from Explorer onto an audio track in Live. Q1: does Live show anything at 10 s / 15 s (markers, warp markers, loop brace)? Q2: does the clip's start/end equal 10/15 s? Repeat in any other DAW installed (Reaper: expect project markers). Write the answers into the spec's DAW table rows.
3. **`.alc` capture:** with the clip from step 2 on the track, set Clip Start to 10.0 s and Clip End to 15.0 s in the clip view (Sample box; switch the display to seconds), then drag the clip into the User Library in Live's browser → Live writes `spike.alc`. Change End to 16.0 s and drag again → `spike2.alc`. Copy both files out of the User Library folder (Live → Preferences → Library → User Library location).
4. `python -c "import gzip,sys; open(sys.argv[2],'wb').write(gzip.open(sys.argv[1]).read())" spike.alc spike.xml` for both; `fc /n spike.xml spike2.xml` (or a diff tool). Q3: which elements changed between the two? (Expected: something under `SampleRef`/`FileRef` for the path and two numeric elements for the start/end in seconds or samples.) Paste the differing lines on #53.
5. **`.alc` drop:** edit `spike2.xml` by hand so the end reads 12.0 s, gzip it back to `spike3.alc` (`python -c "import gzip,sys; gzip.open(sys.argv[2],'wb').write(open(sys.argv[1],'rb').read())" spike3.xml spike3.alc`), drag `spike3.alc` from Explorer into Live. Q4: does it open as a 10–12 s clip referencing `spike.wav`? Q5: drag the clip's right edge — does it extend to 30 s?
6. Result on #53 and in the spec table (Ableton row: Q1/Q2 for markers, Q4/Q5 for the container). **Task i5 ships only if Q4 and Q5 are both yes.**

### Task i3: Export span, slice minting, the two drag renderers

**Files:**
- Modify: `flashback_sampler/core/checkout.py` (`slice`)
- Modify: `flashback_sampler/core/drag_export.py`
- Modify: `flashback_sampler/app/config.py` (`drag_handle_mb`)
- Modify: `tests/unit/test_checkout.py`, `tests/unit/test_drag_export.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces: `CheckoutManager.slice(parent_id, start, n) -> Checkout` (state `saved`, `parent_id`, bins from the handle, manifest, refcount +1; waits for the parent's file, P13); `drag_export.export_span(parent_frames, slice_start, slice_end, channels, bytes_per_sample, handle_mb) -> tuple[int, int]`; `drag_export.BYTES_PER_SAMPLE = {"FLOAT": 4, "PCM_24": 3, "PCM_16": 2}`; `drag_export.DragRender(path: Path, checkout_id: str, minted: bool)`; `drag_export.render_slice_drag(manager, checkout_id, pool_dir, source_name, *, bit_depth="FLOAT", handle_mb=0.0, now=None) -> DragRender`; `drag_export.render_root_drag(manager, checkout_id, pool_dir, source_name, *, bit_depth="FLOAT", markers_at_trim=False, now=None) -> DragRender`. `render_drag_file` is deleted (its two callers move to the new pair). `config.DEFAULT_DRAG_HANDLE_MB = 200.0`, `load_drag_handle_mb()`, `save_drag_handle_mb(mb)`.

- [ ] **Step 1: Failing tests**

Append to `tests/unit/test_checkout.py`:

```python
def test_slice_references_the_parent_file_and_is_saved(scratch, tmp_path):
    mgr = _mgr(scratch, tmp_path)
    parent = mgr.create(duration_s=0.5)
    s = mgr.slice(parent.id, 100, 200)
    assert (s.parent_id, s.path, s.start_frame, s.n_frames, s.state) == (parent.id, parent.path, 100, 200, "saved")
    assert s.bins["360"].shape == (360, 2, 1)
    assert s.bins["360"][0, 0, 0] == pytest.approx(1100.0)  # min of the slice's first bin
    assert mgr.file_refcount(parent.path) == 2
    m = read_manifest(manifest_path(tmp_path, s.id))
    assert m.parent == parent.id and m.start_frame == 100
    mgr.discard(parent.id)
    assert parent.path.exists()
    audio = native.wav_read(s.path, s.start_frame, s.n_frames)
    assert audio[0, 0] == pytest.approx(1100.0)
    mgr.discard(s.id)
    assert not parent.path.exists()


def test_slice_rejects_a_span_past_the_parent_and_a_failed_parent(scratch, tmp_path, monkeypatch):
    mgr = _mgr(scratch, tmp_path)
    parent = mgr.create(duration_s=0.5)
    with pytest.raises(ValueError):
        mgr.slice(parent.id, 450, 100)
    monkeypatch.setattr(mgr, "write_state", lambda cid: "failed")
    with pytest.raises(RuntimeError, match="scratch write failed"):
        mgr.slice(parent.id, 0, 10)
```

Replace the `render_drag_file` tests in `tests/unit/test_drag_export.py` with:

```python
from flashback_sampler.core.drag_export import (
    BYTES_PER_SAMPLE, DragRender, drag_filename, export_span, render_root_drag,
    render_slice_drag, resolve_collision, sanitize_source_name,
)


@pytest.mark.parametrize("parent,s,e,handle_mb,expect", [
    (1000, 400, 500, 0.0, (400, 500)),          # budget 0 = slice only
    (1000, 400, 500, 1e9, (0, 1000)),           # budget ∞ = whole parent
    (1000, 400, 500, 300 * 4 / 2**20, (250, 650)),   # 300 extra mono float frames: 150 each side
    (1000, 50, 150, 300 * 4 / 2**20, (0, 300)),      # clamped at the start; the unused half is not moved
    (1000, 900, 950, 300 * 4 / 2**20, (750, 1000)),  # clamped at the end
    (1000, 400, 500, 50 * 4 / 2**20, (375, 525)),    # a budget smaller than the slice still adds handles; the slice is whole
    (1000, 0, 1000, 10 * 4 / 2**20, (0, 1000)),      # a slice that IS the parent is never truncated
])
def test_export_span(parent, s, e, handle_mb, expect):
    assert export_span(parent, s, e, 1, 4, handle_mb) == expect


def test_render_root_drag_exports_the_whole_clip_with_markers_at_the_trim(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    mgr.set_trim(co.id, 100, 300)
    r = render_root_drag(mgr, co.id, tmp_path / "pool", "Deck A", markers_at_trim=True, now=WHEN)
    assert r == DragRender(tmp_path / "pool" / "deck_a_20260715-130509_0.5s.wav", co.id, False)
    audio, info = read_wav(r.path)
    assert info.frames == 500 and info.subtype == "FLOAT"
    raw = r.path.read_bytes()
    assert b"cue " in raw and b"smpl" in raw
    assert mgr.get(co.id).state == "pending"  # the caller commits on drop


def test_render_root_drag_without_markers_has_no_cue(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    r = render_root_drag(mgr, co.id, tmp_path, "x", bit_depth="PCM_24", now=WHEN)
    assert b"cue " not in r.path.read_bytes()
    assert read_wav(r.path)[1].subtype == "PCM_24"


def test_render_slice_drag_mints_a_saved_slice_and_exports_the_span(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    mgr.set_trim(co.id, 200, 300)
    handles = 100 * 4 / 2**20  # 100 extra mono float frames: 50 each side
    r = render_slice_drag(mgr, co.id, tmp_path, "Deck A", handle_mb=handles, now=WHEN)
    assert r.minted and r.checkout_id != co.id
    s = mgr.get(r.checkout_id)
    assert (s.parent_id, s.start_frame, s.n_frames, s.state) == (co.id, 200, 100, "saved")
    assert r.path.name == "deck_a_20260715-130509_0.1s.wav"  # named for the slice, not the span
    audio, info = read_wav(r.path)
    assert info.frames == 200 and audio[0, 0] == pytest.approx(1150.0)  # span 150..350 = slice 200..300 + 50 each side
    assert b"cue " in r.path.read_bytes()


def test_render_slice_drag_on_an_untrimmed_clip_falls_back_to_the_root(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    r = render_slice_drag(mgr, co.id, tmp_path, "x", now=WHEN)
    assert r == DragRender(r.path, co.id, False)
    assert read_wav(r.path)[1].frames == 500


def test_render_creates_pool_dir(scratch, tmp_path):
    mgr, co = _mgr_with_checkout(scratch, tmp_path)
    pool = tmp_path / "nested" / "exports"
    r = render_root_drag(mgr, co.id, pool, "x", now=WHEN)
    assert r.path.parent == pool and r.path.exists()
```

Append to `tests/unit/test_config.py`:

```python
def test_drag_handle_mb_defaults_200_and_floors_at_zero(tmp_path):
    from flashback_sampler.app import config
    p = tmp_path / "c.json"
    assert config.load_drag_handle_mb(p) == 200.0  # on by default (best out-of-the-box UX); a tunable for constrained systems
    config.save_drag_handle_mb(0, p)
    assert config.load_drag_handle_mb(p) == 0.0
    config.save_drag_handle_mb(-1, p)
    assert config.load_drag_handle_mb(p) == 0.0
```

- [ ] **Step 2: Run to see them fail** — `AttributeError: slice`, `ImportError: export_span`.

- [ ] **Step 3: Implement**

`checkout.py`, after `create_from_abs_range`:

```python
    def slice(self, parent_id: str, start: int, n: int) -> Checkout:
        """A saved segment `(parent file, start, n)`. Waits for the
        parent's file: a slice has no RAM copy, so its bins and audio
        come from disk (plan P13). Raises when the parent's write failed."""
        parent = self.get(parent_id)
        if start < 0 or n <= 0 or start + n > parent.n_frames:
            raise ValueError(f"slice {start}+{n} is outside the parent's {parent.n_frames} frames")
        deadline = time.monotonic() + 30.0
        while True:
            ws = self.write_state(parent_id)
            if ws in ("written", "adopted"):
                break
            if ws == "failed":
                raise RuntimeError("scratch write failed for the parent; cannot slice")
            if time.monotonic() > deadline:
                raise RuntimeError("timed out waiting for the parent's scratch write")
            time.sleep(0.005)
        with self._lock:
            if len(self._checkouts) >= self._max_active:
                raise RuntimeError(f"Maximum active checkouts reached ({self._max_active})")
            handle = self._scratch.checkout_slice(parent.handle, int(start), int(n))
            sr = parent.sample_rate
            co = Checkout(
                id=uuid.uuid4().hex[:12], handle=handle, path=parent.path, created_at=time.monotonic(),
                sample_rate=sr, channels=parent.channels, n_frames=int(n), start_frame=int(parent.start_frame + start),
                abs_sample_start=parent.abs_sample_start + int(start), abs_sample_end=parent.abs_sample_start + int(start + n),
                parent_id=parent.id, state="saved",
            )
            co.bins = {str(b): self._scratch.checkout_peak_bins(handle, b) for b in BIN_COUNTS}
            self._register(co)
        return co
```

`drag_export.py` (replace `render_drag_file` and its docstring):

```python
from dataclasses import dataclass

BYTES_PER_SAMPLE = {"FLOAT": 4, "PCM_24": 3, "PCM_16": 2}


@dataclass(frozen=True)
class DragRender:
    path: Path
    checkout_id: str  # the checkout the drop commits (a minted slice, or the clip itself)
    minted: bool      # True when the render created a slice checkout


def export_span(parent_frames: int, slice_start: int, slice_end: int, channels: int, bytes_per_sample: int, handle_mb: float) -> tuple[int, int]:
    """The parent span to export around a slice: the WHOLE slice plus up
    to `handle_mb` of extra parent audio, half before and half after,
    clamped to the parent. The slice is never truncated. 0 = the slice
    alone; ∞ = the whole parent. A clamp at one edge does not move the
    other (the file just gets smaller)."""
    if handle_mb <= 0:
        return slice_start, slice_end
    half = (int(handle_mb * 2**20) // (channels * bytes_per_sample)) // 2
    return max(0, slice_start - half), min(parent_frames, slice_end + half)


def _target(pool_dir: Path | str, source_name: str, duration_s: float, now: datetime | None) -> Path:
    pool = Path(pool_dir)
    pool.mkdir(parents=True, exist_ok=True)
    return resolve_collision(pool / drag_filename(source_name, now or datetime.now(), duration_s))


def render_root_drag(manager, checkout_id: str, pool_dir, source_name: str, *, bit_depth: CheckoutSubtype = "FLOAT", markers_at_trim: bool = False, now: datetime | None = None) -> DragRender:
    """The whole checkout, optionally with markers at its trim (the
    buffer-deck drag: the root IS the segment, its trim the slice)."""
    co = manager.get(checkout_id)
    markers = None
    if markers_at_trim and co.has_trim():
        start, n = co.trim_range()
        markers = (start, start + n)
    target = _target(pool_dir, source_name, co.duration_seconds, now)
    manager.export_range(checkout_id, target, 0, co.n_frames, bit_depth, markers=markers)
    return DragRender(target, checkout_id, False)


def render_slice_drag(manager, checkout_id: str, pool_dir, source_name: str, *, bit_depth: CheckoutSubtype = "FLOAT", handle_mb: float = 0.0, now: datetime | None = None) -> DragRender:
    """The clip-deck drag of a trimmed band: mint a saved slice, export
    the whole slice plus up to handle_mb of parent audio around it, with
    markers at the slice. An untrimmed clip has no slice to mint: the
    whole clip goes."""
    co = manager.get(checkout_id)
    if not co.has_trim():
        return render_root_drag(manager, checkout_id, pool_dir, source_name, bit_depth=bit_depth, now=now)
    start, n = co.trim_range()
    s = manager.slice(checkout_id, start, n)
    lo, hi = export_span(co.n_frames, start, start + n, co.channels, BYTES_PER_SAMPLE[bit_depth], handle_mb)
    target = _target(pool_dir, source_name, n / co.sample_rate, now)
    manager.export_range(checkout_id, target, lo, hi - lo, bit_depth, markers=(start - lo, start + n - lo))
    return DragRender(target, s.id, True)
```

`config.py`: `DRAG_HANDLE_MB_KEY = "drag_handle_mb"`, `DEFAULT_DRAG_HANDLE_MB = 200.0`, `load_drag_handle_mb(path=None) -> float` (floor 0, default on garbage, same shape as `load_checkout_cache_mb`), `save_drag_handle_mb(mb, path=None)`.

- [ ] **Step 4: Run** — the three files green; then `grep -rn "render_drag_file" flashback_sampler tests` → only `turntable_window.py` (fixed in Task i4).

- [ ] **Step 5: Mutation checks** — (a) in `export_span` drop the `min(parent_frames, …)` clamp → the "clamped at the end" case reddens (`(750, 1100)`). (b) in `render_slice_drag` pass markers `(start, start + n)` un-rebased → the slice-drag test's raw-bytes assertion cannot see it; add `assert struct.unpack_from("<I", raw, raw.index(b"cue ") + 8 + 4 + 20)[0] == 50` (slice start − lo = 200 − 150). Reddens under the mutation. Revert.

- [ ] **Step 6:** commit `feat: slices as references (CheckoutManager.slice), export_span, root/slice drag renderers, drag_handle_mb`.

### Task i4: Window — slice drag, buffer drag ± half, preference row

**Files:**
- Modify: `flashback_sampler/app/turntable_window.py` (`_render_for_drag`, `_drag_current_clip`, `_complete_drag`, `_on_buffer_drag_out`, preferences wiring)
- Modify: `flashback_sampler/app/preferences_dialog.py`
- Modify: `tests/unit/test_turntable_window.py`, `tests/unit/test_preferences_dialog.py`

- [ ] **Step 1: Failing tests (append to `tests/unit/test_turntable_window.py`)**

```python
def test_clip_drag_of_a_trimmed_band_mints_a_slice_and_keeps_it_on_cancel_only_if_accepted(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.checkout_manager
        co = mgr.create(duration_s=0.5)
        win._refresh_clip_side(auto_select_newest=True)
        mgr.set_trim(co.id, co.n_frames // 4, co.n_frames // 2)
        win._export_pool_dir = tmp_path
        win._drag_handle_mb = 0.0
        monkeypatch.setattr("flashback_sampler.app.turntable_window.perform_file_drag", lambda w, p: False)
        win._on_clip_drag_out(0.25, 0.5)
        assert [c.id for c in mgr.list()] == [co.id]  # cancelled: the slice is gone
        assert list(tmp_path.glob("*.wav")) == []
        monkeypatch.setattr("flashback_sampler.app.turntable_window.perform_file_drag", lambda w, p: True)
        win._on_clip_drag_out(0.25, 0.5)
        cos = mgr.list()
        assert len(cos) == 2 and cos[1].parent_id == co.id and cos[1].state == "saved"
        assert mgr.get(co.id).state == "pending"  # the parent is untouched
        assert len(list(tmp_path.glob("*.wav"))) == 1
    finally:
        win.close()


def test_buffer_drag_pulls_the_selection_plus_handles_and_marks_the_trim(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        sr = state.buffer.sample_rate
        win._export_pool_dir = tmp_path
        win._drag_handle_mb = (sr // 2) * state.channels * 4 / 2**20  # handles = 0.5 s of frames in total
        monkeypatch.setattr("flashback_sampler.app.turntable_window.perform_file_drag", lambda w, p: True)
        win.buffer_panel.waveform.manualSelectionChanged.emit(0.4, 0.6)  # 0.2 s selected in the middle
        win._update_selection_display()
        win._on_buffer_drag_out(0.4, 0.6)
        co = state.checkout_manager.list()[0]
        # root = selection ± 0.25 s (half the handle budget each side), trim = the selection
        assert abs(co.n_frames - int(0.7 * sr)) <= 2
        assert abs(co.trim_in_samples - int(0.25 * sr)) <= 2 and abs((co.trim_out_samples - co.trim_in_samples) - int(0.2 * sr)) <= 2
        assert co.state == "saved"
        raw = next(tmp_path.glob("*.wav")).read_bytes()
        assert b"cue " in raw
    finally:
        win.close()
```

`tests/unit/test_preferences_dialog.py`: a `drag_handle_mb` row test mirroring the export bit-depth test: `PreferencesDialog(..., drag_handle_mb=200.0, on_drag_handle_mb_changed=seen.append)`, `dlg.drag_cap_spin.setValue(50)`, `assert seen == [50.0]`.

- [ ] **Step 2: Run to see them fail** — `_drag_handle_mb` missing; `render_drag_file` import error.

- [ ] **Step 3: Window edits**

Imports: replace `render_drag_file` with `render_root_drag, render_slice_drag`; add `load_drag_handle_mb, save_drag_handle_mb` to the config import. In `__init__` next to `_export_bit_depth`: `self._drag_handle_mb: float = load_drag_handle_mb()`.

Replace `_render_for_drag`:

```python
    def _render_for_drag(self, slot, co, *, trimmed: bool, markers_at_trim: bool = False):
        """Render for an OS drag; returns a DragRender or None on failure
        (already reported). A trimmed clip-deck drag mints a slice."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if trimmed:
                return render_slice_drag(slot.checkout_manager, co.id, self._export_pool_dir, slot.name,
                                         bit_depth=self._export_bit_depth, handle_mb=self._drag_handle_mb)
            return render_root_drag(slot.checkout_manager, co.id, self._export_pool_dir, slot.name,
                                    bit_depth=self._export_bit_depth, markers_at_trim=markers_at_trim)
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))
            return None
        finally:
            QApplication.restoreOverrideCursor()
```

`_drag_current_clip(trimmed)`: `r = self._render_for_drag(slot, co, trimmed=trimmed)`; `self._complete_drag(slot, r, self.clip_panel.waveform, discard_on_cancel=r.minted, auto_select_newest=r.minted)`.

`_complete_drag(self, slot, render, source_widget, *, discard_on_cancel, auto_select_newest)`: on accept `mark_saved(render.checkout_id)` (a minted slice is already saved; harmless), refresh, status; on cancel `if discard_on_cancel: slot.checkout_manager.discard(render.checkout_id)` then `render.path.unlink(missing_ok=True)`.

`_on_buffer_drag_out`: after `sel_abs` resolves:

```python
        sel_start, sel_end = sel_abs
        buf = slot.buffer
        total = buf.total_written
        oldest = max(0, total - buf.buffer_size)
        lo, hi = export_span(total - oldest, sel_start - oldest, sel_end - oldest, buf.channels,
                             BYTES_PER_SAMPLE[self._export_bit_depth], self._drag_handle_mb)
        lo, hi = lo + oldest, hi + oldest
```

then the existing create loop uses `create_from_abs_range(lo, hi)`; after it succeeds: `slot.checkout_manager.set_trim(co.id, sel_start - lo, sel_end - lo)`; render with `self._render_for_drag(slot, co, trimmed=False, markers_at_trim=True)`; `_complete_drag(slot, r, self.buffer_panel.waveform, discard_on_cancel=True, auto_select_newest=True)`. Import `export_span, BYTES_PER_SAMPLE` from `drag_export`. Update the method's docstring: "the root pulls selection ± handles (Preferences → Drag-out handles); the selection becomes its trim, marked in the exported file".

Preferences: `PreferencesDialog(drag_handle_mb: float = 200.0, on_drag_handle_mb_changed=None)`: under Export add `QLabel("Drag-out handles (tunable): add up to")`, `self.drag_cap_spin = QSpinBox()` range 0–100000, suffix " MB", value `int(drag_handle_mb)`, `valueChanged` → `on_drag_handle_mb_changed(float(v))`, hint "of extra parent audio before and after a dragged slice, with markers at the slice, so the DAW can recover more than you sliced. The slice itself is always exported whole. 0 = slice only — use it on constrained systems: the handles also size the buffer-deck root's RAM copy." Window: pass `drag_handle_mb=self._drag_handle_mb, on_drag_handle_mb_changed=self._set_drag_handle_mb` and

```python
    def _set_drag_handle_mb(self, mb: float) -> None:
        self._drag_handle_mb = float(mb)
        save_drag_handle_mb(mb)
```

- [ ] **Step 4: Run the full suite** — green. Then the app smoke: trim a clip, drag the band into Explorer (file appears, deck shows the slice), drag from the buffer deck (one entry, trim = selection), cancel a drag (nothing left behind).

- [ ] **Step 5:** commit `feat(app): trimmed clip drag mints a slice, buffer drag pulls handles, drag_handle_mb preference`.

### Task i5 (conditional — only if Task i2 answered Q4 and Q5 yes): `.alc` sidecar

**Files:**
- Create: `flashback_sampler/core/alc.py`, `tests/unit/test_alc.py`
- Modify: `flashback_sampler/app/config.py` (`drag_alc_sidecar` bool pref, default False), `preferences_dialog.py` (checkbox), `drag_out.py` (`build_file_drag_mime` accepts a list of paths), `turntable_window.py` (offer `[wav, alc]` when the pref is on)

- [ ] **Step 1:** Commit the spike's captured `spike.xml` (the decompressed `.alc`, with the absolute path replaced by `SAMPLE_PATH`) as `flashback_sampler/core/alc_template.xml`. Identify from the spike's diff the exact elements for the sample path(s) and the start/end values and their unit (seconds vs samples). Write them into the module docstring of `alc.py` as the contract.
- [ ] **Step 2:** `alc.py`: `def write_alc(target: Path, wav_path: Path, slice_start_s: float, slice_end_s: float) -> Path` — load the template, substitute the identified elements with `str(wav_path)` (and the relative path element, if the diff showed one, with `wav_path.name`), the start/end in the unit the diff showed, gzip to `target`. Test: `gzip.open(out).read()` parses with `xml.etree.ElementTree`, the path element equals the WAV path, the start/end elements equal the values, and a round trip of the template without substitution is byte-identical to `spike.alc`'s decompressed content.
- [ ] **Step 3:** Pref + dialog checkbox "Also offer an Ableton Live Clip (.alc) on drag"; `build_file_drag_mime(paths: list[Path])` sets all URLs; the window writes the `.alc` next to the WAV in the pool when the pref is on and offers both. Test the mime carries two URLs.
- [ ] **Step 4:** Owner: drag from the app into Live; confirm the clip opens at the slice and the edge drags out. Record on #53.

If the spike answered no, this task is skipped and the spec's Ableton row says so; the preference never exists.

### Task i6: Gates, docs, PR i, epic close

- [ ] **Step 1:** Docs: README/PLATFORM "Drag-out" paragraph: slices, handles preference, markers, what each DAW does with them (from the spec table).
- [ ] **Step 2:** Full gate (Task g7 Step 1 block). Expected: Zig 203 (+ i5's tests, none in Zig); pytest ≈ 558.
- [ ] **Step 3:** Whole-branch review — one combined inline `/simplify` + `/code-review` at **medium**.
- [ ] **Step 4:** PR `feat/slices-handles` → `dev`; body: `Closes #NN`; the drag-shape table; the spike result; "Zig concepts": serialising nested RIFF chunks with fixed byte budgets, `?T` optional parameters at the ABI as nullable pointers, `std.debug.assert` as a layout invariant.
- [ ] **Step 5:** After merge: tick the box on #53; comment the final counts and the measurement/spike results; close #53 when all three boxes are ticked. `_ToRemove/` review: none expected (no whole-file deletions in this epic); confirm with `ls _ToRemove` and say so.

---

## Self-review record (2026-08-30)

- **Spec coverage:** Goal bullets → g (reader), h (writer, cache, adoption, no numpy audio), i (slices, handles). Every Decisions-table row has a task: Writer h3, RAM copy h1, Cache h4, budget default h11, scratch dir h7/h9, adoption h9, lifetime h8 (`discard`/`close`), bins in manifest h7/h8, `ClipSource` h2, export h6/i1, slices i3, handles i3/i4, DAW spike i2/i5, count cap h10 (P5), PR split g/h/i. Error-handling table: disk full h3/h4 (`failed` stays resident, unevictable) + h10 status message — **gap:** the status-bar message "Scratch write failed: <name>" has no task step; added to Task h10 Step 3 item 6 as: in `_display_clip_in_panel`, if `mgr.write_state(co.id) == "failed"` show `self.statusBar().showMessage(f"Scratch write failed: {co.id[:6].upper()} (clip kept in RAM)", 6000)`. Quit-with-jobs-queued: `Scratch.stop` drains (h3); the ">500 ms status message" is UI polish deferred to the hand-off (recorded, not built — the drain blocks `shutdown`, which is the spec's behaviour).
- **Placeholders:** none found by `grep -n "TBD\|TODO\|similar to\|appropriate\|fill in"`. Task i5 is conditional on a real-world unknown and says exactly what to do in each branch.
- **Type consistency:** `Checkout.WriteState` wire values 0–4 match `native.WRITE_STATES` and `FbCheckoutInfo.write_state`; `Playback.bind(src, rate, channels)` used identically in h2, h6, i1; `copyRange` has 5 args in g and 6 in i (P1, both callers updated in i1); `export_range(..., markers=None)` introduced in i1 and used in i3; `DragRender(path, checkout_id, minted)` in i3/i4; `from_quality_preset(..., scratch=, scratch_dir=)` in h8/h9; `CheckoutManager(buffer, scratch, scratch_dir, slot_name, max_active_checkouts)` in h8 and every fixture.
- **Counts** are targets; the gate is "the count rose" (Global Constraints).
