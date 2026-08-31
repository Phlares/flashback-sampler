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
        .hold = 0,
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
    // Subtraction form, not `start + n > parent.n_frames`: start/n arrive
    // raw from ctypes, and the addition can overflow-trap in ReleaseSafe
    // on a hostile value.
    if (n == 0 or start > parent.n_frames or n > parent.n_frames - start) return error.InvalidArgument;
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
