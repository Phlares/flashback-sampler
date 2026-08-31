//! Checkout.zig — one checkout: the ONE RAM copy of its frames (or none,
//! once evicted) plus where the same audio lives on disk:
//! `(path, start_frame, n_frames)`. A root owns its file at (0, all); a
//! slice references its parent's file. Python decides lifetimes (which
//! file is deleted when); this file holds bytes and moves them.
//!
//! Concurrency: `write_state` is atomic (the ABI polls it without a
//! lock). `job`, `pinned`, `hold`, the list links and `frames` are
//! guarded by Scratch.mutex — see Scratch.zig's "Thread rules". Nothing
//! here locks; Scratch is the only caller that mutates a checkout after
//! creation.
const std = @import("std");
const Ring = @import("Ring.zig");
const wav = @import("wav.zig");
const peaks = @import("peaks.zig");
const Playback = @import("Playback.zig");
const test_util = @import("test_util.zig");

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
/// Transient residency count taken by ABI calls that read `frames`
/// outside the scratch mutex; eviction skips `hold > 0`. Distinct from
/// `pinned` (the app's select pin) so an ABI read never clears the
/// app's pin.
hold: u32,
queue_next: ?*Checkout,
lru_prev: ?*Checkout,
lru_next: ?*Checkout,
/// Snapshot of `residentBytes()` taken by Scratch at LRU-insert time.
/// `residentBytes()` reads `frames.len`, which can change (load, evict)
/// while this checkout stays linked — Scratch.lruRemoveLocked must
/// subtract the exact figure it added, not whatever `frames` holds at
/// removal time, or `resident_bytes` drifts or underflows. Owned by
/// Scratch.mutex, same as the list links.
lru_bytes: u64,

fn create(allocator: std.mem.Allocator, p: []const u8, start_frame: u64, n_frames: u64, rate: u32, channels: u16, frames: ?[]f32, ws: WriteState) !*Checkout {
    if (p.len >= max_path) return error.PathTooLong;
    // The one place every checkout is born: reject `channels == 0` here
    // so every later caller (load, peakBins, Playback.bind's .file arm)
    // may divide or index by `self.channels` without its own zero-check.
    if (channels == 0) return error.InvalidArgument;
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
        .hold = 0,
        .queue_next = null,
        .lru_prev = null,
        .lru_next = null,
        .lru_bytes = 0,
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
    // n arrives raw off caller-supplied abs_start/abs_end (eventually a
    // ctypes boundary) — `n * chans` alone can wrap `usize` on a hostile
    // span before `@intCast` ever runs. Divide form, not `n * chans >
    // maxInt(usize)`: the product itself is what would overflow.
    if (n > std.math.maxInt(usize) / chans) return error.InvalidArgument;
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
    // Subtraction form, not `start + n > parent.n_frames`: start/n arrive
    // raw from ctypes, and the addition can overflow-trap in ReleaseSafe
    // on a hostile value.
    if (n == 0 or start > parent.n_frames or n > parent.n_frames - start) return error.InvalidArgument;
    // parent.start_frame + start: same hazard as the guard above, but on
    // the PARENT's own start_frame — a corrupt adopted manifest (h7) can
    // set that near maxInt(u64). std.math.add rejects the wrap instead
    // of letting a plain `+` trap.
    const abs_start = std.math.add(u64, parent.start_frame, start) catch return error.InvalidArgument;
    return create(allocator, parent.path(), abs_start, n, parent.rate, parent.channels, null, .adopted);
}

pub fn destroy(self: *Checkout) void {
    if (self.frames) |f| self.allocator.free(f);
    self.allocator.destroy(self);
}

/// Read this checkout's range from its file into a fresh allocation.
/// Idempotent: resident frames stay. Called by the writer thread for a
/// `.load` job, or by tests.
pub fn load(self: *Checkout) !void {
    if (self.frames != null) return;
    // `n_frames * channels` alone can wrap `usize` on a corrupt adopted
    // manifest (h7) before `@intCast` ever runs — same divide-form guard
    // as createFromRing's, checked before the multiply itself. `chans`
    // is never 0 here: create() rejects that at construction.
    const chans: u64 = self.channels;
    if (self.n_frames > std.math.maxInt(usize) / chans) return error.InvalidArgument;
    var o = try wav.open(self.path());
    defer o.file.close(wav.io);
    // self.channels is the count every reader of `frames` (peakBins'
    // RAM arm, Checkout.source, Playback via bind) will trust from here
    // on; if the file's own header disagrees — a stale manifest, or the
    // file swapped out from under an adopted path — reject before any
    // read or state mutation, not after.
    if (o.info.channels != self.channels) return error.InvalidArgument;
    const buf = try self.allocator.alloc(f32, @intCast(self.n_frames * chans));
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
/// otherwise. `out.len == n_bins * channels`. The single contract both
/// arms rely on: `self.channels` IS this checkout's channel count,
/// whether the audio is in RAM or still on disk — the RAM arm trusts it
/// directly, and the file arm checks the file's own header against it
/// before reading, the same check `load()` makes. Same reducer both
/// ways once that holds, so the bins are identical whichever path
/// served them.
pub fn peakBins(self: *Checkout, n_bins: usize, out: []peaks.PeakBin) !void {
    if (self.frames) |f| {
        peaks.peakBinsFlat(f, self.channels, n_bins, out);
        return;
    }
    var o = try wav.open(self.path());
    defer o.file.close(wav.io);
    if (o.info.channels != self.channels) return error.InvalidArgument;
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

test "createFromRing: a span whose n * chans overflows usize is InvalidArgument, not a trap" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    // abs_end - abs_start = maxInt(u64); stereo (chans = 2) means
    // n * chans wraps usize long before any read or allocation would
    // run. std.testing.allocator also fails the test on any leak, so a
    // pass here doubles as proof no allocation was attempted.
    try std.testing.expectError(error.InvalidArgument, createFromRing(std.testing.allocator, &ring, 0, std.math.maxInt(u64), "huge.wav"));
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

test "slice: a hostile start or parent.start_frame is InvalidArgument, not an overflow trap" {
    const parent = try adopt(std.testing.allocator, "parent.wav", 100, 50, 48_000, 2);
    defer parent.destroy();
    // Under the brief's original `start + n > parent.n_frames` form this
    // addition itself overflow-traps before the comparison ever runs;
    // the subtraction-form guard (`start > parent.n_frames`) rejects it
    // cleanly instead, short-circuiting before any subtraction.
    try std.testing.expectError(error.InvalidArgument, slice(std.testing.allocator, parent, std.math.maxInt(u64), 1));

    // The OTHER side: a parent whose own start_frame is corrupt (as an
    // h7 manifest could feed adopt()). `start`/`n` here pass the guard
    // above cleanly — the overflow is in `parent.start_frame + start`,
    // guarded separately by std.math.add.
    const corrupt_parent = try adopt(std.testing.allocator, "corrupt.wav", std.math.maxInt(u64) - 5, 50, 48_000, 2);
    defer corrupt_parent.destroy();
    try std.testing.expectError(error.InvalidArgument, slice(std.testing.allocator, corrupt_parent, 10, 5));
}

test "a path at max_path is rejected" {
    const long = [_]u8{'a'} ** max_path;
    try std.testing.expectError(error.PathTooLong, adopt(std.testing.allocator, &long, 0, 1, 8_000, 1));
}

test "create rejects channels == 0, the single guard every constructor shares" {
    try std.testing.expectError(error.InvalidArgument, adopt(std.testing.allocator, "z.wav", 0, 1, 8_000, 0));
}

test "load reads the file range into frames; evict frees them" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    var in: [20]f32 = undefined; // 10 stereo frames
    for (&in, 0..) |*s, i| s.* = @floatFromInt(i);
    const wav_path = test_util.tmpPath(&pb, &tmp, "l.wav");
    try wav.writeFile(wav_path, &in, 8_000, 2, .float32);
    const co = try adopt(std.testing.allocator, wav_path, 2, 5, 8_000, 2);
    defer co.destroy();
    try co.load();
    try std.testing.expectEqual(@as(u64, 40), co.residentBytes());
    try std.testing.expectEqualSlices(f32, in[4..14], co.frames.?);
    try co.load(); // idempotent: no second allocation (testing.allocator would report a leak)
    co.evict();
    try std.testing.expectEqual(@as(?[]f32, null), co.frames);
    try std.testing.expectEqual(@as(u64, 0), co.residentBytes());
}

test "load: n_frames * channels overflowing usize is InvalidArgument, not a trap" {
    // The overflow guard runs before wav.open, so a nonexistent path is
    // fine here — proof the multiply itself never happens.
    const co = try adopt(std.testing.allocator, "huge.wav", 0, std.math.maxInt(u64), 8_000, 2);
    defer co.destroy();
    try std.testing.expectError(error.InvalidArgument, co.load());
}

test "load rejects a file whose channel count disagrees with the checkout's declared channels" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 0, 1, 2, 3, 4, 5, 6, 7 }; // 4 stereo frames
    const wav_path = test_util.tmpPath(&pb, &tmp, "chanmismatch.wav");
    try wav.writeFile(wav_path, &in, 8_000, 2, .float32);
    // Declared mono; the file on disk is stereo.
    const co = try adopt(std.testing.allocator, wav_path, 0, 4, 8_000, 1);
    defer co.destroy();
    try std.testing.expectError(error.InvalidArgument, co.load());
    try std.testing.expectEqual(@as(?[]f32, null), co.frames);
}

test "peakBins from the file rejects a channel mismatch too" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 0, 1, 2, 3, 4, 5, 6, 7 }; // 4 stereo frames
    const wav_path = test_util.tmpPath(&pb, &tmp, "chanmismatch2.wav");
    try wav.writeFile(wav_path, &in, 8_000, 2, .float32);
    const co = try adopt(std.testing.allocator, wav_path, 0, 4, 8_000, 1); // declared mono
    defer co.destroy();
    var out: [3]peaks.PeakBin = undefined; // 3 bins * 1 (declared) channel
    try std.testing.expectError(error.InvalidArgument, co.peakBins(3, &out));
}

test "peakBins from RAM equals peakBins from the file" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    var in: [200]f32 = undefined; // 100 stereo frames
    for (&in, 0..) |*s, i| s.* = @as(f32, @floatFromInt((i * 37) % 101)) / 101.0 - 0.5;
    const wav_path = test_util.tmpPath(&pb, &tmp, "p.wav");
    try wav.writeFile(wav_path, &in, 8_000, 2, .float32);
    const co = try adopt(std.testing.allocator, wav_path, 10, 80, 8_000, 2);
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

test "load after evict allocates exactly once more (no leak under testing.allocator)" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3, 4 };
    const wav_path = test_util.tmpPath(&pb, &tmp, "e.wav");
    try wav.writeFile(wav_path, &in, 8_000, 2, .float32);
    const co = try adopt(std.testing.allocator, wav_path, 0, 2, 8_000, 2);
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

test "source: resident gives a frames sub-slice, evicted gives a file range at the parent offset" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 0, 1, 2, 3, 4, 5, 6, 7 }; // 8 mono frames
    const wav_path = test_util.tmpPath(&pb, &tmp, "s.wav");
    try wav.writeFile(wav_path, &in, 8_000, 1, .float32);
    const co = try adopt(std.testing.allocator, wav_path, 2, 5, 8_000, 1); // frames 2..7
    defer co.destroy();
    switch (co.source(1, 3)) {
        .file => |f| {
            try std.testing.expectEqual(@as(u64, 3), f.start_frame);
            try std.testing.expectEqual(@as(u64, 3), f.n_frames);
            try std.testing.expectEqualSlices(u8, wav_path, f.path);
        },
        .frames => return error.TestUnexpectedResult,
    }
    try co.load();
    switch (co.source(1, 3)) {
        .frames => |f| try std.testing.expectEqualSlices(f32, &[_]f32{ 3, 4, 5 }, f),
        .file => return error.TestUnexpectedResult,
    }
}
