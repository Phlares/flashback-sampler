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
const ErrorSlot = @import("ErrorSlot.zig");
const Mixer = @This();

pub const max_sources = 8;
pub const stage_seconds = 2.0;
pub const tick_ms = 10;
pub const max_error = ErrorSlot.max_len;

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
err: ErrorSlot,

pub fn init(self: *Mixer, allocator: std.mem.Allocator, backend: Backend.Backend, target: *Ring, specs: []const Backend.Spec) !void {
    if (specs.len == 0 or specs.len > max_sources) return error.InvalidArgument;
    // A Capture opens its device at the spec's own rate/channels and
    // nothing resamples or remaps: a spec that disagrees with the
    // target's format would mix garbage into it silently.
    for (specs) |spec| {
        if (spec.rate != target.sample_rate or spec.channels != target.channels) return error.InvalidArgument;
    }
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
        .err = .{},
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
    self.err.reset();
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
    for (self.sources[0..self.n_sources]) |*s| xruns +|= s.capture.stats().xruns;
    return .{
        .running = @intFromBool(self.running.load(.acquire)),
        .frames_written = self.frames_written.load(.acquire),
        .xruns = xruns,
        .mix_rate = self.sources[0].capture.stats().mix_rate,
    };
}

pub fn lastError(self: *const Mixer) [:0]const u8 {
    const own = self.err.last();
    if (own.len > 0) return own;
    for (self.sources[0..self.n_sources]) |*s| {
        const e = s.capture.lastError();
        if (e.len > 0) return e;
    }
    return "";
}

fn setError(self: *Mixer, comptime fmt: []const u8, args: anytype) void {
    self.err.set(fmt, args);
}

fn run(self: *Mixer) void {
    self.running.store(true, .release);
    defer self.running.store(false, .release);
    const ch: u64 = self.target.channels;
    while (!self.stop_flag.load(.acquire)) {
        // The mixer is the target's registered writer, so a control-thread
        // flush is deferred to us; drain it before sleeping so a flush
        // never waits on the sources to produce (same rule as
        // Capture.runStream's loop top).
        self.target.drainPendingFlush();
        std.Io.sleep(io, .fromMilliseconds(tick_ms), .awake) catch {};
        // Common span: the frames EVERY stage has that we have not consumed,
        // capped at one Ring.write publish per tick.
        var n: u64 = Ring.max_write_frames;
        for (self.sources[0..self.n_sources]) |*s| {
            const tw = s.stage.total_written.load(.acquire);
            // Stages are never flushed, so tw only grows and this can never
            // wrap. Saturating anyway: were a stage ever reset behind us,
            // `avail` pins to 0 and the mixer stalls until the cursor is
            // overtaken again — a stall beats an overflow abort.
            var avail = tw -| s.cursor;
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

test "init rejects a spec whose rate or channels does not match the target's format" {
    // A Capture opens its device at the SPEC's own rate/channels and
    // nothing resamples or remaps -- a mismatched spec would mix garbage
    // into the target silently. Each spec below disagrees on exactly one
    // field so both checks are pinned independently.
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var fake = FakeBackend.init(&.{});
    var m: Mixer = undefined;
    const wrong_channels = Backend.Spec{ .kind = .loopback, .device_id = "", .rate = 100, .channels = 2 };
    try std.testing.expectError(error.InvalidArgument, m.init(std.testing.allocator, fake.backend(), &target, &.{wrong_channels}));
    const wrong_rate = Backend.Spec{ .kind = .loopback, .device_id = "", .rate = 48_000, .channels = 1 };
    try std.testing.expectError(error.InvalidArgument, m.init(std.testing.allocator, fake.backend(), &target, &.{wrong_rate}));
    // A good spec first, a bad one second: pins that the guard loops over
    // EVERY spec, not just specs[0].
    try std.testing.expectError(error.InvalidArgument, m.init(std.testing.allocator, fake.backend(), &target, &.{ test_spec, wrong_rate }));
    try m.init(std.testing.allocator, fake.backend(), &target, &.{test_spec}); // matching spec still inits fine
    defer m.deinit();
}

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

test "a stage that laps the cursor counts one xrun and resumes at the oldest readable frame" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 10.0 });
    defer target.deinit();
    // 300 frames in ONE packet against a 200-frame stage (2 s at 100 Hz):
    // frames 0..99 are gone before the mixer's first tick can read them.
    var big: [300]f32 = undefined;
    for (&big, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i + 1)) / 1000.0;
    var src = FakeBackend.init(&.{&big});
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, src.backend(), &target, &.{test_spec});
    defer m.deinit();
    try m.start();
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 200;
        }
    }.f);
    m.stop();
    try std.testing.expectEqual(@as(u32, 1), m.stats().xruns);
    try std.testing.expectEqual(@as(u64, 300), m.sources[0].cursor); // 100 (oldest valid) + 200 read
    try std.testing.expectEqual(@as(u64, 200), target.total_written.load(.acquire));
    var out: [1]f32 = undefined;
    try target.read(0, &out);
    try std.testing.expectEqual(big[100], out[0]); // the target starts at stage frame 100, not 0
}

test "a flush during mixing is drained by the mixer even while the sources are idle; only post-flush frames remain" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var a = FakeBackend.init(&.{ &[_]f32{ 0.25, 0.25 }, &[_]f32{ 0.5, 0.5 } });
    var b = FakeBackend.init(&.{ &[_]f32{ 0.25, 0.25 }, &[_]f32{ 0.5, 0.5 } });
    a.hold = .{ .packet = 1 }; // both sources park before their second packet
    b.hold = .{ .packet = 1 };
    var router = FakeBackend.init(&.{});
    router.children = &.{ &a, &b };
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, router.backend(), &target, &.{ test_spec, test_spec });
    defer m.deinit();
    try m.start();
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 2;
        }
    }.f);
    try std.testing.expectEqual(@as(u64, 2), target.total_written.load(.acquire));
    target.flush(); // control thread: the mixer is the registered writer, so this is deferred to it
    // Drained at the loop top while no source delivers: a flush must never wait on audio.
    var spins: u32 = 0;
    while (target.flush_pending.load(.acquire) and spins < 5_000_000) : (spins += 1) std.Thread.yield() catch {};
    try std.testing.expect(!target.flush_pending.load(.acquire));
    try std.testing.expectEqual(@as(u64, 0), target.total_written.load(.acquire));
    a.release.store(true, .release);
    b.release.store(true, .release);
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 4;
        }
    }.f);
    m.stop();
    try std.testing.expectEqual(@as(u64, 2), target.total_written.load(.acquire)); // post-flush frames only
    var out: [2]f32 = undefined;
    try target.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1.0, 1.0 }, &out); // 0.5 + 0.5, the SECOND packets
}

test "start failure on source 2 unwinds source 1 and clears writer_active" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var fake = FakeBackend.init(&.{});
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, fake.backend(), &target, &.{ test_spec, test_spec });
    defer m.deinit();
    // Make source 2 already running: the mixer's own start hits AlreadyRunning
    // on it — a real failure through the real path, no fake needed.
    try m.sources[1].capture.start();
    defer m.sources[1].capture.stop(); // runs before m.deinit (defers are LIFO)
    try std.testing.expectError(error.AlreadyRunning, m.start());
    try std.testing.expect(m.sources[0].capture.thread == null); // stopped and joined by the unwind
    try std.testing.expect(m.thread == null);
    try std.testing.expect(!target.writer_active.load(.acquire));
    try std.testing.expectEqualStrings("source start failed: AlreadyRunning", m.lastError());
}

test "writer_active is true from start() through stop(), false after" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var fake = FakeBackend.init(&.{});
    fake.hold = .open;
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, fake.backend(), &target, &.{test_spec});
    defer m.deinit();
    try std.testing.expect(!target.writer_active.load(.acquire));
    try m.start();
    // Probed while the capture is still parked in open(): before any frame can exist.
    try std.testing.expect(target.writer_active.load(.acquire));
    fake.release.store(true, .release);
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.running.load(.acquire);
        }
    }.f);
    try std.testing.expect(target.writer_active.load(.acquire));
    m.stop();
    try std.testing.expect(!target.writer_active.load(.acquire));
    try std.testing.expectEqual(@as(u8, 0), m.stats().running);
    m.stop(); // idempotent
}

test "stats: xruns sums the captures' discontinuities, mix_rate is the first source's, lastError is the first non-empty" {
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 100, .channels = 1, .seconds = 1.0 });
    defer target.deinit();
    var a = FakeBackend.init(&.{ &[_]f32{0}, &[_]f32{0} });
    a.discontinuity_at = 1;
    a.mix_rate = 44_100;
    var b = FakeBackend.init(&.{});
    b.open_error = error.FormatRejected;
    var router = FakeBackend.init(&.{});
    router.children = &.{ &a, &b };
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, router.backend(), &target, &.{ test_spec, test_spec });
    defer m.deinit();
    try m.start();
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.sources[0].capture.stats().xruns + x.sources[1].capture.stats().xruns == 1 and
                (x.sources[0].capture.lastError().len > 0 or x.sources[1].capture.lastError().len > 0);
        }
    }.f);
    m.stop();
    const st = m.stats();
    try std.testing.expectEqual(@as(u32, 1), st.xruns);
    // The router hands a/b out in ARRIVAL order, so which capture got
    // 44_100 is not fixed. Pin "the first source's" against source 0
    // itself, and that the value is one of the two scripted rates.
    try std.testing.expectEqual(m.sources[0].capture.stats().mix_rate, st.mix_rate);
    try std.testing.expect(st.mix_rate == 44_100 or st.mix_rate == 0); // b's open fails, so its capture never publishes a rate
    try std.testing.expectEqualStrings("open failed: FormatRejected", m.lastError());
}

test "one tick's publish is capped at Ring.max_write_frames even when a source has 6_000 frames ready" {
    // Deviates from the brief's own suggested recipe, which flags itself
    // as unreliable ("polling can miss values") and invites a cleaner
    // observable. This one is deterministic, not probabilistic:
    //
    // target: 10 kHz mono, 2 s = 20_000 frames -- large enough to hold the
    // whole packet with headroom. The one source's stage is stage_seconds
    // (2.0 s, a Mixer constant) at 10 kHz = 20_000 frames too, so nothing
    // laps. The source's hold parks its ONE 6_000-frame packet until
    // released -- this, not timing luck, is what makes the pin
    // deterministic: `release` is stored only after `m.start()` returns,
    // so the whole packet lands in ONE Capture write (itself internally
    // chunked into stage publishes of 4096 then 6000 by Ring.write, but
    // that happens in well under a microsecond) LONG before the mixer's
    // next ~10 ms tick can observe it -- the mixer's first successful
    // tick always sees the full 6_000 frames available, never a partial
    // amount. With the cap in place, `n` for that tick is bounded to
    // Ring.max_write_frames (4096), so frames_written must pass through
    // exactly 4096 on its way to 6_000 -- a value it can only ever reach
    // by TWO ticks summing 4096 + 1904. If the cap were removed, the
    // first tick would see avail (6_000) < the mutated cap and take all
    // 6_000 in one shot, and frames_written would jump straight from 0 to
    // 6_000 -- the first waitUntil below would then time out, since it
    // never observes the value 4096.
    var target = try Ring.init(std.testing.allocator, .{ .sample_rate = 10_000, .channels = 1, .seconds = 2.0 });
    defer target.deinit();
    var big: [6_000]f32 = undefined;
    for (&big, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i % 2)); // dyadic; content is never inspected
    var src = FakeBackend.init(&.{&big});
    src.hold = .{ .packet = 0 }; // park before the one packet, released only once the mixer is already running
    const spec = Backend.Spec{ .kind = .loopback, .device_id = "", .rate = 10_000, .channels = 1 };
    var m: Mixer = undefined;
    try m.init(std.testing.allocator, src.backend(), &target, &.{spec});
    defer m.deinit();
    try m.start();
    src.release.store(true, .release);
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == Ring.max_write_frames;
        }
    }.f);
    try waitUntil(&m, struct {
        fn f(x: *Mixer) bool {
            return x.frames_written.load(.acquire) == 6_000;
        }
    }.f);
    m.stop();
    try std.testing.expectEqual(@as(u64, 6_000), target.total_written.load(.acquire));
}
