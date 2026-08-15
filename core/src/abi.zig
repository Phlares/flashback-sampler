//! C ABI shim. Thin and boring by design: translate error unions to
//! status codes, pointers+lengths to slices, and NOTHING else. All
//! logic lives in Ring/Summary/wav — this file must stay liftable-host
//! plumbing. `export fn` gives the symbol an unmangled C name in the
//! shared library.
const std = @import("std");
const Ring = @import("Ring.zig");
const wav = @import("wav.zig");

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

test "fb_ring_storage_frames is capacity plus the guard band, distinct from fb_ring_capacity" {
    const ring = fb_ring_create(8, 1, 1.0) orelse return error.CreateFailed; // capacity == 8
    defer fb_ring_destroy(ring);
    try std.testing.expectEqual(@as(u64, 8), fb_ring_capacity(ring));
    try std.testing.expectEqual(@as(u64, 8) + Ring.max_write_frames, fb_ring_storage_frames(ring));
}

test "fb_ring_summary_bins rejects n_bins == 0" {
    const ring = fb_ring_create(48_000, 1, 1.0) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    var out: [1]f32 = undefined;
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_ring_summary_bins(ring, 0, 0, 0, &out));
}

test "fb_ring_summary_bins rejects n_bins > 4096" {
    const ring = fb_ring_create(48_000, 1, 1.0) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    var out: [4097]f32 = undefined;
    try std.testing.expectEqual(FbStatus.invalid_arg, fb_ring_summary_bins(ring, 4097, 0, 0, &out));
}

test "fb_ring_summary_bins accepts n_bins == 4096, the inclusive boundary" {
    // Pins the guard's `>` (not `>=`) — a mutation to `>=` would reject
    // this exact value and this test would catch it going red.
    const ring = fb_ring_create(48_000, 1, 1.0) orelse return error.CreateFailed;
    defer fb_ring_destroy(ring);
    var out: [4096]f32 = undefined;
    try std.testing.expectEqual(FbStatus.ok, fb_ring_summary_bins(ring, 4096, 0, 0, &out));
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

// The PHYSICAL frame count backing fb_ring_storage — capacity plus the
// guard band (see Ring.zig's struct-level comment and the note above
// Ring.read). A host building a zero-copy numpy view over
// fb_ring_storage must shape that view with THIS value, and any write
// position it derives via modulo must wrap at THIS value too — using
// fb_ring_capacity for either silently corrupts get_peak_bins, which
// walks the raw buffer directly. fb_ring_capacity is still the right
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
    //
    // Locked for the call's duration — see the doc comment on
    // wav_write_mutex above for why. This is the ONLY lock anywhere in
    // this file; Ring.write and the rest of the ring path stay lock-free.
    wav_write_mutex.lockUncancelable(wav_write_io);
    defer wav_write_mutex.unlock(wav_write_io);
    wav.writeFile(std.mem.span(path), frames[0 .. n_frames * channels], rate, channels, st) catch return .io_error;
    return .ok;
}
