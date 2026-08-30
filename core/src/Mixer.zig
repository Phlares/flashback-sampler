//! N capture sources summed into one target Ring on a Zig-owned mixer
//! thread. Each source is a Capture writing its own small staging Ring;
//! every tick the mixer reads the span ALL stages have in common, sums
//! it, clips to [-1, 1], and writes the target. Python never sees a
//! frame or a staging ring: it creates, starts, stops, and polls stats().
//!
//! Allocation happens in init() only (the staging rings). The loop works
//! in the fixed `scratch`/`sum` arrays: no lock, no allocation, no error
//! path — the same RT rule Ring.write and Capture.run follow.
//!
//! SELF-REFERENTIAL: each Capture holds a pointer to its staging ring
//! inside `sources`, so a Mixer is initialised IN PLACE (`init` takes
//! `*Mixer`) and must never be moved afterwards. Hosts allocate the
//! struct first (the ABI: allocator.create; tests: a stack variable).
const std = @import("std");
const Ring = @import("Ring.zig");
const Backend = @import("Backend.zig");
const Capture = @import("Capture.zig");
const FakeBackend = @import("FakeBackend.zig");
const Mixer = @This();

pub const max_sources = 8;
pub const stage_seconds = 2.0;
pub const tick_ms = 10;
pub const max_error = Capture.max_error;

const Source = struct { capture: Capture, stage: Ring, cursor: u64 };

// The tick's sleep. Zig 0.16 has no std.Thread.sleep; blocking waits
// live under std.Io. This is the same single-threaded Io singleton
// abi.zig's wav mutex holds; its `sleep` is a real OS wait (see
// std/Io/Threaded.zig's vtable), not a spin. Mixer talks to Backend.zig
// only and never imports wasapi.zig, so kernel32 Sleep is not an option.
const io = std.Io.Threaded.global_single_threaded.io();

allocator: std.mem.Allocator,
target: *Ring, // host-owned; never freed here
sources: [max_sources]Source,
n_sources: u8,
// One source's read for one tick, and the running sum. Sized for the
// largest single publish Ring.write makes (max_write_frames) at the
// widest channel count Ring.init accepts (2).
scratch: [Ring.max_write_frames * 2]f32,
sum: [Ring.max_write_frames * 2]f32,
thread: ?std.Thread,
stop_flag: std.atomic.Value(bool),
running: std.atomic.Value(bool),
frames_written: std.atomic.Value(u64),
xruns: std.atomic.Value(u32),
err_buf: [max_error]u8,
err_len: std.atomic.Value(usize),

pub fn init(self: *Mixer, allocator: std.mem.Allocator, backend: Backend.Backend, target: *Ring, specs: []const Backend.Spec) !void {
    if (specs.len == 0 or specs.len > max_sources) return error.InvalidArgument;
    self.* = .{
        .allocator = allocator,
        .target = target,
        .sources = undefined,
        .n_sources = @intCast(specs.len),
        .scratch = undefined,
        .sum = undefined,
        .thread = null,
        .stop_flag = std.atomic.Value(bool).init(false),
        .running = std.atomic.Value(bool).init(false),
        .frames_written = std.atomic.Value(u64).init(0),
        .xruns = std.atomic.Value(u32).init(0),
        .err_buf = [_]u8{0} ** max_error,
        .err_len = std.atomic.Value(usize).init(0),
    };
    // errdefer with a progress counter: if Ring.init fails on source k,
    // only stages 0..k-1 exist and only those are freed.
    var built: usize = 0;
    errdefer for (self.sources[0..built]) |*s| s.stage.deinit();
    for (specs, 0..) |spec, i| {
        const s = &self.sources[i];
        s.stage = try Ring.init(allocator, .{ .sample_rate = target.sample_rate, .channels = target.channels, .seconds = stage_seconds });
        built += 1;
        // Capture copies spec.device_id into its own buffer; the caller's
        // slice may die after this call.
        s.capture = Capture.init(&s.stage, backend, spec);
        s.cursor = 0;
    }
}

pub fn deinit(self: *Mixer) void {
    self.stop();
    for (self.sources[0..self.n_sources]) |*s| s.stage.deinit();
    self.* = undefined; // poison, like Ring.deinit
}

pub fn start(self: *Mixer) !void {
    if (self.thread != null) return error.AlreadyRunning;
    self.stop_flag.store(false, .monotonic);
    self.err_buf[0] = 0;
    self.err_len.store(0, .monotonic);
    // Control-thread ownership of the target's writer flag (Ring.flush):
    // registered before any thread that could write the target exists,
    // cleared by the errdefer if anything below fails.
    self.target.writer_active.store(true, .release);
    errdefer self.target.writer_active.store(false, .release);
    // Same progress-counter unwind as init. errdefers run LIFO, so a
    // failure stops the started captures FIRST, then clears the flag.
    var started: usize = 0;
    errdefer for (self.sources[0..started]) |*s| s.capture.stop();
    for (self.sources[0..self.n_sources]) |*s| {
        s.capture.start() catch |e| {
            self.setError("source start failed: {s}", .{@errorName(e)});
            return e;
        };
        started += 1;
    }
    self.thread = std.Thread.spawn(.{}, run, .{self}) catch |e| {
        self.setError("spawn failed: {s}", .{@errorName(e)});
        return e;
    };
}

pub fn stop(self: *Mixer) void {
    const t = self.thread orelse return;
    self.stop_flag.store(true, .release);
    t.join();
    self.thread = null;
    for (self.sources[0..self.n_sources]) |*s| s.capture.stop();
    // Joined: no target writer exists. Clear first, then drain — same
    // order and reason as Capture.stop.
    self.target.writer_active.store(false, .release);
    self.target.drainPendingFlush();
}

pub fn stats(self: *const Mixer) Capture.Stats {
    var xruns = self.xruns.load(.acquire);
    for (self.sources[0..self.n_sources]) |*s| xruns += s.capture.stats().xruns;
    return .{
        .running = @intFromBool(self.running.load(.acquire)),
        .frames_written = self.frames_written.load(.acquire),
        .xruns = xruns,
        .mix_rate = self.sources[0].capture.stats().mix_rate,
    };
}

pub fn lastError(self: *const Mixer) [:0]const u8 {
    const n = self.err_len.load(.acquire);
    if (n > 0) return self.err_buf[0..n :0];
    for (self.sources[0..self.n_sources]) |*s| {
        const e = s.capture.lastError();
        if (e.len > 0) return e;
    }
    return self.err_buf[0..0 :0];
}

fn setError(self: *Mixer, comptime fmt: []const u8, args: anytype) void {
    const s = std.fmt.bufPrintZ(self.err_buf[0..], fmt, args) catch self.err_buf[0 .. max_error - 1 :0];
    self.err_len.store(s.len, .release);
}

fn run(self: *Mixer) void {
    self.running.store(true, .release);
    defer self.running.store(false, .release);
    const ch: u64 = self.target.channels;
    while (!self.stop_flag.load(.acquire)) {
        // The mixer is the target's registered writer, so a control-thread
        // flush is deferred to us; drain it before sleeping so a flush
        // never waits on the sources to produce (same rule as Capture.run).
        self.target.drainPendingFlush();
        std.Io.sleep(io, .fromMilliseconds(tick_ms), .awake) catch {};
        // Common span: the frames EVERY stage has that we have not consumed,
        // capped at one Ring.write publish per tick.
        var n: u64 = Ring.max_write_frames;
        for (self.sources[0..self.n_sources]) |*s| {
            const tw = s.stage.total_written.load(.acquire);
            var avail = tw - s.cursor; // stages are never flushed: tw only grows
            if (avail > s.stage.capacity) {
                // The stage lapped our cursor: we fell more than stage_seconds
                // behind. Resume at the oldest frame still readable.
                s.cursor = tw - s.stage.capacity;
                avail = s.stage.capacity;
                _ = self.xruns.fetchAdd(1, .monotonic);
            }
            n = @min(n, avail);
        }
        if (n == 0) continue;
        const floats: usize = @intCast(n * ch);
        @memset(self.sum[0..floats], 0);
        var complete = true;
        for (self.sources[0..self.n_sources]) |*s| {
            // Seqlock read: never blocks the capture thread. A failure means
            // the stage lapped us between the check above and this copy;
            // give up the tick — the next one re-derives every cursor.
            s.stage.read(s.cursor, self.scratch[0..floats]) catch {
                complete = false;
                break;
            };
            for (self.sum[0..floats], self.scratch[0..floats]) |*acc, x| acc.* += x;
        }
        if (!complete) continue;
        for (self.sum[0..floats]) |*x| x.* = std.math.clamp(x.*, -1.0, 1.0);
        self.target.write(self.sum[0..floats]);
        for (self.sources[0..self.n_sources]) |*s| s.cursor += n;
        _ = self.frames_written.fetchAdd(n, .release);
    }
}

fn waitUntil(m: *Mixer, comptime pred: fn (*Mixer) bool) !void {
    var spins: u32 = 0;
    while (!pred(m) and spins < 5_000_000) : (spins += 1) std.Thread.yield() catch {};
    if (!pred(m)) return error.Timeout;
}

const test_spec = Backend.Spec{ .kind = .loopback, .device_id = "", .rate = 100, .channels = 1 };

test "init rejects zero sources and more than max_sources; builds a 2 s stage per spec at the target's format" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var fake = FakeBackend.init(&.{});
    var m: Mixer = undefined;
    try std.testing.expectError(error.InvalidArgument, m.init(std.testing.allocator, fake.backend(), &target, &.{}));
    const too_many = [_]Backend.Spec{test_spec} ** (max_sources + 1);
    try std.testing.expectError(error.InvalidArgument, m.init(std.testing.allocator, fake.backend(), &target, &too_many));
    try m.init(std.testing.allocator, fake.backend(), &target, &.{ test_spec, test_spec });
    defer m.deinit();
    try std.testing.expectEqual(@as(u8, 2), m.n_sources);
    try std.testing.expectEqual(@as(u64, 200), m.sources[0].stage.capacity); // 2 s at 100 Hz
    try std.testing.expectEqual(@as(u16, 1), m.sources[1].stage.channels);
    try std.testing.expectEqual(@as(u64, 0), m.sources[1].cursor);
    try std.testing.expectEqual(@as(u8, 0), m.stats().running);
}

test "skewed sources: the target gets the common span, summed" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    // Dyadic values: every sum below is exact in f32.
    var a = FakeBackend.init(&.{ &[_]f32{ 0.125, 0.25, 0.375, 0.5 }, &[_]f32{ 0.625, 0.75, 0.875, 1.0 } }); // 8 frames
    var b = FakeBackend.init(&.{&[_]f32{ 0.0625, 0.0625, 0.0625, 0.0625, 0.0625 }}); // 5 frames
    var router = FakeBackend.init(&.{});
    router.children = &.{ &a, &b };
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, router.backend(), &target, &.{ test_spec, test_spec });
    defer m.deinit();
    try m.start();
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 5;
        }
    }.f);
    m.stop();
    // 5 is the common span: b never delivers a 6th frame, so a's last 3 stay unmixed.
    try std.testing.expectEqual(@as(u64, 5), target.total_written.load(.acquire));
    var out: [5]f32 = undefined;
    try target.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0.1875, 0.3125, 0.4375, 0.5625, 0.6875 }, &out);
    try std.testing.expectEqual(@as(u64, 5), m.sources[0].cursor);
    try std.testing.expectEqual(@as(u64, 5), m.sources[1].cursor);
}

test "the sum is clipped to [-1, 1]" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 2, .seconds = 1.0 });
    defer target.deinit();
    var a = FakeBackend.init(&.{&[_]f32{ 0.75, -0.75 }}); // one stereo frame
    var b = FakeBackend.init(&.{&[_]f32{ 0.5, -0.5 }});
    var router = FakeBackend.init(&.{});
    router.children = &.{ &a, &b };
    const stereo = Backend.Spec{ .kind = .loopback, .device_id = "", .rate = 100, .channels = 2 };
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, router.backend(), &target, &.{ stereo, stereo });
    defer m.deinit();
    try m.start();
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 1;
        }
    }.f);
    m.stop();
    var out: [2]f32 = undefined;
    try target.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1.0, -1.0 }, &out);
}
