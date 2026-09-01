//! C ABI shim. Thin and boring by design: translate error unions to
//! status codes, pointers+lengths to slices, and NOTHING else. All
//! logic lives in Ring/Summary/wav — this file must stay liftable-host
//! plumbing. `export fn` gives the symbol an unmangled C name in the
//! shared library.
const std = @import("std");
const Ring = @import("Ring.zig");
const Summary = @import("Summary.zig");
const wav = @import("wav.zig");
const peaks = @import("peaks.zig");
const Capture = @import("Capture.zig");
const Backend = @import("Backend.zig");
const Mixer = @import("Mixer.zig");
const Playback = @import("Playback.zig");
const Checkout = @import("Checkout.zig");
const Scratch = @import("Scratch.zig");
const mem = @import("mem.zig");
const builtin = @import("builtin");

// One allocator instance for every ABI-created object. smp_allocator is
// std's thread-safe general-purpose choice, confirmed present in the
// pinned 0.16.0 std (lib/std/heap.zig) — no libc-linked c_allocator
// fallback needed here. Thread-safe matters because fb_ring_create /
// fb_ring_destroy may be called from whatever thread the Python host's
// GC or control code runs on, not just one fixed thread.
const allocator = std.heap.smp_allocator;

pub const FbStatus = enum(c_int) {
    ok = 0,
    overwritten = 1,
    out_of_range = 2,
    io_error = 3,
    invalid_arg = 4,
    out_of_memory = 5,
};

// fb_wav_write serialises through wav.write_mutex — see wav.zig.

test "abi round-trip: create, write, read, destroy" {
    const ring = fb_ring_create(48_000, 2, 1.0, null) orelse return error.CreateFailed;
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

test "fb_ring_storage_frames is capacity plus the guard band, distinct from fb_ring_capacity" {
    const ring = fb_ring_create(8, 1, 1.0, null) orelse return error.CreateFailed; // capacity == 8
    defer fb_ring_destroy(ring);
    try std.testing.expectEqual(@as(u64, 8), fb_ring_capacity(ring));
    try std.testing.expectEqual(@as(u64, 8) + Ring.max_write_frames, fb_ring_storage_frames(ring));
}

test "fb_ring_summary_bins rejects n_bins == 0" {
    const ring = fb_ring_create(48_000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    var out: [1]f32 = undefined;
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_ring_summary_bins(ring, 0, 0, 0, &out));
}

test "fb_ring_summary_bins rejects n_bins > Summary.max_bins" {
    const ring = fb_ring_create(48_000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    var out: [Summary.max_bins + 1]f32 = undefined;
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_ring_summary_bins(ring, Summary.max_bins + 1, 0, 0, &out));
}

test "fb_ring_summary_bins accepts n_bins == Summary.max_bins, the inclusive boundary" {
    // Pins the guard's `>` (not `>=`) — a mutation to `>=` would reject
    // this exact value and this test would catch it going red.
    const ring = fb_ring_create(48_000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    var out: [Summary.max_bins]f32 = undefined;
    try std.testing.expectEqual(FbStatus.ok, fb_ring_summary_bins(ring, Summary.max_bins, 0, 0, &out));
}

test "fb_wav_write rejects invalid rate/channels/subtype without touching disk" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&path_buf, ".zig-cache/tmp/{s}/never.wav", .{tmp.sub_path}) catch unreachable;
    const in = [_]f32{ 0.1, -0.1 };
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_write(path, &in, 1, 0, 2, 0)); // rate == 0
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_write(path, &in, 1, 48_000, 0, 0)); // channels == 0
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_write(path, &in, 1, 48_000, 2, 3)); // subtype out of range
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_write(path, &in, 1, 48_000, 2, -1)); // subtype negative
}

// The five-clause guard these tests exercise now lives in `Ring.init`
// (issue #21) — fb_ring_create is a pass-through, so these tests reach
// it indirectly: fb_ring_create calls Ring.init, and its `catch` turns
// any error.InvalidArgument into null. Each clause still gets its own
// isolated test (three-of-five valid, one under test) so a fully
// deletable or half-deletable guard shows up immediately as a specific
// red test, not a green suite that never exercised the boundary at all.
test "fb_ring_create rejects rate == 0" {
    try std.testing.expectEqual(@as(?*Ring, null), fb_ring_create(0, 2, 1.0, null));
}

test "fb_ring_create rejects channels == 0" {
    try std.testing.expectEqual(@as(?*Ring, null), fb_ring_create(48_000, 0, 1.0, null));
}

test "fb_ring_create rejects channels == 3" {
    try std.testing.expectEqual(@as(?*Ring, null), fb_ring_create(48_000, 3, 1.0, null));
}

test "fb_ring_create rejects seconds <= 0" {
    try std.testing.expectEqual(@as(?*Ring, null), fb_ring_create(48_000, 2, 0.0, null));
}

// NaN and +Infinity both fail `seconds <= 0` (IEEE 754: any comparison
// against NaN is false; +Infinity compares greater than 0), so neither
// is caught by that clause alone — they need their own, separate
// isFinite check. That check lives in Ring.init's guard now (issue
// #21), ahead of `@intFromFloat(config.seconds * sample_rate)`, and
// intercepts both values before @intFromFloat ever runs — so the
// documented-illegal-behavior process abort that a non-finite float
// would otherwise cause there can no longer happen via fb_ring_create.
// fb_ring_create is a C boundary fed straight from ctypes' c_double —
// Python-side float() conversions can hand across NaN with no
// complaint, so this is directly reachable, not a theoretical input;
// these two tests are what pin the guard against it.
test "fb_ring_create rejects NaN seconds (does not satisfy seconds <= 0)" {
    try std.testing.expectEqual(@as(?*Ring, null), fb_ring_create(48_000, 2, std.math.nan(f64), null));
}

test "fb_ring_create rejects +Infinity seconds (does not satisfy seconds <= 0)" {
    try std.testing.expectEqual(@as(?*Ring, null), fb_ring_create(48_000, 2, std.math.inf(f64), null));
}

test "fb_wav_write rejects a channel count above max_channels instead of trapping in writeHeader" {
    // subtype 0 (float32, 4 bytes/sample): writeHeader's block_align =
    // @intCast(bps * channels) = 4 * 20000 = 80000, which overflows a
    // u16 (max 65535) and panics before this guard exists — see the
    // report for the captured panic line.
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&path_buf, ".zig-cache/tmp/{s}/never2.wav", .{tmp.sub_path}) catch unreachable;
    const in = [_]f32{0.1};
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_write(path, &in, 1, 48_000, 20000, 0));
    // The boundary case: 16384 channels still overflows for float32
    // (4 * 16384 = 65536 > u16 max 65535) even though it is exactly
    // read_chunk_bytes / 4 — the OFF-BY-ONE a naive cap would miss.
    // wav.max_channels must be 16383, not 16384, to reject this.
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_write(path, &in, 1, 48_000, 16384, 0));
}

test "fb_wav_write round-trips a real file" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&path_buf, ".zig-cache/tmp/{s}/abi_roundtrip.wav", .{tmp.sub_path}) catch unreachable;
    const in = [_]f32{ 0.25, -0.25, 0.5, -0.5 }; // 2 stereo frames
    try std.testing.expectEqual(FbStatus.ok, fb_wav_write(path, &in, 2, 48_000, 2, 0));
    var buf: [wav.header_len + 16]u8 = undefined;
    const got = try tmp.dir.readFile(std.testing.io, "abi_roundtrip.wav", &buf);
    try std.testing.expectEqual(@as(usize, wav.header_len + 16), got.len);
    try std.testing.expectEqualSlices(u8, std.mem.sliceAsBytes(&in), got[wav.header_len..]);
}

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

/// Physical memory for the host's footprint check (#41): total and
/// currently available bytes, 0 where the platform cannot say.
export fn fb_mem_info(out: *mem.Info) void {
    out.* = mem.query();
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

// The PHYSICAL frame count backing fb_ring_storage — capacity plus the
// guard band (see Ring.zig's struct-level comment and the note above
// Ring.read). A host building a zero-copy numpy view over
// fb_ring_storage must shape that view with THIS value, and any write
// position it derives via modulo must wrap at THIS value too — using
// fb_ring_capacity for either silently corrupts any host that walks the
// raw buffer directly (the Python host no longer does — peaks come from
// fb_ring_peak_bins). fb_ring_capacity is still the right
// call for "how much audio can I get back" (get_latest-style clamping):
// the two exports answer different questions and neither substitutes
// for the other.
export fn fb_ring_storage_frames(ring: *const Ring) u64 {
    return ring.storage_frames;
}

export fn fb_ring_storage(ring: *const Ring) [*]const f32 {
    // Zero-copy view for the Python host's visualization readers. The
    // pointer is valid until fb_ring_destroy; the host does its own
    // seqlock verify against fb_ring_total_written (see native.py). The
    // view this pointer backs must be shaped by fb_ring_storage_frames,
    // NOT fb_ring_capacity — see the comment on that export.
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
    // Summary.max_bins is the SAME constant Summary.rmsBins sizes its own
    // scratch arrays from — reusing it here (rather than a duplicated
    // literal 4096) means a future change to that bound can't silently
    // desync this guard from what rmsBins actually asserts.
    if (n_bins == 0 or n_bins > Summary.max_bins) return .invalid_arg;
    ring.summary.rmsBins(ring.total_written.load(.acquire), n_samples, bin_span_frames, out_rms[0 .. n_bins * ring.channels]);
    return .ok;
}

export fn fb_ring_peak_bins(ring: *Ring, n_frames: u64, n_bins: usize, out: [*]Ring.PeakBin) FbStatus {
    ring.peakBins(n_frames, n_bins, out[0 .. n_bins * ring.channels]) catch |err| return switch (err) {
        error.InvalidArgument => .invalid_arg,
        error.Overwritten => .overwritten,
    };
    return .ok;
}

export fn fb_ring_rms(ring: *Ring, n_frames: u64, out: [*]f32) FbStatus {
    ring.rmsLatest(n_frames, out[0..ring.channels]) catch |err| return switch (err) {
        error.Overwritten => .overwritten,
        error.OutOfRange => .out_of_range,
    };
    return .ok;
}

export fn fb_wav_write(path: [*:0]const u8, frames: [*]const f32, n_frames: usize, rate: u32, channels: u16, subtype: c_int) FbStatus {
    if (rate == 0 or channels == 0) return .invalid_arg;
    // Also closes writeHeader's `@intCast(bps * channels)` u16 trap: a
    // hostile channel count can overflow that cast before this guard
    // existed (see the report's captured panic). wav.max_channels is
    // sized for the WIDEST subtype (float32, 4 bytes/sample), so this
    // holds for every subtype fb_wav_write accepts, not just the one a
    // caller happens to pass.
    if (channels > wav.max_channels) return .invalid_arg;
    if (subtype < 0 or subtype > 2) return .invalid_arg;
    const st: wav.Subtype = @enumFromInt(@as(u8, @intCast(subtype)));
    // [*:0] is a SENTINEL pointer: length is found by scanning for the
    // 0 terminator — exactly C's char*. std.mem.span turns it into a slice.
    //
    // Locked for the call's duration — see wav.write_mutex in wav.zig.
    // This is the ONLY lock anywhere in this file; Ring.write and the
    // rest of the ring path stay lock-free.
    wav.write_mutex.lockUncancelable(wav.io);
    defer wav.write_mutex.unlock(wav.io);
    wav.writeFile(std.mem.span(path), frames[0 .. n_frames * channels], rate, channels, st) catch |e| return switch (e) {
        error.TooLong => .invalid_arg,
        else => .io_error,
    };
    return .ok;
}

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
/// (the host reads them with fb_wav_info first). n_frames * channels is
/// computed with a checked multiply so a hostile huge n_frames is a
/// status, not a ReleaseSafe overflow trap. `out_len` is the CALLER's
/// own count of that buffer (R-h6a, closes issue #57's carry comment):
/// this callee re-derives its own `len` from the file and rejects a
/// mismatch, rather than trusting a length the host computed from a
/// second, possibly-stale open of the same path.
export fn fb_wav_read(path: [*:0]const u8, start_frame: u64, n_frames: usize, out: [*]f32, out_len: usize) FbStatus {
    var o = wav.open(std.mem.span(path)) catch |e| return wavStatus(e);
    defer o.file.close(wav.io);
    const len = std.math.mul(usize, n_frames, o.info.channels) catch return .invalid_arg;
    if (len != out_len) return .invalid_arg;
    wav.readFrames(o.file, o.info, start_frame, out[0..len]) catch |e| return wavStatus(e);
    return .ok;
}

/// `out` holds n_bins * channels FbPeakBin, out[bin * channels + ch] —
/// the same layout as fb_ring_peak_bins. n_bins * channels is computed
/// with a checked multiply for the same reason as fb_wav_read. `out_len`
/// is the caller's own FbPeakBin count of that buffer — same R-h6a rule
/// as fb_wav_read, counted in FbPeakBin elements, not floats.
export fn fb_wav_peak_bins(path: [*:0]const u8, start_frame: u64, n_frames: u64, n_bins: usize, out: [*]peaks.PeakBin, out_len: usize) FbStatus {
    if (n_bins == 0) return .invalid_arg;
    var o = wav.open(std.mem.span(path)) catch |e| return wavStatus(e);
    defer o.file.close(wav.io);
    const len = std.math.mul(usize, n_bins, o.info.channels) catch return .invalid_arg;
    if (len != out_len) return .invalid_arg;
    peaks.peakBinsFile(o.file, o.info, start_frame, n_frames, n_bins, out[0..len]) catch |e| return wavStatus(e);
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
    try std.testing.expectEqual(FbStatus.ok, fb_wav_read(path, 0, 2, &out, 4));
    try std.testing.expectEqualSlices(f32, &in, &out);
    try std.testing.expectEqual(FbStatus.out_of_range, fb_wav_read(path, 1, 2, &out, 4));
    // Subtraction-form guards (readFrames, peakBinsFile): `start_frame >
    // info.frames` must short-circuit before `n_frames > info.frames -
    // start_frame` ever subtracts — maxInt(u64) - frames does not trap
    // here only because the `or` never evaluates the right side.
    // out_len here is 1 frame * 2 channels = 2, not out.len (4) — the
    // guard compares against what THIS call's n_frames actually derives.
    try std.testing.expectEqual(FbStatus.out_of_range, fb_wav_read(path, std.math.maxInt(u64), 1, &out, 2));
    var bins: [2]peaks.PeakBin = undefined; // stereo file: n_bins(1) * channels(2)
    try std.testing.expectEqual(FbStatus.out_of_range, fb_wav_peak_bins(path, std.math.maxInt(u64), 1, 1, &bins, 2));
}

test "fb_wav_read rejects a mismatched out_len instead of trusting a caller-derived size (R-h6a)" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/outlen.wav", .{tmp.sub_path}) catch unreachable;
    const in = [_]f32{ 0.1, -0.1, 0.2, -0.2 }; // 2 stereo frames
    try std.testing.expectEqual(FbStatus.ok, fb_wav_write(path, &in, 2, 48_000, 2, 0));
    var out: [4]f32 = undefined;
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_read(path, 0, 2, &out, 3)); // real len is 4
    try std.testing.expectEqual(FbStatus.ok, fb_wav_read(path, 0, 2, &out, 4));
}

test "fb_wav_peak_bins rejects a mismatched out_len instead of trusting a caller-derived size (R-h6a)" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/outlen2.wav", .{tmp.sub_path}) catch unreachable;
    const in = [_]f32{ 0.1, -0.1, 0.2, -0.2 }; // 2 stereo frames
    try std.testing.expectEqual(FbStatus.ok, fb_wav_write(path, &in, 2, 48_000, 2, 0));
    var bins: [2]peaks.PeakBin = undefined; // n_bins(1) * channels(2) == 2
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_peak_bins(path, 0, 2, 1, &bins, 1)); // real len is 2
    try std.testing.expectEqual(FbStatus.ok, fb_wav_peak_bins(path, 0, 2, 1, &bins, 2));
}

test "fb_wav_peak_bins rejects a channel count above max_channels instead of hanging its chunk loop" {
    // Same 20000-channel pcm16 image as wav.zig's max_channels test.
    // Before wav.max_channels existed, parseFmt accepted this header
    // (frames == 0, so open succeeded) and peaks.peakBinsFile's
    // frames_per_chunk = read_chunk_bytes / (4 * channels) truncated to
    // 0, hanging the chunk loop forever — so this test must only ever
    // run with the parseFmt fix in place; see wav.zig's sibling test for
    // the red evidence (open succeeding) this closes.
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    const fmt_body = [_]u8{
        1, 0, // tag: PCM
        32, 78, // channels: 20000
        64, 31, 0, 0, // rate: 8000
        0, 0, 0, 0, // byte rate, unchecked
        64, 156, // block_align: 40000
        16, 0, // bits: 16
    };
    var img: [64]u8 = undefined;
    var w: usize = 0;
    @memcpy(img[w .. w + 4], "RIFF");
    w += 4;
    std.mem.writeInt(u32, img[w..][0..4], @intCast(4 + 8 + fmt_body.len + 8), .little);
    w += 4;
    @memcpy(img[w .. w + 4], "WAVE");
    w += 4;
    @memcpy(img[w .. w + 4], "fmt ");
    w += 4;
    std.mem.writeInt(u32, img[w..][0..4], @intCast(fmt_body.len), .little);
    w += 4;
    @memcpy(img[w .. w + fmt_body.len], &fmt_body);
    w += fmt_body.len;
    @memcpy(img[w .. w + 4], "data");
    w += 4;
    std.mem.writeInt(u32, img[w..][0..4], 0, .little);
    w += 4;
    var pb: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/widechan.wav", .{tmp.sub_path}) catch unreachable;
    try tmp.dir.writeFile(std.testing.io, .{ .sub_path = "widechan.wav", .data = img[0..w] });
    // Sized for the 20000 channels the file CLAIMS, not the 1 n_bins
    // this call requests: if the parseFmt cap were ever mutated out and
    // open() actually succeeded, the export would slice
    // out[0 .. n_bins * channels] = out[0..20000] — a 1-element stack
    // array would be an out-of-bounds slice (a stack smash outside
    // Debug/ReleaseSafe's bounds checks), not just a wrong assertion.
    const out = try std.testing.allocator.alloc(peaks.PeakBin, 20000);
    defer std.testing.allocator.free(out);
    // open() itself rejects this file (max_channels), before the out_len
    // check ever runs — out_len's exact value doesn't matter here.
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_peak_bins(path, 0, 0, 1, out.ptr, out.len));
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

test "fb_wav_read: a huge n_frames returns invalid_arg instead of an overflow trap" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/huge.wav", .{tmp.sub_path}) catch unreachable;
    const in = [_]f32{ 0.1, -0.1 };
    try std.testing.expectEqual(FbStatus.ok, fb_wav_write(path, &in, 1, 48_000, 2, 0));
    var out: [4]f32 = undefined;
    // mul(usize, maxInt(usize), 2) overflows and returns invalid_arg
    // before out_len is ever compared — its value here doesn't matter.
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_read(path, 0, std.math.maxInt(usize), &out, out.len));
}

pub const FbCaptureSpec = extern struct { kind: u8, pid: u32, rate: u32, channels: u16, device_id: [*:0]const u8 };

test "fb_capture_create rejects an unknown kind and a bad channel count" {
    const ring = fb_ring_create(48_000, 2, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    try std.testing.expectEqual(@as(?*Capture, null), fb_capture_create(ring, &.{ .kind = 9, .pid = 0, .rate = 48_000, .channels = 2, .device_id = "" }));
    try std.testing.expectEqual(@as(?*Capture, null), fb_capture_create(ring, &.{ .kind = 0, .pid = 0, .rate = 48_000, .channels = 3, .device_id = "" }));
}

test "fb_capture stats/last_error on a never-started capture are zero/empty (Windows only)" {
    if (builtin.os.tag != .windows) return error.SkipZigTest;
    const ring = fb_ring_create(48_000, 2, 1.0, null) orelse return error.CreateFailed;
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

pub const FbProcess = if (builtin.os.tag == .windows) @import("WasapiBackend.zig").Process else extern struct { pid: u32, ppid: u32, name: [128]u8 };

export fn fb_processes_list(out: [*]FbProcess, max: usize) usize {
    if (max == 0) return 0;
    if (builtin.os.tag == .windows) return @import("WasapiBackend.zig").enumerateProcesses(out[0..max]);
    return 0;
}

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

// The ABI edge is the belt, matching fb_capture_create's specFromAbi
// guard: Playback.init itself does not validate rate/channels, so a bad
// pair reaching it directly would build a broken player rather than fail
// to create one.
export fn fb_playback_create(device_id: [*:0]const u8, rate: u32, channels: u16) ?*Playback {
    if (rate == 0 or channels == 0 or channels > 2) return null;
    const be = nativeBackend() orelse return null;
    const pb = allocator.create(Playback) catch return null;
    pb.* = Playback.init(allocator, be, .{ .kind = .render, .device_id = std.mem.span(device_id), .rate = rate, .channels = channels });
    return pb;
}

export fn fb_playback_bind(pb: *Playback, frames: [*]const f32, n_frames: usize, rate: u32, channels: u16) FbStatus {
    if (channels == 0) return .invalid_arg;
    const len = std.math.mul(usize, n_frames, channels) catch return .invalid_arg;
    pb.bind(.{ .frames = frames[0..len] }, rate, channels) catch |e| return switch (e) {
        error.InvalidArgument => .invalid_arg,
        error.OutOfMemory => .out_of_memory,
        else => .io_error,
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

// ---- Checkout persistence (epic #53, PR h) ----

/// Mirrors FbCheckoutInfo in flashback_core.h and native.py's
/// FbCheckoutInfo ctypes struct. write_state is the FbCheckoutInfo wire
/// value — see Checkout.WriteState's own doc comment: backed by u8 so
/// it is stable across Zig versions.
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

/// `s` is reserved: the LRU link for a slice is made on its first load,
/// not at creation — a slice never owns frames of its own to link now.
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
/// prefix). A start at or past the end is invalid_arg. `s` is reserved:
/// same as fb_checkout_slice, the LRU link is made on first load.
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

/// `resident_bytes` reads `co.frames`, a two-word optional slice that is
/// not itself atomic — closing every race on it needs BOTH of the
/// following, neither sufficient alone:
///   - `waitLoad(co)` first: `Checkout.load` assigns `co.frames` OUTSIDE
///     Scratch.mutex (see doLoad's own doc), so a mutex alone would not
///     see that write ordered correctly against a load still in flight
///     — waitLoad is the only thing that blocks until a `.load` job has
///     fully finished.
///   - `s.mutex` around the read itself: an EVICT (not a load) runs
///     entirely under `s.mutex` (`evictOverBudgetLocked` calls
///     `co.evict()` while still holding it), and can free `co.frames`
///     from the WORKER thread for an unrelated job's post-completion
///     budget check, at any moment `waitLoad` alone does not cover.
///     Taking `s.mutex` here makes that a mutual-exclusion boundary
///     against this exact read.
/// Together these close both races; the worker never holds `s.mutex`
/// during file I/O (only for the bookkeeping around a job), so taking
/// it here never blocks on a write or load actually happening.
export fn fb_checkout_info(s: *Scratch, co: *Checkout, out: *FbCheckoutInfo) void {
    s.waitLoad(co);
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

/// R-h6c: `out` holds n_bins * channels FbPeakBin, sized the same
/// checked-multiply way as fb_wav_peak_bins. `out_len` is the caller's
/// own count of that buffer (R-h6a) — never trusted without a match.
/// R-h1d: `hold`/`release` bracket the read so a job finishing between
/// this call starting and `co.peakBins` running cannot free `frames`
/// out from under it (the eviction walk skips `hold > 0`). `touch`
/// records this as a real use, moving `co` to the LRU head — without
/// it every read here would be invisible to the cache, and eviction
/// would fall back to pure load order instead of least-recently-used.
export fn fb_checkout_peak_bins(s: *Scratch, co: *Checkout, n_bins: usize, out: [*]peaks.PeakBin, out_len: usize) FbStatus {
    if (n_bins == 0) return .invalid_arg;
    const len = std.math.mul(usize, n_bins, co.channels) catch return .invalid_arg;
    if (len != out_len) return .invalid_arg;
    s.hold(co);
    defer s.release(co);
    s.touch(co);
    co.peakBins(n_bins, out[0..len]) catch |e| return checkoutStatus(e);
    return .ok;
}

export fn fb_checkout_pin(s: *Scratch, co: *Checkout, on: u8) void {
    s.pin(co, on != 0);
}

/// Materialise `[start, start + n)` of the checkout into `dst`. From the
/// file once the audio is safe on disk (written/adopted — no reload of
/// an evicted clip), from the RAM copy before that. R-h1b: the range
/// guard uses the subtraction form so a hostile `start` cannot overflow
/// the addition `start + n` under ReleaseSafe. R-h1d: `hold`/`release`
/// bracket the whole read (both branches) for the same reason as
/// fb_checkout_peak_bins, and `touch` marks the LRU use the same way.
/// R-h6g: no trailing `FbMarkers *` yet — PR i adds region-aware export
/// on top of this signature.
export fn fb_checkout_export(s: *Scratch, co: *Checkout, dst: [*:0]const u8, start: u64, n: u64, subtype: c_int) FbStatus {
    if (subtype < 0 or subtype > 2) return .invalid_arg;
    if (n == 0 or start > co.n_frames or n > co.n_frames - start) return .invalid_arg;
    const st: wav.Subtype = @enumFromInt(@as(u8, @intCast(subtype)));
    s.hold(co);
    defer s.release(co);
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

/// R-h4b: routes through `forget` (dequeue-by-hand when no worker is
/// running, wait-then-unlink when one is) before freeing `co`, so a
/// checkout created and destroyed on an unstarted or already-stopped
/// scratch cannot dangle a stale FIFO/LRU pointer.
export fn fb_checkout_destroy(s: *Scratch, co: *Checkout) void {
    s.forget(co);
    co.destroy();
}

/// R-h1b: subtraction-form range guard, same reason as fb_checkout_export.
/// R-h1d: `hold`/`release` bracket `Checkout.source` (which returns a
/// `frames` sub-slice or a file range — either way a reference into
/// state that must not be evicted mid-bind) and `Playback.bind`'s copy;
/// `touch` marks the LRU use, same as the other two wrapped exports.
export fn fb_playback_bind_checkout(pb: *Playback, s: *Scratch, co: *Checkout, start: u64, n: u64) FbStatus {
    if (n == 0 or start > co.n_frames or n > co.n_frames - start) return .invalid_arg;
    s.hold(co);
    defer s.release(co);
    s.touch(co);
    pb.bind(co.source(start, n), co.rate, co.channels) catch |e| return checkoutStatus(e);
    return .ok;
}

test "WriteState wire values are stable across Zig versions (native.py's WRITE_STATES mirrors these ints)" {
    try std.testing.expectEqual(@as(u8, 0), @intFromEnum(Checkout.WriteState.queued));
    try std.testing.expectEqual(@as(u8, 1), @intFromEnum(Checkout.WriteState.writing));
    try std.testing.expectEqual(@as(u8, 2), @intFromEnum(Checkout.WriteState.written));
    try std.testing.expectEqual(@as(u8, 3), @intFromEnum(Checkout.WriteState.failed));
    try std.testing.expectEqual(@as(u8, 4), @intFromEnum(Checkout.WriteState.adopted));
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

test "fb_checkout_create reports overwritten/out_of_range/invalid_arg distinctly (R-h6e: overwritten is a real lapped-span case)" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/x.wav", .{tmp.sub_path}) catch unreachable;
    const ring = fb_ring_create(1000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    fb_ring_write(ring, &[_]f32{ 1, 2, 3 }, 3);
    const s = fb_scratch_create(0, null) orelse return error.CreateFailed;
    defer fb_scratch_destroy(s);
    var st: FbStatus = .ok;
    try std.testing.expectEqual(@as(?*Checkout, null), fb_checkout_create(s, ring, 1, 9, path, &st));
    try std.testing.expectEqual(FbStatus.out_of_range, st);
    try std.testing.expectEqual(@as(?*Checkout, null), fb_checkout_create(s, ring, 2, 2, path, &st));
    try std.testing.expectEqual(FbStatus.invalid_arg, st);

    // R-h6e: a genuine overwritten span — a ring whose capacity has
    // already been lapped by later writes, so the requested span's
    // bytes no longer exist anywhere. Capacity 4: writing 10 frames
    // laps everything before frame 6 (total_written(10) - capacity(4)).
    var pb2: [64]u8 = undefined;
    const lapped_path = std.fmt.bufPrintZ(&pb2, ".zig-cache/tmp/{s}/lapped.wav", .{tmp.sub_path}) catch unreachable;
    const small = fb_ring_create(4, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(small);
    var ramp: [10]f32 = undefined;
    for (&ramp, 0..) |*f, i| f.* = @floatFromInt(i);
    fb_ring_write(small, &ramp, 10);
    try std.testing.expectEqual(@as(?*Checkout, null), fb_checkout_create(s, small, 0, 4, lapped_path, &st));
    try std.testing.expectEqual(FbStatus.overwritten, st);
}

test "fb_checkout_export rejects a span past the checkout (R-h4b: destroy on an unstarted scratch does not hang)" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const co_path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/never.wav", .{tmp.sub_path}) catch unreachable;
    var pb2: [64]u8 = undefined;
    const out_path = std.fmt.bufPrintZ(&pb2, ".zig-cache/tmp/{s}/out.wav", .{tmp.sub_path}) catch unreachable;
    const ring = fb_ring_create(1000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    fb_ring_write(ring, &[_]f32{ 1, 2, 3 }, 3);
    const s = fb_scratch_create(1 << 20, null) orelse return error.CreateFailed;
    defer fb_scratch_destroy(s); // never started: fb_checkout_destroy below must route through forget's dequeue branch
    const co = fb_checkout_create(s, ring, 0, 3, co_path, null) orelse return error.CreateFailed;
    defer fb_checkout_destroy(s, co);
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_checkout_export(s, co, out_path, 2, 2, 0));
}

test "fb_checkout_peak_bins rejects a mismatched out_len instead of trusting a caller-derived size (R-h6a/R-h6c)" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&pb, ".zig-cache/tmp/{s}/peaks.wav", .{tmp.sub_path}) catch unreachable;
    const ring = fb_ring_create(1000, 2, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    fb_ring_write(ring, &[_]f32{ 1, -1, 2, -2, 3, -3, 4, -4 }, 4);
    const s = fb_scratch_create(1 << 20, null) orelse return error.CreateFailed;
    defer fb_scratch_destroy(s);
    const co = fb_checkout_create(s, ring, 0, 4, path, null) orelse return error.CreateFailed;
    defer fb_checkout_destroy(s, co);
    var bins: [4]peaks.PeakBin = undefined; // n_bins(2) * channels(2) == 4
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_checkout_peak_bins(s, co, 2, &bins, 3)); // real len is 4
    try std.testing.expectEqual(FbStatus.ok, fb_checkout_peak_bins(s, co, 2, &bins, 4));
}

test "fb_checkout_peak_bins touches the checkout: reading A then adding B over budget evicts the UNTOUCHED one, not A" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pbp: [64]u8 = undefined;
    const path_a = std.fmt.bufPrintZ(&pa, ".zig-cache/tmp/{s}/touch-a.wav", .{tmp.sub_path}) catch unreachable;
    const path_b = std.fmt.bufPrintZ(&pbp, ".zig-cache/tmp/{s}/touch-b.wav", .{tmp.sub_path}) catch unreachable;
    const ring = fb_ring_create(1000, 2, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    var in: [16]f32 = undefined; // 8 stereo frames
    for (&in, 0..) |*f, i| f.* = @floatFromInt(i);
    fb_ring_write(ring, &in, 8);
    const s = fb_scratch_create(1 << 30, null) orelse return error.CreateFailed; // generous: both writes land uncontended
    defer fb_scratch_destroy(s);
    try std.testing.expectEqual(FbStatus.ok, fb_scratch_start(s));

    // Both 4 stereo frames: residentBytes = 4 * 2 * 4 = 32 each.
    const a = fb_checkout_create(s, ring, 0, 4, path_a, null) orelse return error.CreateFailed;
    defer fb_checkout_destroy(s, a);
    s.waitJob(a);
    const b = fb_checkout_create(s, ring, 4, 8, path_b, null) orelse return error.CreateFailed;
    defer fb_checkout_destroy(s, b);
    s.waitJob(b);
    // Submit order alone would put b at the LRU head and a at the tail
    // (submitLocked inserts each new .write at the head) — so without a
    // touch, a lowered budget would evict `a` next, not `b`.

    var bins: [2]peaks.PeakBin = undefined; // n_bins(1) * channels(2)
    // Read A: this must call Scratch.touch, moving A to the LRU head —
    // otherwise this whole test is exercising nothing but write order.
    try std.testing.expectEqual(FbStatus.ok, fb_checkout_peak_bins(s, a, 1, &bins, 2));

    fb_scratch_set_budget(s, 32); // room for exactly one of the two 32-byte entries
    var info_a: FbCheckoutInfo = undefined;
    var info_b: FbCheckoutInfo = undefined;
    fb_checkout_info(s, a, &info_a);
    fb_checkout_info(s, b, &info_b);
    try std.testing.expectEqual(@as(u64, 32), info_a.resident_bytes); // touched: survives
    try std.testing.expectEqual(@as(u64, 0), info_b.resident_bytes); // untouched: evicted
}

/// R-h6d: the Python racy "RAM vs file agree" test cannot be made
/// deterministic (the write finishing is a race with the test's own
/// timing), so the RAM branch is pinned here instead — in-process Zig,
/// using the same write_fn parking seam Scratch.zig's own tests use
/// (Recorder), reimplemented locally since Recorder is private to that
/// file. Parking the writer keeps write_state at `.queued`/`.writing`
/// (never `.written`) while this test exports from RAM; unparking,
/// waiting for the job, and evicting exercises the file branch on the
/// exact same bytes for comparison.
const ParkedWrite = struct {
    var park: std.atomic.Value(bool) = std.atomic.Value(bool).init(false);
    fn reset() void {
        park.store(false, .monotonic);
    }
    fn write(path: []const u8, frames: []const f32, rate: u32, channels: u16) anyerror!void {
        while (park.load(.acquire)) std.Thread.yield() catch {};
        try wav.writeFile(path, frames, rate, channels, .float32);
    }
};

test "fb_checkout_export: the RAM branch (pre-write) and the file branch (post-write, evicted) agree byte-for-byte" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var in: [40]f32 = undefined; // 20 stereo frames
    for (&in, 0..) |*f, i| f.* = @as(f32, @floatFromInt(i)) / 10.0;
    ring.write(&in);
    var pco: [64]u8 = undefined;
    const co_path = std.fmt.bufPrintZ(&pco, ".zig-cache/tmp/{s}/co.wav", .{tmp.sub_path}) catch unreachable;

    var s = Scratch.init(1 << 20);
    s.write_fn = &ParkedWrite.write;
    ParkedWrite.reset(); // defensive: a prior failed run in this binary must not leave park stuck true
    ParkedWrite.park.store(true, .release); // the write never lands until unparked below
    try s.start();
    defer s.stop(); // registered before the unpark defer below: see its comment for why that matters

    var st: FbStatus = .io_error;
    const co = fb_checkout_create(&s, &ring, 0, 20, co_path, &st) orelse return error.CreateFailed;
    defer fb_checkout_destroy(&s, co); // also registered before the unpark defer, same reason
    // Registered LAST, after every defer above that can itself block on
    // the parked worker: `fb_checkout_destroy` -> `forget`'s
    // worker-running branch waits for co's `.write` job to finish, and
    // `s.stop()` joins the worker thread — both would hang forever
    // against a still-parked write. Zig defers unwind LIFO (most
    // recently registered runs first), so THIS being the last one
    // registered is what guarantees it runs before both of those on
    // every exit path (the `try`s and `expectEqual`s all the way to the
    // bottom of this test included), not just the happy path. Putting
    // it in the same combined defer as `s.stop()` — tried first, and
    // wrong — does not help: `fb_checkout_destroy`'s defer, registered
    // between the two, would still unwind before it (reproduced: the
    // suite hung for real on a deliberately forced failure here).
    defer ParkedWrite.park.store(false, .release);
    try std.testing.expectEqual(FbStatus.ok, st);
    // Not written/adopted yet (parked): fb_checkout_export must take the
    // RAM (frames) branch here, not the file branch.
    try std.testing.expect(co.write_state.load(.acquire) != .written);

    var pram: [64]u8 = undefined;
    const ram_dst = std.fmt.bufPrintZ(&pram, ".zig-cache/tmp/{s}/ram.wav", .{tmp.sub_path}) catch unreachable;
    try std.testing.expectEqual(FbStatus.ok, fb_checkout_export(&s, co, ram_dst, 2, 10, 0)); // FLOAT32

    ParkedWrite.park.store(false, .release); // let the parked write finish
    s.waitJob(co);
    try std.testing.expectEqual(Checkout.WriteState.written, co.write_state.load(.acquire));

    s.setBudget(0); // no pin, no hold, job none, ws written: evicts
    try std.testing.expectEqual(@as(?[]f32, null), co.frames);

    var pfile: [64]u8 = undefined;
    const file_dst = std.fmt.bufPrintZ(&pfile, ".zig-cache/tmp/{s}/file.wav", .{tmp.sub_path}) catch unreachable;
    try std.testing.expectEqual(FbStatus.ok, fb_checkout_export(&s, co, file_dst, 2, 10, 0)); // file branch now

    var oram = try wav.open(ram_dst);
    defer oram.file.close(wav.io);
    var ofile = try wav.open(file_dst);
    defer ofile.file.close(wav.io);
    try std.testing.expectEqual(@as(u64, 10), oram.info.frames);
    try std.testing.expectEqual(@as(u64, 10), ofile.info.frames);
    var ram_samples: [20]f32 = undefined; // 10 frames * 2 channels
    var file_samples: [20]f32 = undefined;
    try wav.readFrames(oram.file, oram.info, 0, &ram_samples);
    try wav.readFrames(ofile.file, ofile.info, 0, &file_samples);
    try std.testing.expectEqualSlices(f32, &ram_samples, &file_samples);
    try std.testing.expectEqualSlices(f32, in[4..24], &ram_samples); // both equal the source ramp too
}

test "fb_playback_bind_checkout rejects a span past the checkout (R-h1b subtraction guard)" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&path_buf, ".zig-cache/tmp/{s}/bind.wav", .{tmp.sub_path}) catch unreachable;
    const ring = fb_ring_create(1000, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    fb_ring_write(ring, &[_]f32{ 1, 2, 3 }, 3);
    const s = fb_scratch_create(1 << 20, null) orelse return error.CreateFailed;
    defer fb_scratch_destroy(s);
    const co = fb_checkout_create(s, ring, 0, 3, path, null) orelse return error.CreateFailed;
    defer fb_checkout_destroy(s, co);
    if (builtin.os.tag != .windows) return error.SkipZigTest; // fb_playback_create needs a backend
    const pb = fb_playback_create("", 48_000, 1) orelse return error.CreateFailed;
    defer fb_playback_destroy(pb);
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_playback_bind_checkout(pb, s, co, 2, 2)); // start(2) + n(2) > n_frames(3)
    try std.testing.expectEqual(FbStatus.ok, fb_playback_bind_checkout(pb, s, co, 1, 2));
}

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

test "fb_ring_create: out_of_memory when Ring.init's own allocation fails (the #41 path)" {
    // fail_index = 1: the first allocation (alloc.create(Ring)) succeeds, the
    // second — Ring.init's storage — fails, so this pins the INNER switch arm.
    // Backed by std.testing.allocator, so a leak on the unwind path fails too.
    var fa = std.testing.FailingAllocator.init(std.testing.allocator, .{ .fail_index = 1 });
    var st: FbStatus = .ok;
    try std.testing.expectEqual(@as(?*Ring, null), ringCreate(fa.allocator(), 48_000, 2, 1.0, &st));
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

test "fb_playback_create rejects rate 0 and channels 3" {
    try std.testing.expectEqual(@as(?*Playback, null), fb_playback_create("", 0, 2));
    try std.testing.expectEqual(@as(?*Playback, null), fb_playback_create("", 48_000, 3));
}

test "fb_playback_bind rejects an n_frames * channels product that overflows usize" {
    if (builtin.os.tag != .windows) return error.SkipZigTest; // fb_playback_create needs a backend
    const pb = fb_playback_create("", 48_000, 2) orelse return error.CreateFailed;
    defer fb_playback_destroy(pb);
    const frames = [_]f32{0.0};
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_playback_bind(pb, &frames, std.math.maxInt(usize), 48_000, 2));
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
    // 2 frames * 3 channels == 6, exactly the array's length: in range, so
    // this trips the channels guard and nothing else (a 3-frame slice over
    // 6 elements at channels=3 would run out of range and prove nothing
    // about the guard itself).
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_playback_bind(pb, &frames, 2, 48_000, 3));
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

test "FbWavInfo layout matches native.py's FbWavInfo ctypes struct" {
    try std.testing.expectEqual(@as(usize, 16), @sizeOf(FbWavInfo));
    try std.testing.expectEqual(@as(usize, 8), @offsetOf(FbWavInfo, "frames"));
}

test "FbCaptureStats layout matches native.py's FbCaptureStats ctypes struct" {
    // u8, 7 pad, u64, u32, u32, u8, 7 pad. Offsets, not only the size:
    // two fields swapped keep @sizeOf at 32 while ctypes reads the wrong
    // bytes.
    try std.testing.expectEqual(@as(usize, 32), @sizeOf(Capture.Stats));
    try std.testing.expectEqual(@as(usize, 8), @offsetOf(Capture.Stats, "frames_written"));
    try std.testing.expectEqual(@as(usize, 16), @offsetOf(Capture.Stats, "xruns"));
    try std.testing.expectEqual(@as(usize, 20), @offsetOf(Capture.Stats, "mix_rate"));
    try std.testing.expectEqual(@as(usize, 24), @offsetOf(Capture.Stats, "sources"));
}

test "FbCheckoutInfo layout matches native.py's FbCheckoutInfo ctypes struct" {
    // The trickiest padding of the three mirrored structs: rate(u32) +
    // channels(u16) + write_state(u8) leaves 7 bytes before the first
    // u64 field, which needs 8-byte alignment — 1 padding byte pushes
    // n_frames to offset 8. A Zig-only drift here (a reordered field, a
    // changed field type) would still pass `zig build test` on its own;
    // this is what pins it against native.py's FbCheckoutInfo ctypes
    // Structure actually agreeing, byte for byte.
    try std.testing.expectEqual(@as(usize, 32), @sizeOf(FbCheckoutInfo));
    try std.testing.expectEqual(@as(usize, 0), @offsetOf(FbCheckoutInfo, "rate"));
    try std.testing.expectEqual(@as(usize, 4), @offsetOf(FbCheckoutInfo, "channels"));
    try std.testing.expectEqual(@as(usize, 6), @offsetOf(FbCheckoutInfo, "write_state"));
    try std.testing.expectEqual(@as(usize, 8), @offsetOf(FbCheckoutInfo, "n_frames"));
    try std.testing.expectEqual(@as(usize, 16), @offsetOf(FbCheckoutInfo, "start_frame"));
    try std.testing.expectEqual(@as(usize, 24), @offsetOf(FbCheckoutInfo, "resident_bytes"));
}

test "fb_ring_rms reports per-channel RMS of the newest window" {
    const ring = fb_ring_create(16, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    const in = [_]f32{ 3, 4, 0, 0 };
    fb_ring_write(ring, &in, 4);
    var out: [1]f32 = undefined;
    try std.testing.expectEqual(FbStatus.ok, fb_ring_rms(ring, 4, &out));
    try std.testing.expectEqual(@as(f32, 2.5), out[0]);
}
