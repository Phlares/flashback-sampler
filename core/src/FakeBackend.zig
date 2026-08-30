//! Test double for Backend. Scripted packets, injectable failures,
//! observable lifecycle. Lives in src/ (not a test dir) so Capture.zig's
//! tests can @import it. root.zig's refAllDecls does analyze it, but
//! nothing in abi.zig references it, so none of it is exported from the
//! shared library.
const std = @import("std");
const Backend = @import("Backend.zig");
const FakeBackend = @This();

/// Where the worker parks until `release` is stored true. Lets a test
/// hold the capture thread at a known point — before its stream exists
/// (`.open`) or before a given packet (`.packet = k`) — and probe the
/// control-thread state from outside. Set before start(); the spawn
/// publishes it to the worker.
pub const Hold = union(enum) { none, open, packet: usize };

packets: []const []const f32,
discontinuity_at: ?usize = null,
open_error: ?Backend.Error = null,
mix_rate: u32 = 48_000,
devices: []const Backend.Device = &.{},
opened: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
stopped: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
delivered: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),
last_spec: ?Backend.Spec = null,
hold: Hold = .none,
release: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
/// Per-source doubles for a multi-source owner (Mixer): when set, each
/// open() is forwarded to the next child in order of ARRIVAL, so every
/// capture thread gets its own packet script. Arrival order is whichever
/// thread opens first — tests using this must be symmetric under source
/// order (they are: a sum does not care which addend came first).
children: []const *FakeBackend = &.{},
opens: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),

pub fn init(packets: []const []const f32) FakeBackend {
    return .{ .packets = packets };
}

fn waitRelease(self: *FakeBackend) void {
    // Bounded, like the exhausted-wait in next(): a test that forgets
    // `release` fails instead of hanging. Same bound as waitUntil: on a
    // loaded box 1M yields is not enough for the test thread to get there.
    var spins: u32 = 0;
    while (!self.release.load(.acquire) and spins < 5_000_000) : (spins += 1) std.Thread.yield() catch {};
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
    if (self.children.len > 0) {
        const k = self.opens.fetchAdd(1, .monotonic);
        if (k >= self.children.len) return error.DeviceNotFound;
        return open(self.children[k], spec);
    }
    if (self.open_error) |e| return e;
    if (self.hold == .open) self.waitRelease();
    self.last_spec = spec;
    self.opened.store(true, .release);
    return .{ .ptr = self, .vtable = &stream_vtable };
}

fn next(ptr: *anyopaque, timeout_ms: u32) Backend.Error!?Backend.Packet {
    _ = timeout_ms;
    const self: *FakeBackend = @ptrCast(@alignCast(ptr));
    const i = self.delivered.load(.acquire);
    switch (self.hold) {
        .packet => |k| if (k == i) self.waitRelease(),
        else => {},
    }
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

test "hold = .open parks open() until release" {
    var fake = FakeBackend.init(&.{});
    fake.hold = .open;
    const t = try std.Thread.spawn(.{}, struct {
        fn f(fb: *FakeBackend) void {
            _ = fb.backend().open(.{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 }) catch {};
        }
    }.f, .{&fake});
    // A wide window: the spawned thread has had every chance to open.
    var spins: u32 = 0;
    while (spins < 1000) : (spins += 1) std.Thread.yield() catch {};
    try std.testing.expect(!fake.opened.load(.acquire));
    fake.release.store(true, .release);
    t.join();
    try std.testing.expect(fake.opened.load(.acquire));
}

test "children route each open() to the next child in order" {
    var a = FakeBackend.init(&.{&[_]f32{ 1, 1 }});
    var b = FakeBackend.init(&.{&[_]f32{ 2, 2 }});
    var router = FakeBackend.init(&.{});
    router.children = &.{ &a, &b };
    const spec = Backend.Spec{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 };
    const s1 = try router.backend().open(spec);
    const s2 = try router.backend().open(spec);
    try std.testing.expect(a.opened.load(.acquire));
    try std.testing.expect(b.opened.load(.acquire));
    try std.testing.expect(!router.opened.load(.acquire));
    const p1 = (try s1.next(10)) orelse return error.Expected;
    const p2 = (try s2.next(10)) orelse return error.Expected;
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 1 }, p1.frames);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 2, 2 }, p2.frames);
    try std.testing.expectError(error.DeviceNotFound, router.backend().open(spec)); // a third open has no child
}
