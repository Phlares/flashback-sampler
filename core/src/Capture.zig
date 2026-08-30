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
    // Clear the TEXT too, not only the length. err_buf backs lastError(),
    // and fb_capture_last_error hands lastError()'s pointer straight to
    // ctypes as a C string. A stale byte at position 0 caused the
    // restart-after-error sentinel panic (issue #45); lastError()'s
    // n == 0 guard covers that panic too, but this reset keeps err_buf
    // itself clean, not just the guarded read.
    self.err_buf[0] = 0;
    self.err_len.store(0, .monotonic);
    // `writer_active` is owned by THIS thread, not the worker: the scope
    // that spawns is the scope that joins, so it holds the flag across
    // both. Stored BEFORE the spawn so a flush that lands while the
    // worker is still opening its stream is deferred to the loop top
    // (Ring.flush), never executed under a writer about to appear.
    self.ring.writer_active.store(true, .release);
    errdefer self.ring.writer_active.store(false, .release);
    // std.Thread.spawn takes the function and a tuple of its arguments.
    self.thread = try std.Thread.spawn(.{}, run, .{self});
}

pub fn stop(self: *Capture) void {
    const t = self.thread orelse return;
    self.stop_flag.store(true, .release);
    t.join();
    self.thread = null;
    // Joined: no writer exists. Clear the flag FIRST so a flush landing
    // from here on takes Ring.flush's immediate path, then drain the
    // one that may have been deferred while the loop wound down — on
    // this thread, now the only one touching the ring (issue #20).
    self.ring.writer_active.store(false, .release);
    self.ring.drainPendingFlush();
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
    // setError runs at most once per run() call. Only the n == 0 -> n > 0
    // transition can race a reader: the sentinel check below reads
    // err_buf[n], and n == 0 is exactly the stale value a reader sees
    // while setError (another thread) is mid-way through writing a FIRST
    // error message starting at err_buf[0] (issue #45). This guard skips
    // the sentinel check on that path only; n > 0 below still gets the
    // full, type-checked sentinel slice.
    if (n == 0) return "";
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
    defer stream.deinit();
    defer stream.stop();
    while (!self.stop_flag.load(.acquire)) {
        // Drains a flush deferred while we were mid-loop (see
        // `Ring.drainPendingFlush`'s doc comment) BEFORE polling for the
        // next packet — a silent loopback/process source can leave
        // `stream.next` returning null indefinitely, and a flush must
        // not wait on the source to have something to deliver.
        self.ring.drainPendingFlush();
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

test "capture drains a pending flush even when the source is idle (no packets)" {
    // A silent loopback/process endpoint (WasapiBackend.next returning
    // null, no packet ready) must not stall a flush the user asked for
    // — `Ring.write` is the only other drain site, and it never runs
    // while the source is quiet.
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{}); // no packets, ever
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.running.load(.acquire);
        }
    }.f);
    // Flip the fake's own "stopped" flag WITHOUT calling cap.stop(): this
    // makes every stream.next() call return null immediately (the fast
    // exhausted path in FakeBackend.next) instead of spinning in its own
    // bounded wait — i.e. a source that is up and running but has
    // nothing to deliver, over and over, exactly the no-packet loop
    // iteration this test targets.
    fake.stopped.store(true, .release);
    ring.flush_pending.store(true, .release);
    var spins: u32 = 0;
    while (ring.flush_pending.load(.acquire) and spins < 5_000_000) : (spins += 1) std.Thread.yield() catch {};
    try std.testing.expect(!ring.flush_pending.load(.acquire));
    try std.testing.expectEqual(@as(u64, 0), ring.total_written.load(.acquire)); // no packet ever arrived
    cap.stop();
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

test "a flush between start() and the worker's first stream call is deferred, not executed" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 9, 9 }); // audio the flush must not drop while the worker is still parked
    var fake = FakeBackend.init(&.{&[_]f32{ 1, 1 }});
    fake.hold = .open; // the worker parks inside backend.open until released
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    // Probe from the control thread while the worker has no stream yet.
    try std.testing.expect(ring.writer_active.load(.acquire));
    ring.flush();
    try std.testing.expect(ring.flush_pending.load(.acquire)); // deferred to the writer...
    try std.testing.expectEqual(@as(u64, 1), ring.total_written.load(.acquire)); // ...so nothing was reset yet
    fake.release.store(true, .release);
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.frames_written.load(.acquire) == 1;
        }
    }.f);
    cap.stop();
    // The worker drained the flush at its loop top, then wrote its packet.
    try std.testing.expect(!ring.flush_pending.load(.acquire));
    try std.testing.expectEqual(@as(u64, 1), ring.total_written.load(.acquire));
    var out: [2]f32 = undefined;
    try ring.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 1 }, &out);
    try std.testing.expect(!ring.writer_active.load(.acquire));
}

test "restart after a recorded error: lastError is empty and does not trap on the sentinel" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var fake = FakeBackend.init(&.{});
    fake.open_error = error.DeviceNotFound;
    var cap = Capture.init(&ring, fake.backend(), .{ .kind = .input, .device_id = "", .rate = 48_000, .channels = 2 });
    try cap.start();
    try waitUntil(&cap, struct {
        fn f(c: *Capture) bool {
            return c.err_len.load(.acquire) > 0;
        }
    }.f);
    cap.stop();
    fake.open_error = null;
    try cap.start();
    defer cap.stop();
    // lastError()'s n == 0 guard pins the observable API: no panic, empty
    // string, even though this restart's err_len == 0 races nothing here
    // (single-threaded). err_buf[0] pins the ACTUAL reset in start() that
    // this whole test is about, independent of lastError()'s return shape.
    try std.testing.expectEqualStrings("", cap.lastError());
    try std.testing.expectEqual(@as(u8, 0), cap.err_buf[0]);
}
