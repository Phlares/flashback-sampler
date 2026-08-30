//! One capture source: a Zig-owned thread pulling packets from a
//! Backend.Stream and writing them into a Ring. Python never sees a
//! frame; it starts/stops this and polls stats(). All shared state is
//! atomics — the loop never locks, allocates, or fails.
const std = @import("std");
const Ring = @import("Ring.zig");
const Backend = @import("Backend.zig");
const FakeBackend = @import("FakeBackend.zig");
const Capture = @This();

pub const Stats = extern struct { running: u8, frames_written: u64, xruns: u32, mix_rate: u32 };
pub const max_device_id = 256;
pub const max_error = 256;

ring: *Ring,
backend: Backend.Backend,
kind: Backend.Kind,
pid: u32,
rate: u32,
channels: u16,
id_buf: [max_device_id]u8,
id_len: usize,
thread: ?std.Thread = null,
stop_flag: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
running: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
frames_written: std.atomic.Value(u64) = std.atomic.Value(u64).init(0),
xruns: std.atomic.Value(u32) = std.atomic.Value(u32).init(0),
mix_rate: std.atomic.Value(u32) = std.atomic.Value(u32).init(0),
err_buf: [max_error]u8 = [_]u8{0} ** max_error,
err_len: std.atomic.Value(usize) = std.atomic.Value(usize).init(0),

fn waitUntil(cap: *Capture, comptime pred: fn (*Capture) bool) !void {
    var spins: u32 = 0;
    while (!pred(cap) and spins < 5_000_000) : (spins += 1) std.Thread.yield() catch {};
    if (!pred(cap)) return error.Timeout;
}

pub fn init(ring: *Ring, backend: Backend.Backend, spec: Backend.Spec) Capture {
    var self = Capture{
        .ring = ring,
        .backend = backend,
        .kind = spec.kind,
        .pid = spec.pid,
        .rate = spec.rate,
        .channels = spec.channels,
        .id_buf = undefined,
        .id_len = 0,
    };
    // Own the id bytes: the caller's slice (a Python str via ctypes) is
    // gone by the time the thread reads it. Fixed buffer, no allocator.
    const n = @min(spec.device_id.len, max_device_id - 1);
    @memcpy(self.id_buf[0..n], spec.device_id[0..n]);
    self.id_buf[n] = 0;
    self.id_len = n;
    return self;
}

/// Named to avoid shadowing init's `spec` parameter — Zig rejects a local
/// that shadows a declaration.
fn currentSpec(self: *const Capture) Backend.Spec {
    return .{ .kind = self.kind, .device_id = self.id_buf[0..self.id_len], .pid = self.pid, .rate = self.rate, .channels = self.channels };
}

pub fn start(self: *Capture) !void {
    if (self.thread != null) return error.AlreadyRunning;
    self.stop_flag.store(false, .monotonic);
    self.err_len.store(0, .monotonic);
    // std.Thread.spawn takes the function and a tuple of its arguments.
    self.thread = try std.Thread.spawn(.{}, run, .{self});
}

pub fn stop(self: *Capture) void {
    const t = self.thread orelse return;
    self.stop_flag.store(true, .release);
    t.join();
    self.thread = null;
    // The join guarantees `run`'s defers (writer_active -> false) have
    // already executed, so a flush that arrived while the loop was
    // winding down and got deferred (issue #20) is drained HERE,
    // immediately, rather than lost until some future writer.
    if (self.ring.flush_pending.load(.acquire)) {
        self.ring.flush_pending.store(false, .release);
        self.ring.flush();
    }
}

pub fn stats(self: *const Capture) Stats {
    return .{
        .running = @intFromBool(self.running.load(.acquire)),
        .frames_written = self.frames_written.load(.acquire),
        .xruns = self.xruns.load(.acquire),
        .mix_rate = self.mix_rate.load(.acquire),
    };
}

pub fn lastError(self: *const Capture) [:0]const u8 {
    const n = self.err_len.load(.acquire);
    return self.err_buf[0..n :0];
}

fn setError(self: *Capture, comptime fmt: []const u8, args: anytype) void {
    // bufPrintZ into a fixed buffer: no allocation on the audio thread.
    const s = std.fmt.bufPrintZ(self.err_buf[0..], fmt, args) catch self.err_buf[0 .. max_error - 1 :0];
    self.err_len.store(s.len, .release);
}

fn run(self: *Capture) void {
    const stream = self.backend.open(self.currentSpec()) catch |e| {
        self.setError("open failed: {s}", .{@errorName(e)});
        return;
    };
    self.mix_rate.store(stream.mixRate(), .release);
    self.running.store(true, .release);
    defer self.running.store(false, .release);
    // Tells Ring.flush() to defer to us instead of resetting directly
    // (issue #20) — declared AFTER the `running` defer so it runs FIRST
    // (defers are LIFO): writer_active must go false before running
    // does, so a flush arriving right at shutdown is never accepted as
    // "no writer" while a write from this loop could still be in flight.
    self.ring.writer_active.store(true, .release);
    defer self.ring.writer_active.store(false, .release);
    defer stream.deinit();
    defer stream.stop();
    while (!self.stop_flag.load(.acquire)) {
        const maybe = stream.next(100) catch |e| {
            self.setError("stream failed: {s}", .{@errorName(e)});
            return;
        };
        const pkt = maybe orelse continue;
        if (pkt.discontinuity) _ = self.xruns.fetchAdd(1, .monotonic);
        self.ring.write(pkt.frames);
        _ = self.frames_written.fetchAdd(pkt.frames.len / self.ring.channels, .release);
    }
}

test "capture writes every packet into the ring and counts frames" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{ &[_]f32{ 0.1, 0.2, 0.3, 0.4 }, &[_]f32{ 0.5, 0.6 } });
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.frames_written.load(.acquire) == 3;
        }
    }.f);
    cap.stop();
    try std.testing.expectEqual(@as(u64, 3), ring.total_written.load(.acquire));
    var out: [6]f32 = undefined;
    try ring.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0.1, 0.2, 0.3, 0.4, 0.5, 0.6 }, &out);
    try std.testing.expectEqual(@as(u8, 0), cap.stats().running);
}

test "stop is idempotent and joins; running flips false" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{});
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .input, .device_id = "dev", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.running.load(.acquire);
        }
    }.f);
    cap.stop();
    cap.stop();
    try std.testing.expectEqual(@as(u8, 0), cap.stats().running);
    try std.testing.expect(fake.stopped.load(.acquire));
}

test "open failure lands in lastError and running stays false" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{});
    fake.open_error = error.DeviceNotFound;
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .input, .device_id = "gone", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.err_len.load(.acquire) > 0;
        }
    }.f);
    cap.stop();
    try std.testing.expectEqualStrings("open failed: DeviceNotFound", cap.lastError());
    try std.testing.expectEqual(@as(u8, 0), cap.stats().running);
}

test "discontinuity flag increments xruns; mix_rate is published" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{ &[_]f32{ 0, 0 }, &[_]f32{ 0, 0 }, &[_]f32{ 0, 0 } });
    fake.discontinuity_at = 1;
    fake.mix_rate = 44_100;
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.frames_written.load(.acquire) == 3;
        }
    }.f);
    cap.stop();
    const s = cap.stats();
    try std.testing.expectEqual(@as(u32, 1), s.xruns);
    try std.testing.expectEqual(@as(u32, 44_100), s.mix_rate);
}

test "spec's device_id and pid reach the backend" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{});
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .process, .device_id = "{abc}", .pid = 4242, .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.running.load(.acquire);
        }
    }.f);
    cap.stop();
    const spec = fake.last_spec orelse return error.Expected;
    try std.testing.expectEqualStrings("{abc}", spec.device_id);
    try std.testing.expectEqual(@as(u32, 4242), spec.pid);
    try std.testing.expectEqual(Backend.Kind.process, spec.kind);
}

test "capture marks the ring's writer active for the life of the loop, and stop drains a pending flush" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{&[_]f32{ 1, 1 }});
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .input, .device_id = "", .rate = 48_000, .channels = 2 });
    try std.testing.expect(!ring.writer_active.load(.acquire));
    try cap.start();
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.frames_written.load(.acquire) == 1;
        }
    }.f);
    try std.testing.expect(ring.writer_active.load(.acquire));
    try std.testing.expectEqual(@as(u64, 1), ring.total_written.load(.acquire));
    // A flush that arrives while the loop is winding down must not be lost:
    // stop() drains it after the join, when the writer is inactive.
    ring.flush_pending.store(true, .release);
    cap.stop();
    try std.testing.expect(!ring.writer_active.load(.acquire));
    try std.testing.expectEqual(@as(u64, 0), ring.total_written.load(.acquire));
    try std.testing.expect(!ring.flush_pending.load(.acquire));
}

test "start twice is AlreadyRunning" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{});
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .input, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    defer cap.stop();
    try std.testing.expectError(error.AlreadyRunning, cap.start());
}
