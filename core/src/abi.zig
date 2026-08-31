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

// Serializes fb_wav_write. writeFile uses
// std.Io.Threaded.global_single_threaded, which std documents
// (std/Io/Threaded.zig) as not supporting concurrency, but that doc
// comment is written against Io.async/Io.concurrent/task groups that
// writeFile never uses — it has NOT been exhaustively traced against
// every syscall wrapper writeFile calls (createFile,
// writeStreamingAll, close) for other shared mutable state in the
// singleton. Rather than leave "is concurrent writeFile safe?" as an
// open question on a published C interface, this mutex makes the
// question moot: fb_wav_write is an offline file-write path (never the
// RT audio thread), so a lock held for the call's duration costs
// nothing that matters. Ring.write's RT-safety (no locks, no
// allocation, no error path) is completely untouched by this — the
// mutex only ever guards wav.writeFile.
//
// Zig 0.16 moved blocking primitives (Mutex included) under std.Io —
// there is no bare std.Thread.Mutex anymore. std.Io.Mutex needs an `Io`
// implementation to block/wake on, so it reuses the SAME
// global_single_threaded singleton wav.writeFile itself already reaches
// for (see wav.zig's doc comment on writeFile for why that singleton is
// the right "hardcode a synchronous Io" choice here). Despite the name,
// its futex wait/wake underneath is a real OS primitive (WaitOnAddress
// / futex), so this is genuine cross-OS-thread mutual exclusion, not a
// same-thread-only stub. `lockUncancelable`/`unlock` (as opposed to the
// cancelable `lock`) are used because this Io singleton has no
// cancelation source to ever trigger — there is no error path to plumb
// through an `export fn`.
const wav_write_io = std.Io.Threaded.global_single_threaded.io();
var wav_write_mutex: std.Io.Mutex = .init;

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
    // Locked for the call's duration — see the doc comment on
    // wav_write_mutex above for why. This is the ONLY lock anywhere in
    // this file; Ring.write and the rest of the ring path stay lock-free.
    wav_write_mutex.lockUncancelable(wav_write_io);
    defer wav_write_mutex.unlock(wav_write_io);
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
/// status, not a ReleaseSafe overflow trap.
export fn fb_wav_read(path: [*:0]const u8, start_frame: u64, n_frames: usize, out: [*]f32) FbStatus {
    var o = wav.open(std.mem.span(path)) catch |e| return wavStatus(e);
    defer o.file.close(wav.io);
    const len = std.math.mul(usize, n_frames, o.info.channels) catch return .invalid_arg;
    wav.readFrames(o.file, o.info, start_frame, out[0..len]) catch |e| return wavStatus(e);
    return .ok;
}

/// `out` holds n_bins * channels FbPeakBin, out[bin * channels + ch] —
/// the same layout as fb_ring_peak_bins. n_bins * channels is computed
/// with a checked multiply for the same reason as fb_wav_read.
export fn fb_wav_peak_bins(path: [*:0]const u8, start_frame: u64, n_frames: u64, n_bins: usize, out: [*]peaks.PeakBin) FbStatus {
    if (n_bins == 0) return .invalid_arg;
    var o = wav.open(std.mem.span(path)) catch |e| return wavStatus(e);
    defer o.file.close(wav.io);
    const len = std.math.mul(usize, n_bins, o.info.channels) catch return .invalid_arg;
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
    try std.testing.expectEqual(FbStatus.ok, fb_wav_read(path, 0, 2, &out));
    try std.testing.expectEqualSlices(f32, &in, &out);
    try std.testing.expectEqual(FbStatus.out_of_range, fb_wav_read(path, 1, 2, &out));
    // Subtraction-form guards (readFrames, peakBinsFile): `start_frame >
    // info.frames` must short-circuit before `n_frames > info.frames -
    // start_frame` ever subtracts — maxInt(u64) - frames does not trap
    // here only because the `or` never evaluates the right side.
    try std.testing.expectEqual(FbStatus.out_of_range, fb_wav_read(path, std.math.maxInt(u64), 1, &out));
    var bins: [2]peaks.PeakBin = undefined; // stereo file: n_bins(1) * channels(2)
    try std.testing.expectEqual(FbStatus.out_of_range, fb_wav_peak_bins(path, std.math.maxInt(u64), 1, 1, &bins));
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
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_peak_bins(path, 0, 0, 1, out.ptr));
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
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_wav_read(path, 0, std.math.maxInt(usize), &out));
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

test "fb_ring_rms reports per-channel RMS of the newest window" {
    const ring = fb_ring_create(16, 1, 1.0, null) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    const in = [_]f32{ 3, 4, 0, 0 };
    fb_ring_write(ring, &in, 4);
    var out: [1]f32 = undefined;
    try std.testing.expectEqual(FbStatus.ok, fb_ring_rms(ring, 4, &out));
    try std.testing.expectEqual(@as(f32, 2.5), out[0]);
}
