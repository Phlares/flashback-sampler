//! Test double for Backend. Scripted packets, injectable failures,
//! observable lifecycle. Lives in src/ (not a test dir) so Capture.zig's
//! tests can @import it. root.zig's refAllDecls does analyze it, but
//! nothing in abi.zig references it, so none of it is exported from the
//! shared library.
const std = @import("std");
const Backend = @import("Backend.zig");
const FakeBackend = @This();

packets: []const []const f32,
discontinuity_at: ?usize = null,
open_error: ?Backend.Error = null,
mix_rate: u32 = 48_000,
devices: []const Backend.Device = &.{},
opened: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
stopped: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
delivered: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),
last_spec: ?Backend.Spec = null,

pub fn init(packets: []const []const f32) FakeBackend {
    return .{ .packets = packets };
}

pub fn backend(self: *FakeBackend) Backend.Backend {
    return .{ .ptr = self, .vtable = &backend_vtable };
}

const backend_vtable = Backend.Backend.VTable{ .enumerate = enumerate, .open = open };
const stream_vtable = Backend.Stream.VTable{ .next = next, .stop = stop, .deinit = deinit, .mixRate = mixRate };

fn enumerate(ptr: *anyopaque, out: []Backend.Device) usize {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    const n = @min(out.len, self.devices.len);
    @memcpy(out[0..n], self.devices[0..n]);
    return n;
}

fn open(ptr: *anyopaque, spec: Backend.Spec) Backend.Error!Backend.Stream {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    if (self.open_error) |e| return e;
    self.last_spec = spec;
    self.opened.store(true, .release);
    return .{ .ptr = self, .vtable = &stream_vtable };
}

fn next(ptr: *anyopaque, timeout_ms: u32) Backend.Error!?Backend.Packet {
    _ = timeout_ms;
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    const i = self.delivered.load(.acquire);
    if (i < self.packets.len) {
        self.delivered.store(i + 1, .release);
        return .{ .frames = self.packets[i], .discontinuity = (self.discontinuity_at orelse std.math.maxInt(usize)) == i };
    }
    // Exhausted: behave like a quiet device until stop() — a bounded wait
    // so a test that forgets stop() fails instead of hanging.
    var spins: u32 = 0;
    while (!self.stopped.load(.acquire) and spins < 1_000_000) : (spins += 1) std.Thread.yield() catch {};
    return null;
}

fn stop(ptr: *anyopaque) void {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    self.stopped.store(true, .release);
}

fn deinit(ptr: *anyopaque) void {
    _ = ptr;
}

fn mixRate(ptr: *anyopaque) u32 {
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    return self.mix_rate;
}

test "fake backend hands out packets in order then null after stop" {
    var fake = FakeBackend.init(&.{ &[_]f32{ 1, 1 }, &[_]f32{ 2, 2 } });
    const be = fake.backend();
    const stream = try be.open(.{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 });
    defer stream.deinit();
    try std.testing.expect(fake.opened.load(.acquire));
    const p1 = (try stream.next(10)) orelse return error.Expected;
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 1 }, p1.frames);
    const p2 = (try stream.next(10)) orelse return error.Expected;
    try std.testing.expectEqualSlices(f32, &[_]f32{ 2, 2 }, p2.frames);
    stream.stop();
    try std.testing.expectEqual(@as(?Backend.Packet, null), try stream.next(10));
    try std.testing.expectEqual(@as(u32, 48_000), stream.mixRate());
}

test "fake backend open_error propagates" {
    var fake = FakeBackend.init(&.{});
    fake.open_error = error.DeviceNotFound;
    try std.testing.expectError(error.DeviceNotFound, fake.backend().open(.{ .kind = .input, .device_id = "x", .rate = 48_000, .channels = 2 }));
}

test "fake backend enumerate copies its device list" {
    var fake = FakeBackend.init(&.{});
    var dev = std.mem.zeroes(Backend.Device);
    dev.kind = @intFromEnum(Backend.Kind.loopback);
    dev.mix_rate = 44_100;
    fake.devices = &.{dev};
    var out: [4]Backend.Device = undefined;
    try std.testing.expectEqual(@as(usize, 1), fake.backend().enumerate(&out));
    try std.testing.expectEqual(@as(u32, 44_100), out[0].mix_rate);
}
