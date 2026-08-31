//! peaks.zig — the ONE min/max bin reducer. Three callers, one bin-edge
//! rule: Ring.peakBins (live ring, seqlock), peakBinsFlat (a checkout's
//! RAM copy), peakBinsFile (a scratch file, streamed). Bin edges follow
//! numpy.linspace's integer cast: edge_i = trunc(float(i) * step),
//! last edge = n. Keep the multiply order — `i * n / n_bins` rounds
//! differently by one frame on some (n, n_bins) pairs (case G below).
const std = @import("std");
const wav = @import("wav.zig");

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
    // `start_frame + n_frames` can overflow-trap in ReleaseSafe on a
    // hostile start_frame; the subtraction form below cannot.
    if (start_frame > info.frames or n_frames > info.frames - start_frame) return error.OutOfRange;
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
    var from_file: [300 * 2]PeakBin = undefined;
    var from_flat: [300 * 2]PeakBin = undefined;
    try peakBinsFile(o.file, o.info, 0, n, 300, &from_file);
    peakBinsFlat(&frames, 2, 300, &from_flat);
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
