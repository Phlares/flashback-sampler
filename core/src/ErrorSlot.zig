//! ErrorSlot.zig — a fixed, allocation-free error message slot shared by
//! every worker (Capture, Mixer, Playback). The worker thread writes it at
//! most once per run; the control thread reads it at any time.
const std = @import("std");
const ErrorSlot = @This();

pub const max_len = 256;

buf: [max_len]u8 = [_]u8{0} ** max_len,
len: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),

/// Control thread, before a (re)start: clears the text AND the length.
pub fn reset(self: *ErrorSlot) void {
    self.buf[0] = 0;
    self.len.store(0, .monotonic);
}

/// Worker thread. bufPrintZ into the fixed buffer — no allocation. On
/// overflow 0.16's bufPrintZ leaves buf FULL with no sentinel, so the
/// fallback writes the terminator itself before taking the [:0] slice.
pub fn set(self: *ErrorSlot, comptime fmt: []const u8, args: anytype) void {
    const s = std.fmt.bufPrintZ(self.buf[0..], fmt, args) catch blk: {
        self.buf[max_len - 1] = 0;
        break :blk self.buf[0 .. max_len - 1 :0];
    };
    self.len.store(s.len, .release);
}

/// "" when no error is recorded. The n == 0 guard is load-bearing: the
/// sentinel check of buf[0..n :0] READS buf[n], and n == 0 is exactly the
/// stale length a reader sees while set() is mid-way through a first
/// message at buf[0] (issue #45).
pub fn last(self: *const ErrorSlot) [:0]const u8 {
    const n = self.len.load(.acquire);
    if (n == 0) return "";
    return self.buf[0..n :0];
}

test "last returns empty on a fresh slot; reset on a fresh slot is a no-op" {
    var slot = ErrorSlot{};
    // Models set() mid-write on a first message: buf[0] holds a stray byte
    // while len is still 0. The n == 0 guard must skip the sentinel read
    // and return "" without touching buf[0] at all.
    slot.buf[0] = 'x';
    try std.testing.expectEqualStrings("", slot.last());
    slot.reset();
    try std.testing.expectEqualStrings("", slot.last());
}

test "set then last round-trips the formatted text with a sentinel" {
    var slot = ErrorSlot{};
    slot.set("open failed: {s}", .{"DeviceNotFound"});
    try std.testing.expectEqualStrings("open failed: DeviceNotFound", slot.last());
    try std.testing.expectEqual(slot.last().len, slot.len.load(.acquire));
}

test "overflow truncates to max_len - 1 and restores the sentinel byte" {
    var slot = ErrorSlot{};
    slot.set("{s}", .{"x" ** 300});
    try std.testing.expectEqual(@as(usize, max_len - 1), slot.last().len);
    try std.testing.expectEqual(@as(u8, 0), slot.buf[max_len - 1]);
}

test "reset after set returns last to empty and clears buf[0]" {
    var slot = ErrorSlot{};
    slot.set("boom", .{});
    slot.reset();
    try std.testing.expectEqualStrings("", slot.last());
    try std.testing.expectEqual(@as(u8, 0), slot.buf[0]);
}
