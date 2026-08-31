//! Scratch.zig — the scratch writer thread and the RAM cache, one per
//! process. Two job kinds ride one intrusive FIFO: `.write` streams a
//! fresh checkout's RAM copy to `<path>.part` and renames it to
//! `<path>`; `.load` reads an evicted checkout back into RAM (preload on
//! select). The same struct holds the LRU byte cache (Task h4).
//!
//! Thread rules (see the plan's PR h header): `mutex` guards the FIFO
//! links, the LRU links, `resident_bytes`, `budget_bytes`, and every
//! `Checkout.job` / `pinned` / `frames` access on the control thread.
//! The worker owns `co.frames` while `co.job == .load` and only reads
//! it while `co.job == .write`. The worker never holds `mutex` during
//! file I/O. No heap for the queue/LRU bookkeeping: the lists are
//! intrusive and the file buffers are wav.zig's stack buffers. Only a
//! `.load` job allocates, and only the clip it is loading
//! (`Checkout.load`'s own allocation) — a `.write` job allocates
//! nothing.
//!
//! Zig 0.16: blocking primitives live under std.Io (`std.Io.Mutex`,
//! `std.Io.Condition`) and take the Io they block on; the singleton
//! `wav.io` is a real futex underneath (see abi.zig's note on the
//! wav_write mutex, now wav.write_mutex).
const std = @import("std");
const wav = @import("wav.zig");
const Checkout = @import("Checkout.zig");

const Scratch = @This();
const io = wav.io;

/// The writer seam: production writes a float32 WAV through wav.writeFile
/// under wav.write_mutex; tests inject a slow or failing writer.
pub const WriteFn = *const fn (path: []const u8, frames: []const f32, rate: u32, channels: u16) anyerror!void;
pub const max_part_path = Checkout.max_path + 5; // ".part"

mutex: std.Io.Mutex = .init,
cond: std.Io.Condition = .init,
queue_head: ?*Checkout = null,
queue_tail: ?*Checkout = null,
lru_head: ?*Checkout = null, // most recently used
lru_tail: ?*Checkout = null, // next to evict
resident_bytes: u64 = 0,
budget_bytes: u64,
thread: ?std.Thread = null,
stop_flag: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
write_fn: WriteFn = &defaultWrite,

pub fn init(budget_bytes: u64) Scratch {
    return .{ .budget_bytes = budget_bytes };
}

/// Control thread. The scope that spawns joins (`stop`).
pub fn start(self: *Scratch) !void {
    if (self.thread != null) return error.AlreadyRunning;
    self.stop_flag.store(false, .monotonic);
    self.thread = try std.Thread.spawn(.{}, run, .{self});
}

/// Control thread. The worker finishes every queued job before it
/// exits, so a quit never leaves a `.part` behind unless the process
/// dies. Blocks for the drain.
pub fn stop(self: *Scratch) void {
    const t = self.thread orelse return;
    self.stop_flag.store(true, .release);
    self.mutex.lockUncancelable(io);
    self.cond.broadcast(io);
    self.mutex.unlock(io);
    t.join();
    self.thread = null;
}

/// Queue `job` for `co`. A checkout already queued is left alone. A
/// `.write` submission is the moment a root becomes resident in the
/// cache's eyes: its bytes are counted and it is linked at the LRU head.
pub fn submit(self: *Scratch, co: *Checkout, job: Checkout.Job) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    self.submitLocked(co, job);
}

fn submitLocked(self: *Scratch, co: *Checkout, job: Checkout.Job) void {
    if (co.job != .none or job == .none) return;
    co.job = job;
    co.queue_next = null;
    if (self.queue_tail) |t| t.queue_next = co else self.queue_head = co;
    self.queue_tail = co;
    if (job == .write) self.lruInsertHeadLocked(co);
    self.cond.broadcast(io);
}

/// Block while a `.load` job for `co` is queued or running. Never waits
/// on a `.write` (the write only reads `frames`; a bind right after a
/// checkout must not wait for a gigabyte to hit the disk). Ruling
/// R-h4b: a `.load` submitted with no worker running (never started, or
/// already stopped) would otherwise wait forever with nothing left to
/// process it and broadcast — `self.thread` is written only by `start`/
/// `stop` on this same control thread, so reading it here needs no
/// separate lock.
pub fn waitLoad(self: *Scratch, co: *Checkout) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    while (co.job == .load and self.thread != null) self.cond.waitUncancelable(io, &self.mutex);
}

/// Block until `co` has no job at all. Tests and `forget` use it. Same
/// no-worker early-out as `waitLoad` (ruling R-h4b).
pub fn waitJob(self: *Scratch, co: *Checkout) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    while (co.job != .none and self.thread != null) self.cond.waitUncancelable(io, &self.mutex);
}

fn run(self: *Scratch) void {
    while (true) {
        self.mutex.lockUncancelable(io);
        while (self.queue_head == null and !self.stop_flag.load(.acquire)) {
            self.cond.waitUncancelable(io, &self.mutex);
        }
        const co = self.queue_head orelse {
            // stop requested and nothing left: the drain is complete.
            self.mutex.unlock(io);
            return;
        };
        self.queue_head = co.queue_next;
        if (self.queue_head == null) self.queue_tail = null;
        co.queue_next = null;
        const job = co.job;
        self.mutex.unlock(io);

        switch (job) {
            .write => self.doWrite(co),
            .load => self.doLoad(co),
            .none => unreachable, // submitLocked never queues .none
        }

        self.mutex.lockUncancelable(io);
        co.job = .none;
        self.cond.broadcast(io);
        self.evictOverBudgetLocked();
        self.mutex.unlock(io);
    }
}

/// Stream the RAM copy to `<path>.part`, then rename to `<path>`. The
/// rename is the "complete" signal a crash cannot fake: a `.part` on
/// disk at launch is by definition partial. Any failure marks the
/// checkout `failed`; it stays resident and unevictable (Task h4 skips
/// non-written entries), so the audio is never lost to a full disk.
fn doWrite(self: *Scratch, co: *Checkout) void {
    co.write_state.store(.writing, .release);
    const frames = co.frames orelse {
        co.write_state.store(.failed, .release);
        return;
    };
    var pb: [max_part_path]u8 = undefined;
    const part = std.fmt.bufPrint(&pb, "{s}.part", .{co.path()}) catch unreachable;
    self.write_fn(part, frames, co.rate, co.channels) catch {
        co.write_state.store(.failed, .release);
        return;
    };
    std.Io.Dir.cwd().rename(part, std.Io.Dir.cwd(), co.path(), io) catch {
        co.write_state.store(.failed, .release);
        return;
    };
    co.write_state.store(.written, .release);
}

/// Read the file back into RAM. A failure leaves the checkout evicted;
/// the fallback bind reads the file itself. Accounting happens under
/// the mutex once the bytes exist.
fn doLoad(self: *Scratch, co: *Checkout) void {
    co.load() catch return;
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    if (co.frames != null and !self.lruLinkedLocked(co)) self.lruInsertHeadLocked(co);
}

fn defaultWrite(path: []const u8, frames: []const f32, rate: u32, channels: u16) anyerror!void {
    wav.write_mutex.lockUncancelable(wav.io);
    defer wav.write_mutex.unlock(wav.io);
    try wav.writeFile(path, frames, rate, channels, .float32);
}

// ---- LRU (Task h4 fills in pin/touch/budget; the list ops live here) ----

fn lruLinkedLocked(self: *Scratch, co: *Checkout) bool {
    return co.lru_prev != null or co.lru_next != null or self.lru_head == co;
}

fn lruInsertHeadLocked(self: *Scratch, co: *Checkout) void {
    co.lru_prev = null;
    co.lru_next = self.lru_head;
    if (self.lru_head) |h| h.lru_prev = co else self.lru_tail = co;
    self.lru_head = co;
    self.resident_bytes += co.residentBytes();
}

fn lruRemoveLocked(self: *Scratch, co: *Checkout) void {
    if (!self.lruLinkedLocked(co)) return;
    if (co.lru_prev) |p| p.lru_next = co.lru_next else self.lru_head = co.lru_next;
    if (co.lru_next) |n| n.lru_prev = co.lru_prev else self.lru_tail = co.lru_prev;
    co.lru_prev = null;
    co.lru_next = null;
    self.resident_bytes -= co.residentBytes();
}

fn evictOverBudgetLocked(self: *Scratch) void {
    _ = self; // Task h4
}

// ---- tests ----

const Ring = @import("Ring.zig");
const test_util = @import("test_util.zig");

fn testRoot(tmp: *const std.testing.TmpDir, pb: []u8, name: []const u8, ring: *Ring, n: u64) !*Checkout {
    return Checkout.createFromRing(std.testing.allocator, ring, 0, n, test_util.tmpPath(pb, tmp, name));
}

/// Test writer: records the order of paths it was asked to write, and
/// writes a real file so the rename has something to move.
const Recorder = struct {
    var order: [8]u8 = undefined; // last byte of each path's stem, in write order
    var count: usize = 0;
    var fail_next: bool = false;
    var park: std.atomic.Value(bool) = std.atomic.Value(bool).init(false);

    fn reset() void {
        count = 0;
        fail_next = false;
        park.store(false, .monotonic);
    }
    fn write(path: []const u8, frames: []const f32, rate: u32, channels: u16) anyerror!void {
        while (park.load(.acquire)) std.Thread.yield() catch {};
        if (fail_next) {
            fail_next = false;
            return error.DiskFull;
        }
        // path ends in "<x>.wav.part": record <x>
        order[count] = path[path.len - 10];
        count += 1;
        try wav.writeFile(path, frames, rate, channels, .float32);
    }
};

test "a queued write lands as <path>: .part gone, written state, bytes intact" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    const in = [_]f32{ 1, 2, 3, 4 };
    ring.write(&in);
    var s = Scratch.init(1 << 30);
    var pb: [64]u8 = undefined;
    const co = try testRoot(&tmp, &pb, "a.wav", &ring, 4);
    defer co.destroy();
    try s.start();
    s.submit(co, .write);
    s.waitJob(co);
    s.stop();
    try std.testing.expectEqual(Checkout.WriteState.written, co.write_state.load(.acquire));
    try std.testing.expectError(error.FileNotFound, tmp.dir.statFile(std.testing.io, "a.wav.part", .{}));
    var o = try wav.open(co.path());
    defer o.file.close(wav.io);
    var out: [4]f32 = undefined;
    try wav.readFrames(o.file, o.info, 0, &out);
    try std.testing.expectEqualSlices(f32, &in, &out);
}

test "jobs run FIFO and stop drains the queue" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2, 3 });
    Recorder.reset();
    var s = Scratch.init(1 << 30);
    s.write_fn = &Recorder.write;
    var p1: [64]u8 = undefined;
    var p2: [64]u8 = undefined;
    var p3: [64]u8 = undefined;
    const a = try testRoot(&tmp, &p1, "1.wav", &ring, 3);
    defer a.destroy();
    const b = try testRoot(&tmp, &p2, "2.wav", &ring, 3);
    defer b.destroy();
    const c = try testRoot(&tmp, &p3, "3.wav", &ring, 3);
    defer c.destroy();
    Recorder.park.store(true, .release); // hold the worker so all three queue up
    try s.start();
    s.submit(a, .write);
    s.submit(b, .write);
    s.submit(c, .write);
    Recorder.park.store(false, .release);
    s.stop(); // must not return before all three are written
    try std.testing.expectEqual(@as(usize, 3), Recorder.count);
    try std.testing.expectEqualSlices(u8, "123", Recorder.order[0..3]);
    try std.testing.expectEqual(Checkout.WriteState.written, c.write_state.load(.acquire));
}

test "a failing writer marks the checkout failed and keeps its frames" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2 });
    Recorder.reset();
    Recorder.fail_next = true;
    var s = Scratch.init(1 << 30);
    s.write_fn = &Recorder.write;
    var pb: [64]u8 = undefined;
    const co = try testRoot(&tmp, &pb, "f.wav", &ring, 2);
    defer co.destroy();
    try s.start();
    s.submit(co, .write);
    s.waitJob(co);
    s.stop();
    try std.testing.expectEqual(Checkout.WriteState.failed, co.write_state.load(.acquire));
    try std.testing.expect(co.frames != null);
}

test "an unwritable path (missing directory) marks failed without a panic" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{1});
    var s = Scratch.init(1 << 30);
    const co = try Checkout.createFromRing(std.testing.allocator, &ring, 0, 1, ".zig-cache/tmp/no-such-dir/x.wav");
    defer co.destroy();
    try s.start();
    s.submit(co, .write);
    s.waitJob(co);
    s.stop();
    try std.testing.expectEqual(Checkout.WriteState.failed, co.write_state.load(.acquire));
}

test "a load job reads an evicted checkout back and waitLoad returns once it is resident" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 5, 6, 7 };
    const path = test_util.tmpPath(&pb, &tmp, "ld.wav");
    try wav.writeFile(path, &in, 8_000, 1, .float32);
    const co = try Checkout.adopt(std.testing.allocator, path, 0, 3, 8_000, 1);
    defer co.destroy();
    var s = Scratch.init(1 << 30);
    try s.start();
    s.submit(co, .load);
    s.waitLoad(co);
    s.stop();
    try std.testing.expectEqualSlices(f32, &in, co.frames.?);
    try std.testing.expectEqual(@as(u64, 12), s.resident_bytes);
}

test "submit ignores a checkout that is already queued; start twice is AlreadyRunning" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{1});
    var s = Scratch.init(1 << 30);
    var pb: [64]u8 = undefined;
    const co = try testRoot(&tmp, &pb, "q.wav", &ring, 1);
    defer co.destroy();
    s.submit(co, .write);
    s.submit(co, .load); // ignored: job is already .write
    try std.testing.expectEqual(Checkout.Job.write, co.job);
    try std.testing.expectEqual(@as(?*Checkout, null), co.queue_next);
    try std.testing.expectEqual(@as(u64, 4), s.resident_bytes);
    try s.start();
    try std.testing.expectError(error.AlreadyRunning, s.start());
    s.stop();
}
