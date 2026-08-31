//! Pre-decimated summary ring: slots of `slot_frames` frames, each
//! storing (min, max, sum-of-squares, count) per channel, keyed by the
//! absolute index of the slot's first frame (its GENERATION tag — the
//! same trick the seqlock uses, applied per-slot). A slot whose tag
//! doesn't match the incoming span's generation is overwritten, not
//! accumulated. Poisoning every tag to -1 is how flush invalidates the
//! whole summary in O(n_slots) without touching audio data.
const std = @import("std");

const Summary = @This();

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
gen: std.atomic.Value(u64), // seqlock: odd = a writer is mid-update/poison

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
        .gen = std.atomic.Value(u64).init(0),
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
    // Seqlock: bump to odd before mutating, back to even after. A reader
    // (rmsBins) that samples gen while it's odd knows a write is in
    // flight and retries — the writer itself never waits on anyone.
    //
    // The pre-bump is acq_rel, not release: release only orders writes
    // that precede it in program order. The slot writes below come
    // AFTER it, so release alone would let them hoist above the odd
    // bump on a weakly-ordered target (aarch64) — acq_rel's acquire
    // side blocks that.
    _ = self.gen.fetchAdd(1, .acq_rel);
    defer _ = self.gen.fetchAdd(1, .release);
    @memset(self.slot_abs, -1);
}

/// The largest `n_bins` `rmsBins` will accept — bounds its two
/// allocation-free stack scratch arrays below. Public so Task 6's ABI
/// guard (`fb_ring_summary_bins`) can reject an oversized `n_bins`
/// BEFORE calling in, using this same constant, rather than duplicating
/// `4096` as an unlinked magic number that could silently desync from
/// this one if the scratch-array bound ever changed.
pub const max_bins: usize = 4096;

/// Folds one written chunk into the frozen slots. `interleaved` is the
/// PRE-gain input; gain is re-applied here (a block is ~1k frames — the
/// extra multiply is nothing, and it keeps write()'s fast path free of
/// a second pass). Runs on the audio thread: no locks, no allocation.
pub fn update(self: *Summary, interleaved: []const f32, gain: f32, start_abs: u64) void {
    const chans: u64 = self.channels;
    const n: u64 = interleaved.len / chans;
    // Checked BEFORE the seqlock bump below: a no-op write must not
    // force a reader into a retry it gains nothing from — nothing is
    // mutated on this path, so there is no torn state to protect a
    // reader against.
    if (n == 0) return;
    // Seqlock: odd gen = "being written". Readers (rmsBins, on the UI
    // thread) snapshot gen before and after; a mismatch or an odd value
    // means retry. The writer never waits — same discipline as Ring.
    //
    // The pre-bump is acq_rel, not release: release only orders writes
    // that precede it in program order. The slot writes below come
    // AFTER it, so release alone would let them hoist above the odd
    // bump on a weakly-ordered target (aarch64) — acq_rel's acquire
    // side blocks that.
    _ = self.gen.fetchAdd(1, .acq_rel);
    defer _ = self.gen.fetchAdd(1, .release);
    const slot_first = start_abs / self.slot_frames;
    const slot_last = (start_abs + n - 1) / self.slot_frames;
    var s_global = slot_first;
    while (s_global <= slot_last) : (s_global += 1) {
        const slot_idx: usize = @intCast(s_global % self.n_slots);
        const slot_start_abs: i64 = @intCast(s_global * self.slot_frames);
        const slot_start_u: u64 = s_global * self.slot_frames;
        const f_from: u64 = if (slot_start_u > start_abs) slot_start_u - start_abs else 0;
        const f_to: u64 = @min(n, slot_start_u + self.slot_frames - start_abs);
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

/// Seqlock read: run `ctx.run()` until `gen` is stable and even, at most
/// 4 attempts, then hand back the last result. Never blocks the writer.
fn seqRead(gen: *const std.atomic.Value(u64), ctx: anytype) void {
    var attempt: u8 = 0;
    while (true) : (attempt += 1) {
        const g0 = gen.load(.acquire);
        ctx.run();
        const g1 = gen.load(.acquire);
        if ((g0 & 1) == 0 and g0 == g1) return;
        if (attempt >= 3) return;
    }
}

/// out.len = n_bins * channels. n_samples_req = 0 → all available.
/// bin_span_frames = 0 → derived from window (n_samples / n_bins).
/// Slots whose generation tag falls inside [abs_start, abs_start+n)
/// scatter-add ss and count into their bin; out = sqrt(ss/count).
/// Aggregates frozen slots into display bins; n_avail clamps against
/// `capacity_frames` (the ring's readable window).
///
/// THREADING: called from a control/UI thread (Task 6 exposes this as
/// `fb_ring_summary_bins`, polled at ~30 Hz), reading `Summary` fields
/// that `update` — running concurrently on the audio thread — mutates.
/// `Summary` is a seqlock, the same discipline as `Ring.read`/`write`:
/// `update` and `poison` bump `gen` to odd before mutating and back to
/// even after; this function snapshots `gen` before and after computing
/// into its scratch (via `seqRead`), and retries — 4 attempts total (1
/// plus up to 3 retries) — if `gen` was odd or changed in between. This
/// is BEST EFFORT, not a guarantee: if every attempt still lands on a
/// moving or odd generation, the last computation is returned into
/// `out` anyway, possibly torn — `seqRead` never blocks waiting for a
/// clean one, the same way the writer never waits on a reader. The
/// parity scheme assumes a single writer; `Ring.writer_active` is what
/// enforces that between `update` and `flushNow`'s `poison`: every
/// writer thread's owner registers it (`Capture`, `Mixer` — see
/// `Ring.flush`'s OWNERSHIP note), so no unregistered writer of a ring
/// remains.
///
/// Residual gap: the second acquire load in `seqRead` does not force
/// the payload read above it to complete first — the same gap
/// `Ring.read`'s footnote documents in full. x86-64 TSO closes it in
/// practice; a weakly-ordered target relies on the bounded retry, not
/// a proven fence, for the same reason Ring's stress test exists.
///
/// STACK: allocates ~96 KiB of scratch on the caller's stack (`bin_ss`:
/// max_bins * 2 channels * 8 bytes = 64 KiB, plus `bin_cnt`: max_bins * 8
/// bytes = 32 KiB) — unsafe to call from a thread with a small stack.
/// The ctypes host's control/UI thread has an ordinary OS-default stack,
/// so this is a non-issue there, but a future non-Python host (this is a
/// C ABI precisely so other hosts can link it) with a constrained-stack
/// thread must account for this before calling in.
pub fn rmsBins(self: *const Summary, total_written: u64, n_samples_req: u64, bin_span_frames: u64, out: []f32) void {
    const Ctx = struct {
        summary: *const Summary,
        total_written: u64,
        n_samples_req: u64,
        bin_span_frames: u64,
        out: []f32,
        fn run(c: @This()) void {
            c.summary.rmsBinsOnce(c.total_written, c.n_samples_req, c.bin_span_frames, c.out);
        }
    };
    seqRead(&self.gen, Ctx{
        .summary = self,
        .total_written = total_written,
        .n_samples_req = n_samples_req,
        .bin_span_frames = bin_span_frames,
        .out = out,
    });
}

fn rmsBinsOnce(self: *const Summary, total_written: u64, n_samples_req: u64, bin_span_frames: u64, out: []f32) void {
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

    // Two fixed-size stack scratch arrays, sized to the asserted maximum
    // (never a heap allocation): `bin_ss`/`bin_cnt` accumulate ss (f64,
    // for precision) and count per bin across a first pass over slots
    // (n_slots is small: capacity/slot_frames). `out` itself is only
    // written in the second pass below, once each bin's final sqrt(ss/
    // count) is known — allocation-free by bounding n_bins: callers ask
    // for display bins (≤ max_bins). Assert it.
    std.debug.assert(n_bins <= max_bins);
    std.debug.assert(chans <= 2);
    var bin_ss: [max_bins * 2]f64 = undefined; // max bins * max channels
    var bin_cnt: [max_bins]u64 = undefined;
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

test "rmsBins window clamp uses true capacity, not n_slots*slot_frames" {
    // capacity=13, slot_frames=4 -> n_slots = 13/4 = 3 (floor), so
    // n_slots*slot_frames = 12 UNDER-counts the ring's true 13-frame
    // capacity — the exact non-slot-aligned case the brief's comment on
    // `capacity_frames` warns about (48000/4096 isn't aligned either).
    // Only slot 0 (tag=0) is ever written, so it sits right at the low
    // edge of the window — the one place the two possible clamp values
    // produce different behavior (see below).
    var s = try Summary.init(std.testing.allocator, 13, 4, 1);
    defer s.deinit();
    try std.testing.expectEqual(@as(u64, 3), s.n_slots);
    try std.testing.expectEqual(@as(u64, 13), s.capacity_frames); // NOT n_slots*slot_frames (12)
    s.update(&[_]f32{ 2, 2, 2, 2 }, 1.0, 0); // slot 0, tag=0, ss=16, count=4 -> RMS=2.0

    var out: [1]f32 = undefined; // 1 bin, mono
    // total_written is a literal 13 here (Summary doesn't validate it
    // against real write history) specifically to probe the clamp:
    // n_avail = min(13, capacity_frames). With the CORRECT capacity_frames
    // (13): n_avail=13, n_samples=13, abs_start=0 -> slot 0's tag (0)
    // sits exactly at the window's low edge and IS included -> RMS 2.0.
    // With the BUGGY n_slots*slot_frames (12): n_avail=12, n_samples=12,
    // abs_start=1 -> slot 0's tag (0) is now BELOW the window
    // (0 < abs_start=1) and is excluded -> out stays 0. This is the one
    // slot/window combination where the two clamp values disagree.
    s.rmsBins(13, 0, 0, &out);
    try std.testing.expectApproxEqAbs(@as(f32, 2.0), out[0], 1e-6);
}

test "rmsBins with n_bins exceeding the number of populated slots leaves the rest at zero" {
    var s = try Summary.init(std.testing.allocator, 16, 4, 1); // 4 slots total
    defer s.deinit();
    s.update(&[_]f32{ 0.5, 0.5, 0.5, 0.5 }, 1.0, 0); // slot 0 only
    s.update(&[_]f32{ 1, 1, 1, 1 }, 1.0, 4); // slot 1 only
    // Slots 2 and 3 are never written — still poisoned (tag == -1).
    var out: [4]f32 = undefined; // 4 bins requested, only 2 slots ever populated
    s.rmsBins(16, 0, 0, &out); // full 16-frame window, bin_span = 16/4 = 4 (== slot_frames)
    try std.testing.expectApproxEqAbs(@as(f32, 0.5), out[0], 1e-6);
    try std.testing.expectApproxEqAbs(@as(f32, 1.0), out[1], 1e-6);
    try std.testing.expectEqual(@as(f32, 0), out[2]);
    try std.testing.expectEqual(@as(f32, 0), out[3]);
}

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
    // The bounded-retry, never-spins-forever property for a stuck-odd
    // generation is pinned separately, by the two seqRead tests below —
    // this test's own job is only the gen-bumping arithmetic above.
}

test "rmsBins after one update returns that update's value (rmsBinsOnce regression guard)" {
    var s = try Summary.init(std.testing.allocator, 4096 * 2, 4096, 1);
    defer s.deinit();
    const a = [_]f32{0.5} ** 4096;
    s.update(&a, 1.0, 0);
    var out: [1]f32 = undefined;
    s.rmsBins(4096, 4096, 4096, &out);
    try std.testing.expectApproxEqAbs(@as(f32, 0.5), out[0], 1e-4);
}

test "seqRead retries when the generation moves during the read" {
    var g = std.atomic.Value(u64).init(2);
    const Probe = struct {
        gen: *std.atomic.Value(u64),
        calls: *u32,
        fn run(p: @This()) void {
            p.calls.* += 1;
            if (p.calls.* == 1) _ = p.gen.fetchAdd(2, .release); // writer lands mid-read
        }
    };
    var calls: u32 = 0;
    seqRead(&g, Probe{ .gen = &g, .calls = &calls });
    try std.testing.expectEqual(@as(u32, 2), calls); // pins g0 == g1
}

test "seqRead treats an odd generation as mid-write and exhausts its bounded attempts" {
    var g = std.atomic.Value(u64).init(7); // stuck odd, never changes
    const Probe = struct {
        calls: *u32,
        fn run(p: @This()) void {
            p.calls.* += 1;
        }
    };
    var calls: u32 = 0;
    seqRead(&g, Probe{ .calls = &calls });
    try std.testing.expectEqual(@as(u32, 4), calls); // pins parity AND the bound
}
