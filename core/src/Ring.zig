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
