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
    const chans: u64 = self.channels;
    const n: u64 = interleaved.len / chans;
    if (n == 0) return;
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

/// out.len = n_bins * channels. n_samples_req = 0 → all available.
/// bin_span_frames = 0 → derived from window (n_samples / n_bins).
/// Slots whose generation tag falls inside [abs_start, abs_start+n)
/// scatter-add ss and count into their bin; out = sqrt(ss/count).
/// Mirror of buffer.py get_summary_bins (buffer.py:421); n_avail clamps
/// against `capacity_frames` (the RING's readable window), matching
/// Python's clamp to `buffer_size` exactly.
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
    std.debug.assert(chans <= 2);
    var bin_ss: [4096 * 2]f64 = undefined; // max bins * max channels
    var bin_cnt: [4096]u64 = undefined;
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
