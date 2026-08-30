//! One clip player: a Zig-owned render thread feeding a
//! Backend.RenderStream from an owned copy of the clip. Python binds,
//! plays, pauses, seeks, and reads State; it never sees a frame. The
//! thread never locks or allocates — the clip is allocated in bind() on
//! the control thread, and a two-flag handshake (playing + in_copy)
//! keeps bind() from freeing a buffer the thread is copying from.
const std = @import("std");
const Backend = @import("Backend.zig");
const FakeBackend = @import("FakeBackend.zig");
const ErrorSlot = @import("ErrorSlot.zig");
const Playback = @This();

pub const State = extern struct { running: u8, playing: u8, cursor: u64, clip_frames: u64, mix_rate: u32 };
pub const max_device_id = 256;
pub const max_error = ErrorSlot.max_len;
/// Largest single write. WASAPI's shared-mode buffer at Initialize(0, 0)
/// is a few thousand frames; a bigger `available()` is filled over
/// several wakes rather than sized dynamically.
pub const max_fill_frames = 8192;

allocator: std.mem.Allocator,
backend: Backend.Backend,
rate: u32,
channels: u16,
id_buf: [max_device_id]u8,
id_len: usize,
clip: []f32 = &.{},
clip_frames: std.atomic.Value(u64) = std.atomic.Value(u64).init(0),
cursor: std.atomic.Value(u64) = std.atomic.Value(u64).init(0),
playing: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
in_copy: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
reopen: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
thread: ?std.Thread = null,
stop_flag: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
running: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
done: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
mix_rate: std.atomic.Value(u32) = std.atomic.Value(u32).init(0),
scratch: [max_fill_frames * 2]f32 = undefined,
err: ErrorSlot = .{},

/// The one wait in this file's tests: spin on a POSITIVE predicate until
/// it holds, then stop. `ctx` is whatever the predicate needs (the
/// player, the fake backend, or a small struct holding both), so
/// `@TypeOf(ctx)` types the predicate. A stuck thread fails as
/// error.Timeout instead of hanging the suite, and a wait that ends the
/// moment its condition holds keeps the fake from recording megabytes of
/// writes that slow every test after it.
fn waitFor(ctx: anytype, comptime pred: fn (@TypeOf(ctx)) bool) !void {
    var spins: u32 = 0;
    while (!pred(ctx) and spins < 5_000_000) : (spins += 1) std.Thread.yield() catch {};
    if (!pred(ctx)) return error.Timeout;
}

pub fn init(allocator: std.mem.Allocator, backend: Backend.Backend, spec: Backend.Spec) Playback {
    var self = Playback{
        .allocator = allocator,
        .backend = backend,
        .rate = spec.rate,
        .channels = spec.channels,
        .id_buf = undefined,
        .id_len = 0,
    };
    // Own the id bytes — the caller's slice is a Python str via ctypes
    // and is gone before the thread reads it. Fixed buffer, no allocator.
    const n = @min(spec.device_id.len, max_device_id - 1);
    @memcpy(self.id_buf[0..n], spec.device_id[0..n]);
    self.id_buf[n] = 0;
    self.id_len = n;
    return self;
}

pub fn deinit(self: *Playback) void {
    self.stop();
    self.allocator.free(self.clip);
    self.clip = &.{};
}

/// Named to avoid shadowing init's `spec` parameter — Zig rejects a local
/// that shadows a declaration.
fn currentSpec(self: *const Playback) Backend.Spec {
    return .{ .kind = .render, .device_id = self.id_buf[0..self.id_len], .rate = self.rate, .channels = self.channels };
}

/// Control thread. The ONLY place the clip is allocated or freed.
/// `clip` is an OWNED slice: `dupe` allocates and copies, so the caller's
/// `frames` may die the moment bind returns. The previous slice is handed
/// back to the same allocator, never to a different one.
pub fn bind(self: *Playback, frames: []const f32, rate: u32, channels: u16) !void {
    // Clause order is load-bearing: `channels == 0` must come first, or
    // the modulo below divides by zero.
    if (channels == 0 or channels > 2 or rate == 0 or frames.len % channels != 0) return error.InvalidArgument;
    // Handshake with fill(): clear `playing`, then wait for any copy in
    // flight. fill() raises in_copy BEFORE it reads `playing`, so once we
    // observe in_copy == false after storing playing = false, no copy can
    // start on the old clip. seq_cst on both sides makes the two stores
    // and two loads globally ordered (Dekker's pattern).
    self.playing.store(false, .seq_cst);
    while (self.in_copy.load(.seq_cst)) std.Thread.yield() catch {};
    const copy = try self.allocator.dupe(f32, frames);
    self.allocator.free(self.clip);
    self.clip = copy;
    self.cursor.store(0, .release);
    self.clip_frames.store(frames.len / channels, .release);
    if (rate != self.rate or channels != self.channels) {
        self.rate = rate;
        self.channels = channels;
        // The thread reopens the stream at the new format on its next
        // wake; no stream is opened here (bind may run before play).
        self.reopen.store(true, .release);
    }
}

pub fn play(self: *Playback) !void {
    // A thread that exited (open failed, stream error) is joined here so
    // the next play retries the open instead of silently doing nothing.
    if (self.thread) |t| {
        if (self.done.load(.acquire)) {
            t.join();
            self.thread = null;
        }
    }
    const total = self.clip_frames.load(.acquire);
    if (self.cursor.load(.acquire) >= total) self.cursor.store(0, .release);
    self.playing.store(total > 0, .seq_cst);
    if (self.thread == null) {
        self.stop_flag.store(false, .monotonic);
        self.done.store(false, .monotonic);
        self.err.reset();
        // A spawn that fails leaves no thread to clear `playing`, and
        // run()'s deferred store never happens. Clear it here or the
        // state stays "playing" for the life of the player.
        errdefer self.playing.store(false, .seq_cst);
        self.thread = try std.Thread.spawn(.{}, run, .{self});
    }
}

pub fn pause(self: *Playback) void {
    self.playing.store(false, .seq_cst);
}

pub fn seek(self: *Playback, frames: u64) void {
    self.cursor.store(@min(frames, self.clip_frames.load(.acquire)), .release);
}

pub fn setDevice(self: *Playback, id: []const u8) void {
    const n = @min(id.len, max_device_id - 1);
    @memcpy(self.id_buf[0..n], id[0..n]);
    self.id_buf[n] = 0;
    self.id_len = n;
    self.reopen.store(true, .release);
}

pub fn stop(self: *Playback) void {
    const t = self.thread orelse return;
    self.stop_flag.store(true, .release);
    t.join();
    self.thread = null;
}

pub fn state(self: *const Playback) State {
    return .{
        .running = @intFromBool(self.running.load(.acquire)),
        .playing = @intFromBool(self.playing.load(.acquire)),
        .cursor = self.cursor.load(.acquire),
        .clip_frames = self.clip_frames.load(.acquire),
        .mix_rate = self.mix_rate.load(.acquire),
    };
}

pub fn lastError(self: *const Playback) [:0]const u8 {
    return self.err.last();
}

/// Render thread. Produces `want` frames into scratch. Never allocates.
/// `ch` is the channel count of the OPEN stream, snapshotted by run()
/// after each open: bind() mutates self.channels on the control thread
/// while this stream still owns the old count.
fn fill(self: *Playback, want: usize, ch: usize) []const f32 {
    const out = self.scratch[0 .. want * ch];
    self.in_copy.store(true, .seq_cst);
    defer self.in_copy.store(false, .seq_cst);
    if (!self.playing.load(.seq_cst)) {
        @memset(out, 0);
        return out;
    }
    const total = self.clip_frames.load(.acquire);
    // Bound by the slice at the stream's `ch`, not by clip_frames: a
    // play() or seek() that lands between a channel-changing bind and the
    // reopen must not index past a clip bound at the other count.
    const limit = @min(total, self.clip.len / ch);
    const at = self.cursor.load(.acquire);
    // Stale stream: the cursor counts frames at the NEW channel count
    // while this stream still reads the clip at the OLD one, so a legal
    // seek can sit past `limit`. Output silence and leave the cursor
    // ALONE — writing a clamped value back would move the user's seek
    // (seek(8) on an 8-frame mono clip would become 4 under a stereo
    // stream, and the reopened stream would resume halfway). The reopen
    // is already pending, so this window is at most one fill long.
    if (at > limit) {
        @memset(out, 0);
        return out;
    }
    const n = @min(want, limit - at);
    const src = self.clip[at * ch .. (at + n) * ch];
    @memcpy(out[0 .. n * ch], src);
    @memset(out[n * ch ..], 0);
    self.cursor.store(at + n, .release);
    // Auto-stop, no loop: the UI re-calls play() for LOOP, as today.
    if (at + n >= total) self.playing.store(false, .seq_cst);
    return out;
}

fn run(self: *Playback) void {
    defer {
        // Every exit path (stop, open failure, stream failure) leaves
        // `playing` false: no thread feeds the stream any more. `done` is
        // stored last, after `running` is false, so play()'s join-on-done
        // sees a fully torn-down thread.
        self.playing.store(false, .seq_cst);
        self.done.store(true, .release);
    }
    // `?Backend.RenderStream`: the stream can be absent — before the first
    // open, and between a torn-down stream and its reopen. The optional is
    // what lets the deferred teardown below skip a stream that is not
    // there, instead of running stop()/deinit() on a stale value.
    var stream: ?Backend.RenderStream = self.backend.openRender(self.currentSpec()) catch |e| {
        self.err.set("open failed: {s}", .{@errorName(e)});
        return;
    };
    // An open consumed the pending flag: a reopen requested before the
    // first open is already satisfied.
    self.reopen.store(false, .release);
    self.mix_rate.store(stream.?.mixRate(), .release);
    // The stream's channel count, fixed until the next reopen. fill()
    // takes it as a parameter so a bind() that changes self.channels
    // cannot resize the writes under a stream opened at the old count.
    var ch: usize = self.channels;
    self.running.store(true, .release);
    defer self.running.store(false, .release);
    defer if (stream) |s| {
        s.stop();
        s.deinit();
    };
    while (!self.stop_flag.load(.acquire)) {
        if (self.reopen.swap(false, .acq_rel)) {
            // Reopen on THIS thread: the backend's COM apartment belongs
            // to the thread that opened the stream, so a stream opened on
            // the control thread could not be driven from here.
            if (stream) |s| {
                s.stop();
                s.deinit();
            }
            stream = null;
            self.running.store(false, .release);
            stream = self.backend.openRender(self.currentSpec()) catch |e| {
                self.err.set("open failed: {s}", .{@errorName(e)});
                return;
            };
            self.mix_rate.store(stream.?.mixRate(), .release);
            ch = self.channels;
            self.running.store(true, .release);
        }
        const s = stream.?;
        if (!s.wait(100)) continue;
        const avail = s.available() catch |e| {
            self.err.set("stream failed: {s}", .{@errorName(e)});
            return;
        };
        const want: usize = @min(avail, max_fill_frames);
        if (want == 0) continue;
        s.write(self.fill(want, ch)) catch |e| {
            self.err.set("stream failed: {s}", .{@errorName(e)});
            return;
        };
    }
}

fn ramp(comptime n: usize) [n * 2]f32 {
    var out: [n * 2]f32 = undefined;
    for (0..n) |i| {
        out[i * 2] = @floatFromInt(i + 1);
        out[i * 2 + 1] = -@as(f32, @floatFromInt(i + 1));
    }
    return out;
}

const test_spec = Backend.Spec{ .kind = .render, .device_id = "", .rate = 48_000, .channels = 2 };

/// Reopen waits key on the fake's open COUNT, never on `running`:
/// `running` is true from the first open and goes false only inside the
/// reopen block, so a wait on it can pass before the reopen and pin
/// nothing. The count only moves forward.
fn secondOpen(f: *FakeBackend) bool {
    return f.render_opens.load(.acquire) == 2;
}

/// The render thread has reached its first wait(), so `render_hold` now
/// parks it inside that wait.
fn firstWait(f: *FakeBackend) bool {
    return f.render_waits.load(.acquire) >= 1;
}

test "partial tail zero-pads the last write and auto-stops with the cursor at clip end" {
    var fake = FakeBackend.init(&.{});
    fake.render_allocator = std.testing.allocator;
    defer fake.deinitRender();
    fake.render_available = 4;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const clip = ramp(6);
    try pb.bind(&clip, 48_000, 2);
    try pb.play();
    try waitFor(&pb, struct {
        fn f(p: *Playback) bool {
            return !p.playing.load(.acquire) and p.cursor.load(.acquire) == 6;
        }
    }.f);
    pb.stop();
    try std.testing.expect(fake.written.items.len >= 16);
    try std.testing.expectEqualSlices(f32, &clip, fake.written.items[0..12]);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0, 0, 0, 0 }, fake.written.items[12..16]);
    const st = pb.state();
    try std.testing.expectEqual(@as(u8, 0), st.playing);
    try std.testing.expectEqual(@as(u64, 6), st.cursor);
    try std.testing.expectEqual(@as(u64, 6), st.clip_frames);
}

test "paused: writes are zeros and the cursor does not move" {
    var fake = FakeBackend.init(&.{});
    fake.render_allocator = std.testing.allocator;
    defer fake.deinitRender();
    fake.render_available = 2;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const clip = ramp(100);
    try pb.bind(&clip, 48_000, 2);
    try pb.play();
    try waitFor(&pb, struct {
        fn f(p: *Playback) bool {
            return p.cursor.load(.acquire) >= 2;
        }
    }.f);
    pb.pause();
    // A fill that started before pause() saw `playing` may still advance the cursor; let it finish.
    while (pb.in_copy.load(.seq_cst)) std.Thread.yield() catch {};
    const at = pb.cursor.load(.acquire);
    const writes = fake.render_writes.load(.acquire);
    // Wait for 50 more writes. The target rides in the context: the
    // predicate is a plain function, not a closure over locals.
    const Writes = struct { fake: *FakeBackend, target: usize };
    try waitFor(Writes{ .fake = &fake, .target = writes + 50 }, struct {
        fn f(c: Writes) bool {
            return c.fake.render_writes.load(.acquire) >= c.target;
        }
    }.f);
    try std.testing.expect(fake.render_writes.load(.acquire) > writes);
    try std.testing.expectEqual(at, pb.cursor.load(.acquire));
    pb.stop();
    const tail = fake.written.items[fake.written.items.len - 4 ..];
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0, 0, 0, 0 }, tail);
}

test "seek past end clamps to clip_frames; play at end rewinds to 0" {
    var fake = FakeBackend.init(&.{});
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const clip = ramp(10);
    try pb.bind(&clip, 48_000, 2);
    pb.seek(500);
    try std.testing.expectEqual(@as(u64, 10), pb.state().cursor);
    pb.seek(3);
    try std.testing.expectEqual(@as(u64, 3), pb.state().cursor);
    pb.seek(10);
    fake.render_available = 0; // thread wakes but never writes; cursor stays observable
    try pb.play();
    try std.testing.expectEqual(@as(u64, 0), pb.state().cursor);
    try std.testing.expectEqual(@as(u8, 1), pb.state().playing);
    pb.stop();
}

test "bind while playing pauses, resets the cursor, and replaces the clip" {
    var fake = FakeBackend.init(&.{});
    fake.render_allocator = std.testing.allocator;
    defer fake.deinitRender();
    fake.render_available = 1;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const a = ramp(1000);
    try pb.bind(&a, 48_000, 2);
    try pb.play();
    try waitFor(&pb, struct {
        fn f(p: *Playback) bool {
            return p.cursor.load(.acquire) >= 5;
        }
    }.f);
    const b = ramp(3);
    try pb.bind(&b, 48_000, 2);
    try std.testing.expectEqual(@as(u8, 0), pb.state().playing);
    try std.testing.expectEqual(@as(u64, 0), pb.state().cursor);
    try std.testing.expectEqual(@as(u64, 3), pb.state().clip_frames);
    try std.testing.expectEqualSlices(f32, &b, pb.clip);
    pb.stop();
}

test "rebind at a new rate reopens the stream on the render thread with the new spec" {
    var fake = FakeBackend.init(&.{});
    // The DEVICE mix rate the fake reports, not the rate Playback asks
    // for. It changes between the two opens so each mix_rate publish is
    // pinned on its own: one value would let either store cover the other.
    fake.mix_rate = 44_100;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const a = ramp(4);
    try pb.bind(&a, 48_000, 2);
    try pb.play();
    try waitFor(&pb, struct {
        fn f(p: *Playback) bool {
            return p.running.load(.acquire);
        }
    }.f);
    try std.testing.expectEqual(@as(u32, 1), fake.render_opens.load(.acquire));
    // run() publishes mix_rate before it sets running, so this is safe here.
    try std.testing.expectEqual(@as(u32, 44_100), pb.state().mix_rate);
    // Written before the bind that requests the reopen: the render thread
    // reads it only inside openRender, and `reopen` orders the two.
    fake.mix_rate = 96_000;
    try pb.bind(&a, 96_000, 2);
    try waitFor(&fake, secondOpen);
    try std.testing.expectEqual(@as(u32, 2), fake.render_opens.load(.acquire));
    // The counter moves INSIDE openRender, so the publish that follows it
    // needs its own wait.
    try waitFor(&pb, struct {
        fn f(p: *Playback) bool {
            return p.state().mix_rate == 96_000;
        }
    }.f);
    pb.stop();
    try std.testing.expectEqual(@as(u32, 96_000), pb.state().mix_rate);
    try std.testing.expectEqual(@as(u32, 96_000), fake.last_render_spec.?.rate);
    // Same rate again: no reopen.
    try pb.bind(&a, 96_000, 2);
    try std.testing.expect(!pb.reopen.load(.acquire));
}

test "bind mono at the same rate under a stereo stream reopens and never resizes a write" {
    var fake = FakeBackend.init(&.{});
    fake.render_allocator = std.testing.allocator;
    defer fake.deinitRender();
    fake.render_available = 4;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    // Park the thread INSIDE wait(): past the loop-top reopen check,
    // before fill(). The bind below then lands where a reopen cannot
    // rescue a fill that reads self.channels.
    fake.render_hold = true;
    const stereo = ramp(1000);
    try pb.bind(&stereo, 48_000, 2);
    try pb.play();
    try waitFor(&fake, firstWait);
    try std.testing.expect(fake.render_waits.load(.acquire) == 1);
    // Mono clip, same rate: self.channels flips to 1 while the stereo
    // stream is still open. The next fill must still write 4 * 2 samples
    // (the stream's count); a fill() that read self.channels would hand
    // the stereo stream 4 * 1 samples and the fake rejects that write.
    const mono = [_]f32{ 1, 2, 3, 4, 5, 6, 7, 8 };
    try pb.bind(&mono, 48_000, 1);
    fake.release.store(true, .release);
    try waitFor(&fake, secondOpen);
    try std.testing.expectEqual(@as(u32, 2), fake.render_opens.load(.acquire));
    // The reopen block runs mid-iteration, so exactly one wait/write at
    // the new count happens before the loop top re-reads stop_flag.
    pb.stop();
    try std.testing.expectEqual(@as(u32, 2), fake.render_opens.load(.acquire));
    try std.testing.expectEqual(@as(u16, 1), fake.last_render_spec.?.channels);
    try std.testing.expect(std.mem.indexOf(u8, pb.lastError(), "stream failed") == null);
}

test "a seek past the stream's channel bound outputs silence and keeps the seek target" {
    var fake = FakeBackend.init(&.{});
    fake.render_allocator = std.testing.allocator;
    defer fake.deinitRender();
    fake.render_available = 4;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    fake.render_hold = true;
    const stereo = ramp(1000);
    try pb.bind(&stereo, 48_000, 2);
    try pb.play();
    try waitFor(&fake, firstWait);
    // 8 mono frames = 4 stereo frames. seek(8) is legal against
    // clip_frames, but the parked fill still runs at the stereo stream's
    // ch = 2, where the clip holds only 4 frames. Two failures are
    // pinned here: an unclamped cursor slices clip[16..16] on an 8-sample
    // clip and panics, and a cursor CLAMPED to 4 and written back would
    // silently move the user's seek to the middle of the clip.
    const mono = [_]f32{ 1, 2, 3, 4, 5, 6, 7, 8 };
    try pb.bind(&mono, 48_000, 1);
    // play() before seek(): play() rewinds a cursor already at the end,
    // so seeking first would leave the cursor at 0 and prove nothing.
    try pb.play();
    pb.seek(8);
    fake.release.store(true, .release);
    try waitFor(&fake, secondOpen);
    try std.testing.expectEqual(@as(u32, 2), fake.render_opens.load(.acquire));
    pb.stop();
    try std.testing.expect(std.mem.indexOf(u8, pb.lastError(), "stream failed") == null);
    // The seek target survives both the stale-stream fill and the reopen.
    try std.testing.expectEqual(@as(u64, 8), pb.state().cursor);
    // The cursor sits at the clip end, so NOTHING is ever played: the
    // stale fill writes silence and the reopened stream finds the cursor
    // still at 8. This is what pins the rewind, not the end-state cursor
    // above — a cursor clamped to 4 and written back also ends at 8,
    // because the reopened stream gets there by PLAYING frames 4..8.
    try std.testing.expect(fake.written.items.len >= 12);
    for (fake.written.items) |s| try std.testing.expectEqual(@as(f32, 0), s);
}

test "setDevice copies the id, sets reopen, and the new id reaches the backend" {
    var fake = FakeBackend.init(&.{});
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    pb.setDevice("{hp}");
    try std.testing.expect(pb.reopen.load(.acquire));
    const a = ramp(4);
    try pb.bind(&a, 48_000, 2);
    try pb.play();
    try waitFor(&pb, struct {
        fn f(p: *Playback) bool {
            return p.running.load(.acquire) and !p.reopen.load(.acquire);
        }
    }.f);
    pb.stop();
    try std.testing.expectEqualStrings("{hp}", fake.last_render_spec.?.device_id);
}

test "available() == 0 does not spin into zero-length writes" {
    var fake = FakeBackend.init(&.{});
    fake.render_available = 0;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const a = ramp(4);
    try pb.bind(&a, 48_000, 2);
    try pb.play();
    try waitFor(&fake, struct {
        fn f(k: *FakeBackend) bool {
            return k.render_waits.load(.acquire) > 100;
        }
    }.f);
    try std.testing.expect(fake.render_waits.load(.acquire) > 100);
    try std.testing.expectEqual(@as(usize, 0), fake.render_writes.load(.acquire));
    try std.testing.expect(pb.running.load(.acquire));
    pb.stop();
}

test "openRender failure lands in lastError, running stays false, and the next play retries" {
    var fake = FakeBackend.init(&.{});
    fake.render_open_error = error.DeviceNotFound;
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const a = ramp(4);
    try pb.bind(&a, 48_000, 2);
    try pb.play();
    try waitFor(&pb, struct {
        fn f(p: *Playback) bool {
            return p.done.load(.acquire);
        }
    }.f);
    try std.testing.expectEqualStrings("open failed: DeviceNotFound", pb.lastError());
    try std.testing.expectEqual(@as(u8, 0), pb.state().running);
    fake.render_open_error = null;
    try pb.play();
    try waitFor(&pb, struct {
        fn f(p: *Playback) bool {
            return p.running.load(.acquire);
        }
    }.f);
    try std.testing.expectEqualStrings("", pb.lastError());
    pb.stop();
}

test "bind rejects a bad channel count, a zero channel count, a zero rate, and a ragged frame slice" {
    var fake = FakeBackend.init(&.{});
    var pb = Playback.init(std.testing.allocator, fake.backend(), test_spec);
    defer pb.deinit();
    const a = ramp(4);
    // 6 samples divide evenly by 3, so only the `channels > 2` clause can
    // reject this one. A ragged length here would pass the test for the
    // wrong reason and leave that clause unpinned.
    try std.testing.expectError(error.InvalidArgument, pb.bind(&[_]f32{ 1, 2, 3, 4, 5, 6 }, 48_000, 3));
    try std.testing.expectError(error.InvalidArgument, pb.bind(&[_]f32{ 1, 2 }, 48_000, 0));
    try std.testing.expectError(error.InvalidArgument, pb.bind(&a, 0, 2));
    try std.testing.expectError(error.InvalidArgument, pb.bind(a[0..3], 48_000, 2));
    try std.testing.expectEqual(@as(u64, 0), pb.state().clip_frames);
}
