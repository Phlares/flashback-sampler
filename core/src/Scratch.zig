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
//! `self.thread` is written only by `start`/`stop` and read (without
//! the mutex) by `start`'s own `AlreadyRunning` check and by
//! `waitLoad`/`waitJob`'s no-worker early-out. All of `start`, `stop`,
//! `submit`, `waitLoad`, `waitJob` assume ONE control thread — this
//! module has no lock protecting `thread` itself, so concurrent calls
//! into these from more than one control thread are unsupported.
//! `waitLoad`/`waitJob` returning early because `thread == null` means
//! quiescence was NOT reached — `co` may still be linked into the FIFO
//! (and the LRU, for a `.write`) — so a caller must unlink it (`forget`,
//! Task h4/h5) before freeing it in that case. h4 reads this block: its
//! eviction routine relies on `Checkout.lru_bytes` (set under `mutex` at
//! LRU-insert, snapshotting `residentBytes()` at that moment) as the
//! exact figure to subtract from `resident_bytes` on removal — never a
//! fresh `residentBytes()` recomputed at removal time, which can differ
//! from what was added if `frames` changed size while linked.
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
    // Same guard doLoad uses: a re-write of a checkout that is already
    // resident (still linked from an earlier write, never evicted) must
    // not double-count its bytes or, worse, re-link an already-linked
    // node onto itself.
    if (job == .write and !self.lruLinkedLocked(co)) self.lruInsertHeadLocked(co);
    self.cond.broadcast(io);
}

/// Block while a `.load` job for `co` is queued or running. Never waits
/// on a `.write` (the write only reads `frames`; a bind right after a
/// checkout must not wait for a gigabyte to hit the disk). Ruling
/// R-h4b: a `.load` submitted with no worker running (never started, or
/// already stopped) would otherwise wait forever with nothing left to
/// process it and broadcast — `self.thread` is written only by `start`/
/// `stop` on this same control thread, so reading it here needs no
/// separate lock. With no worker running this returns WITHOUT
/// quiescence: `co` may still be linked into the FIFO. See `waitJob`'s
/// doc for the same caveat on destroying a checkout in that state.
pub fn waitLoad(self: *Scratch, co: *Checkout) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    while (co.job == .load and self.thread != null) self.cond.waitUncancelable(io, &self.mutex);
}

/// Block until `co` has no job at all. Tests and `forget` use it. Same
/// no-worker early-out as `waitLoad` (ruling R-h4b): with no worker
/// running, this returns WITHOUT quiescence — `co` may still be linked
/// into the FIFO and/or the LRU (nothing ran to unlink it). The caller
/// must unlink the checkout (`forget`, Task h4/h5) before destroying it
/// in that case; destroying a still-linked checkout leaves Scratch's
/// list pointers dangling.
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
    // The rename below still walks `wav.io` (the same synchronous Io
    // singleton `defaultWrite` locks `wav.write_mutex` around) via
    // `Dir.rename`'s vtable call — wav.zig's own doc says concurrent use
    // of that singleton is untraced, so this needs the same lock, taken
    // here rather than folded into `defaultWrite` (a swappable write_fn,
    // e.g. the test Recorder, never takes it): every write_fn, default
    // or injected, gets the rename protected regardless.
    {
        wav.write_mutex.lockUncancelable(wav.io);
        defer wav.write_mutex.unlock(wav.io);
        std.Io.Dir.cwd().rename(part, std.Io.Dir.cwd(), co.path(), io) catch {
            co.write_state.store(.failed, .release);
            return;
        };
    }
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
    // Snapshot NOW: `co.residentBytes()` reads `frames.len`, which can
    // change (a later evict, or a load onto a still-linked checkout —
    // see doLoad's own linked-guard) while `co` stays in this list.
    // `lruRemoveLocked` must undo exactly this many bytes, not whatever
    // `frames` holds when it runs.
    co.lru_bytes = co.residentBytes();
    self.resident_bytes += co.lru_bytes;
}

fn lruRemoveLocked(self: *Scratch, co: *Checkout) void {
    if (!self.lruLinkedLocked(co)) return;
    if (co.lru_prev) |p| p.lru_next = co.lru_next else self.lru_head = co.lru_next;
    if (co.lru_next) |n| n.lru_prev = co.lru_prev else self.lru_tail = co.lru_prev;
    co.lru_prev = null;
    co.lru_next = null;
    // Subtract the snapshot taken at insert, not a fresh
    // `co.residentBytes()` (see the insert-side comment). `@min` is the
    // subtraction-guard idiom applied to running-total bookkeeping: it
    // cannot underflow `resident_bytes` even if `lru_bytes` were ever
    // larger than the tracked total (defensive; every insert should pair
    // with exactly one remove of its own snapshot).
    self.resident_bytes -= @min(self.resident_bytes, co.lru_bytes);
    co.lru_bytes = 0;
}

/// Pin = "the UI is looking at this one". Pinned entries are never
/// evicted, and pinning an evicted checkout queues its preload so PLAY
/// finds it resident. Unpinning re-checks the budget at once.
pub fn pin(self: *Scratch, co: *Checkout, on: bool) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    co.pinned = on;
    if (on) {
        if (co.frames == null) self.submitLocked(co, .load);
    } else {
        self.evictOverBudgetLocked();
    }
}

/// Record a use: move to the LRU head, then trim to budget.
pub fn touch(self: *Scratch, co: *Checkout) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    if (co.frames != null) {
        self.lruRemoveLocked(co);
        self.lruInsertHeadLocked(co);
    }
    self.evictOverBudgetLocked();
}

pub fn setBudget(self: *Scratch, bytes: u64) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    self.budget_bytes = bytes;
    self.evictOverBudgetLocked();
}

/// Take `co` out of the cache before the caller destroys it.
///
/// With a worker running, waits for any job on `co` first (a write in
/// flight must finish before its frames are freed) — quiescence is
/// reached and the worker itself is the one that would have linked/
/// unlinked `co` meanwhile.
///
/// With no worker running (ruling R-h4b/R-h3f), waiting would hang
/// forever: nothing will ever pop `co`'s queued job or broadcast. So
/// instead this dequeues `co` from the FIFO by hand, under the mutex,
/// before it can be destroyed out from under a job that will now never
/// run — the deadlock the pre-flight caught.
pub fn forget(self: *Scratch, co: *Checkout) void {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    if (self.thread != null) {
        while (co.job != .none) self.cond.waitUncancelable(io, &self.mutex);
    } else {
        self.dequeueLocked(co);
    }
    self.lruRemoveLocked(co);
}

pub fn residentBytes(self: *Scratch) u64 {
    self.mutex.lockUncancelable(io);
    defer self.mutex.unlock(io);
    return self.resident_bytes;
}

/// Unlink `co` from the FIFO if it is still queued, and reset its job to
/// `.none`. Only safe to call when no worker is running: with a worker,
/// `co` might already be popped (mid-job, `queue_next == null` but
/// `job != .none`) and unlinking it here would race the worker's own
/// mutation of `job`. `forget`'s no-worker branch is the only caller.
/// The FIFO is singly linked, so removing a non-head node means walking
/// from `queue_head` to find its predecessor.
fn dequeueLocked(self: *Scratch, co: *Checkout) void {
    if (co.job == .none) return;
    if (self.queue_head == co) {
        self.queue_head = co.queue_next;
        if (self.queue_head == null) self.queue_tail = null;
    } else {
        var prev = self.queue_head;
        while (prev) |p| : (prev = p.queue_next) {
            if (p.queue_next == co) {
                p.queue_next = co.queue_next;
                if (self.queue_tail == co) self.queue_tail = p;
                break;
            }
        }
    }
    co.queue_next = null;
    co.job = .none;
}

/// Walk from the LRU tail while over budget. Skips pinned entries, held
/// entries (`hold > 0`: an ABI call is reading `frames` right now
/// outside the mutex — R-h1d, same eviction-blocking tier as `pinned`),
/// entries with a job in flight, and entries whose audio is not yet safe
/// on disk (`queued`/`writing`/`failed`) — evicting those would lose the
/// only copy.
///
/// A `frames == null` entry can reach this walk: a `.write` submitted on
/// a checkout with no frames links into the LRU at submit time (before
/// `doWrite` ever runs, per `submitLocked`), and `doWrite`'s own
/// no-frames failure path leaves it linked forever with a 0-byte
/// snapshot — a "ghost" node inherited from h3. Its `write_state` never
/// becomes `.written`/`.adopted`, so the check below already keeps it
/// out of the removal branch; `Checkout.evict()` is also a no-op on null
/// frames regardless, so no separate `frames == null` guard is needed
/// here.
fn evictOverBudgetLocked(self: *Scratch) void {
    var cur = self.lru_tail;
    while (self.resident_bytes > self.budget_bytes) {
        const co = cur orelse return;
        cur = co.lru_prev;
        if (co.pinned or co.hold > 0 or co.job != .none) continue;
        const ws = co.write_state.load(.acquire);
        if (ws != .written and ws != .adopted) continue;
        self.lruRemoveLocked(co);
        co.evict();
    }
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

/// Runs `Scratch.waitLoad` on a spare thread so a test can bound-poll
/// whether it returned, instead of blocking the test thread itself (a
/// mutated predicate must redden with a timeout, never an unattended
/// hang).
const WaitLoadCtx = struct {
    scratch: *Scratch,
    co: *Checkout,
    done: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),

    fn run(self: *WaitLoadCtx) void {
        self.scratch.waitLoad(self.co);
        self.done.store(true, .release);
    }
};

/// Poll `flag`, yielding the thread between checks, for a bounded number
/// of iterations. Returns whether it became true in time — no `Io` is
/// threaded through this test file, so this spins on `Thread.yield`
/// rather than a timed sleep; the iteration cap still guarantees this
/// never blocks unboundedly, so a caller can safely still stop/join
/// after a "timeout" instead of wedging the test binary.
fn pollTrue(flag: *std.atomic.Value(bool)) bool {
    var i: u32 = 0;
    while (!flag.load(.acquire) and i < 2_000_000) : (i += 1) {
        std.Thread.yield() catch {};
    }
    return flag.load(.acquire);
}

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
    // tmpDir creates its own directory; the "no-such-dir" component
    // under it is never created, so this stays a real, guaranteed-
    // missing directory rather than a bare cwd-relative path (ruling
    // R-2: every test path routes through tmpDir + test_util.tmpPath).
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{1});
    var s = Scratch.init(1 << 30);
    var pb: [64]u8 = undefined;
    const co = try Checkout.createFromRing(std.testing.allocator, &ring, 0, 1, test_util.tmpPath(&pb, &tmp, "no-such-dir/x.wav"));
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

test "waitLoad returns immediately on a parked .write; it only blocks on .load" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{1});
    Recorder.reset();
    Recorder.park.store(true, .release); // the write never finishes until unparked below
    var s = Scratch.init(1 << 30);
    s.write_fn = &Recorder.write;
    var pb: [64]u8 = undefined;
    const co = try testRoot(&tmp, &pb, "w.wav", &ring, 1);
    defer co.destroy();
    try s.start();
    s.submit(co, .write);

    var ctx = WaitLoadCtx{ .scratch = &s, .co = co };
    const t = try std.Thread.spawn(.{}, WaitLoadCtx.run, .{&ctx});
    defer {
        Recorder.park.store(false, .release); // let the parked write finish so `t` can join
        t.join();
        s.stop();
    }

    try std.testing.expect(pollTrue(&ctx.done)); // must not need the parked write to finish
    try std.testing.expectEqual(Checkout.Job.write, co.job); // still running: proof of the claim
}

/// Runs `Scratch.forget` on a spare thread, same shape as `WaitLoadCtx`
/// above, so a test can bound-poll whether `forget` has returned instead
/// of blocking the test thread itself on it.
const ForgetCtx = struct {
    scratch: *Scratch,
    co: *Checkout,
    done: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),

    fn run(self: *ForgetCtx) void {
        self.scratch.forget(self.co);
        self.done.store(true, .release);
    }
};

test "forget's worker-running branch waits for an in-flight write before unlinking" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{1});
    Recorder.reset();
    Recorder.park.store(true, .release); // the write never finishes until unparked below
    var s = Scratch.init(1 << 30);
    s.write_fn = &Recorder.write;
    var pb: [64]u8 = undefined;
    const co = try testRoot(&tmp, &pb, "fg.wav", &ring, 1);
    defer co.destroy();
    try s.start();
    s.submit(co, .write); // co.job == .write; the worker pops it and parks inside write_fn

    var ctx = ForgetCtx{ .scratch = &s, .co = co };
    const t = try std.Thread.spawn(.{}, ForgetCtx.run, .{&ctx});
    defer {
        Recorder.park.store(false, .release); // let the parked write finish so `t` can join
        t.join();
        s.stop();
    }

    // Bounded busy-wait for "still not done": a deterministic block (the
    // parked write can only finish once we release it below), not a
    // timing race, so this budget just needs to be generous enough for
    // the forget thread to have had every chance to return wrongly.
    var i: u32 = 0;
    while (!ctx.done.load(.acquire) and i < 200_000) : (i += 1) {
        std.Thread.yield() catch {};
    }
    try std.testing.expect(!ctx.done.load(.acquire)); // forget must still be blocked on the write

    Recorder.park.store(false, .release); // let the parked write finish
    try std.testing.expect(pollTrue(&ctx.done)); // forget now returns
}

test "re-submitting the same checkout for another write does not double-count LRU bytes" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2 });
    var s = Scratch.init(1 << 30);
    var pb: [64]u8 = undefined;
    const co = try testRoot(&tmp, &pb, "r.wav", &ring, 2);
    defer co.destroy();
    try s.start();
    s.submit(co, .write);
    s.waitJob(co);
    const bytes_after_first = s.resident_bytes;
    try std.testing.expectEqual(co.residentBytes(), bytes_after_first);

    // The guard under test (submitLocked's LRU insert) runs
    // synchronously inside `submit`, under `mutex`, before the worker
    // ever touches this job — so it's already fully checkable right
    // here, without waiting for this second write to finish. No second
    // `waitJob`: this test must stay decoupled from whether the FIFO
    // itself ever redelivers the job (a different guard, pinned by its
    // own test) — call `stop` unconditionally after for cleanup, bounded
    // regardless of that.
    s.submit(co, .write); // re-write: co is already linked at the LRU head
    defer s.stop();

    try std.testing.expectEqual(bytes_after_first, s.resident_bytes); // unchanged, not doubled
    try std.testing.expectEqual(@as(?*Checkout, null), co.lru_next); // still the sole entry: no self-loop
    try std.testing.expectEqual(@as(?*Checkout, null), co.lru_prev);
    try std.testing.expectEqual(@as(?*Checkout, co), s.lru_head);
    try std.testing.expectEqual(@as(?*Checkout, co), s.lru_tail);
}

test "lruRemoveLocked subtracts the snapshot taken at insert, not a stale recompute" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2, 3, 4 });
    const co = try Checkout.createFromRing(std.testing.allocator, &ring, 0, 4, "snap.wav");
    defer co.destroy();
    var s = Scratch.init(1 << 30);
    s.mutex.lockUncancelable(io);
    s.lruInsertHeadLocked(co); // co.residentBytes() == 16 right now
    s.mutex.unlock(io);
    try std.testing.expectEqual(@as(u64, 16), s.resident_bytes);
    try std.testing.expectEqual(@as(u64, 16), co.lru_bytes);

    // Simulates the shape of h4's future eviction changing `frames`
    // while `co` stays linked (h4 itself isn't built yet): evict
    // directly, bypassing Scratch's own removal.
    co.evict();
    try std.testing.expectEqual(@as(u64, 0), co.residentBytes());

    s.mutex.lockUncancelable(io);
    s.lruRemoveLocked(co);
    s.mutex.unlock(io);
    // Correct: subtracted the 16-byte snapshot taken at insert, not a
    // fresh residentBytes() (which reads 0 now that frames is null —
    // that bug would leave resident_bytes wrongly stuck at 16).
    try std.testing.expectEqual(@as(u64, 0), s.resident_bytes);
}

test "lruRemoveLocked's resident_bytes subtraction cannot underflow even if the snapshot exceeds the running total" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{1});
    const co = try Checkout.createFromRing(std.testing.allocator, &ring, 0, 1, "clamp.wav");
    defer co.destroy();
    var s = Scratch.init(1 << 30);
    s.mutex.lockUncancelable(io);
    s.lruInsertHeadLocked(co); // resident_bytes = co.lru_bytes = 4
    s.resident_bytes = 1; // contrived: a running total smaller than this entry's own snapshot
    s.lruRemoveLocked(co);
    s.mutex.unlock(io);
    try std.testing.expectEqual(@as(u64, 0), s.resident_bytes); // clamped, not wrapped to a huge u64
}

test "submit ignores job == .none: it never reaches the FIFO" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{1});
    var s = Scratch.init(1 << 30);
    const co = try Checkout.createFromRing(std.testing.allocator, &ring, 0, 1, "none.wav");
    defer co.destroy();
    s.submit(co, .none);
    try std.testing.expectEqual(Checkout.Job.none, co.job);
    try std.testing.expectEqual(@as(?*Checkout, null), s.queue_head);
}

test "doWrite on a checkout with no resident frames fails without calling write_fn" {
    Recorder.reset();
    var s = Scratch.init(1 << 30);
    s.write_fn = &Recorder.write;
    const co = try Checkout.adopt(std.testing.allocator, "adopted-empty.wav", 0, 1, 8_000, 1);
    defer co.destroy();
    try s.start();
    s.submit(co, .write);
    s.waitJob(co);
    s.stop();
    try std.testing.expectEqual(Checkout.WriteState.failed, co.write_state.load(.acquire));
    try std.testing.expectEqual(@as(usize, 0), Recorder.count); // write_fn never called
}

test "a load job for a nonexistent file leaves the checkout unresident and unlinked" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const co = try Checkout.adopt(std.testing.allocator, test_util.tmpPath(&pb, &tmp, "nope.wav"), 0, 1, 8_000, 1);
    defer co.destroy();
    var s = Scratch.init(1 << 30);
    try s.start();
    s.submit(co, .load);
    s.waitLoad(co);
    s.stop();
    try std.testing.expectEqual(@as(?[]f32, null), co.frames);
    try std.testing.expectEqual(@as(u64, 0), s.resident_bytes);
    try std.testing.expectEqual(@as(?*Checkout, null), s.lru_head);
}

test "a submit after the queue fully drains is not lost to a stale tail pointer" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2 });
    var s = Scratch.init(1 << 30);
    var p1: [64]u8 = undefined;
    var p2: [64]u8 = undefined;
    const a = try testRoot(&tmp, &p1, "d1.wav", &ring, 2);
    defer a.destroy();
    const b = try testRoot(&tmp, &p2, "d2.wav", &ring, 2);
    defer b.destroy();
    try s.start();
    s.submit(a, .write);
    s.waitJob(a); // drains the queue fully: queue_head/queue_tail both go back to null
    s.submit(b, .write); // fresh submit after the drain
    // `stop` is bounded even under the bug this pins: the worker still
    // sees `stop_flag` and exits even if `b` never got linked to
    // `queue_head` from the (buggy) stale tail.
    s.stop();
    try std.testing.expectEqual(Checkout.WriteState.written, b.write_state.load(.acquire));
}

test "stop before start is a safe no-op" {
    var s = Scratch.init(1 << 30);
    s.stop();
    try std.testing.expect(s.thread == null);
}

// ---- Task h4: LRU under a byte budget, pin, touch, forget ----

/// Two written roots in a tmp dir, both resident, no thread running.
const Pair = struct {
    tmp: std.testing.TmpDir,
    ring: Ring,
    a: *Checkout,
    b: *Checkout,
    pa: [64]u8 = undefined,
    pb: [64]u8 = undefined,

    fn init(self: *Pair) !void {
        self.tmp = std.testing.tmpDir(.{});
        self.ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
        var in: [10]f32 = undefined;
        for (&in, 0..) |*s, i| s.* = @floatFromInt(i);
        self.ring.write(&in);
        self.a = try Checkout.createFromRing(std.testing.allocator, &self.ring, 0, 4, tmpPath(&self.pa, &self.tmp, "a.wav"));
        self.b = try Checkout.createFromRing(std.testing.allocator, &self.ring, 4, 10, tmpPath(&self.pb, &self.tmp, "b.wav"));
    }
    fn writeBoth(self: *Pair, s: *Scratch) !void {
        try s.start();
        s.submit(self.a, .write);
        s.submit(self.b, .write);
        s.stop();
    }
    fn deinit(self: *Pair) void {
        self.a.destroy();
        self.b.destroy();
        self.ring.deinit();
        self.tmp.cleanup();
    }
};
const tmpPath = test_util.tmpPath;

test "budget 0 drops every written root after its write; bytes read 0" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(0);
    try p.writeBoth(&s);
    try std.testing.expectEqual(@as(?[]f32, null), p.a.frames);
    try std.testing.expectEqual(@as(?[]f32, null), p.b.frames);
    try std.testing.expectEqual(@as(u64, 0), s.residentBytes());
}

test "eviction is LRU: touch moves to the head; the tail goes first" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(1 << 30);
    try p.writeBoth(&s); // both resident: a (16 B) then b (24 B) at the head
    s.touch(p.a); // a is now most recent
    s.setBudget(20); // 40 > 20: evict the tail = b
    try std.testing.expect(p.a.frames != null);
    try std.testing.expectEqual(@as(?[]f32, null), p.b.frames);
    try std.testing.expectEqual(@as(u64, 16), s.residentBytes());
}

test "a pinned checkout survives budget 0; unpin evicts it" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(1 << 30);
    try p.writeBoth(&s);
    s.pin(p.a, true);
    s.setBudget(0);
    try std.testing.expect(p.a.frames != null);
    try std.testing.expectEqual(@as(?[]f32, null), p.b.frames);
    s.pin(p.a, false);
    try std.testing.expectEqual(@as(?[]f32, null), p.a.frames);
    try std.testing.expectEqual(@as(u64, 0), s.residentBytes());
}

test "a checkout that is not yet written is never evicted" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(0);
    // no thread: a and b stay .queued
    s.submit(p.a, .write);
    s.setBudget(0);
    try std.testing.expect(p.a.frames != null);
    try std.testing.expectEqual(@as(u64, 16), s.residentBytes());
}

test "pin on an evicted checkout preloads it (budget 0 keeps it while pinned)" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(0);
    try p.writeBoth(&s); // both evicted
    try s.start();
    s.pin(p.b, true);
    s.waitLoad(p.b);
    s.stop();
    try std.testing.expectEqualSlices(f32, &[_]f32{ 4, 5, 6, 7, 8, 9 }, p.b.frames.?);
    try std.testing.expectEqual(@as(u64, 24), s.residentBytes());
}

test "forget unlinks and stops counting; destroy afterwards is clean" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(1 << 30);
    try p.writeBoth(&s);
    s.forget(p.a);
    try std.testing.expectEqual(@as(u64, 24), s.residentBytes());
    try std.testing.expectEqual(p.b, s.lru_head.?);
    try std.testing.expectEqual(p.b, s.lru_tail.?);
    s.forget(p.b);
    try std.testing.expectEqual(@as(u64, 0), s.residentBytes());
    try std.testing.expectEqual(@as(?*Checkout, null), s.lru_head);
}

test "a held checkout survives budget 0 like a pinned one (R-h1d: hold is eviction-blocking, same tier as pinned)" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(1 << 30);
    try p.writeBoth(&s);
    p.a.hold = 1; // no worker is running at this point (writeBoth already stopped it)
    s.setBudget(0);
    try std.testing.expect(p.a.frames != null); // held: survives
    try std.testing.expectEqual(@as(?[]f32, null), p.b.frames); // not held: evicted
    p.a.hold = 0;
    s.setBudget(0); // re-check at once, same as unpin does
    try std.testing.expectEqual(@as(?[]f32, null), p.a.frames);
    try std.testing.expectEqual(@as(u64, 0), s.residentBytes());
}

test "R-h4a: a failed write (job none, write_state failed) stays resident and unevictable" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    Recorder.reset();
    Recorder.fail_next = true;
    var s = Scratch.init(1 << 30);
    s.write_fn = &Recorder.write;
    try s.start();
    s.submit(p.a, .write);
    s.waitJob(p.a); // job reaches .none; write_state becomes .failed
    s.stop();
    try std.testing.expectEqual(Checkout.Job.none, p.a.job);
    try std.testing.expectEqual(Checkout.WriteState.failed, p.a.write_state.load(.acquire));
    s.setBudget(0); // over budget: the eviction walk reaches p.a with job == .none
    try std.testing.expect(p.a.frames != null); // a failed write is not lost to eviction
}

test "R-h4a companion: an adopted (write_state == .adopted) resident clip IS evicted at budget 0" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 5, 6, 7 };
    const path = test_util.tmpPath(&pb, &tmp, "adopt-ev.wav");
    try wav.writeFile(path, &in, 8_000, 1, .float32);
    const co = try Checkout.adopt(std.testing.allocator, path, 0, 3, 8_000, 1);
    defer co.destroy();
    var s = Scratch.init(1 << 30); // high budget first: the load succeeds and links in
    try s.start();
    s.submit(co, .load);
    s.waitLoad(co);
    s.stop();
    try std.testing.expectEqual(Checkout.WriteState.adopted, co.write_state.load(.acquire));
    try std.testing.expect(co.frames != null);
    s.setBudget(0); // adopted clips must be evictable, not permanently pinned like .failed
    try std.testing.expectEqual(@as(?[]f32, null), co.frames);
    try std.testing.expectEqual(@as(u64, 0), s.residentBytes());
    try std.testing.expectEqual(@as(?*Checkout, null), s.lru_head);
}

test "forget on an unstarted scratch dequeues the FIFO entry instead of hanging (R-h4b/R-h3f)" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(1 << 30);
    s.submit(p.a, .write); // no start(): thread stays null, nothing will ever pop these jobs
    s.submit(p.b, .write); // queue is now a -> b, tail == b

    // Forget the TAIL first: dequeueLocked's non-head branch must walk
    // from queue_head to find b's predecessor (a) and fix up queue_tail
    // (which pointed at b) to point at a instead.
    s.forget(p.b);
    try std.testing.expectEqual(Checkout.Job.none, p.b.job);
    try std.testing.expectEqual(@as(?*Checkout, p.a), s.queue_head);
    try std.testing.expectEqual(@as(?*Checkout, p.a), s.queue_tail); // the tail fix-up ran

    s.forget(p.a); // must also return without a worker to run its job
    try std.testing.expectEqual(Checkout.Job.none, p.a.job);
    try std.testing.expectEqual(@as(?*Checkout, null), s.queue_head);
    try std.testing.expectEqual(@as(?*Checkout, null), s.queue_tail);
    try std.testing.expectEqual(@as(u64, 0), s.residentBytes());
    try std.testing.expectEqual(@as(?*Checkout, null), s.lru_head);
}

// ---- Task h5: the concurrency proof ----

test "the writer thread and a control-thread wav.writeFile serialise through wav.write_mutex" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    // R-h5a: the brief's n = 40_000 makes `in` a 320 KiB stack object;
    // 8_000 stereo frames (64 KiB) keeps the same intent (a real clip
    // streamed through more than a trivial single write) at a safe
    // stack size.
    const n: usize = 8_000;
    var in: [n * 2]f32 = undefined;
    for (&in, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i % 1000)) / 1000.0;
    ring.write(&in);
    var s = Scratch.init(1 << 30);
    var pa: [64]u8 = undefined;
    var pb: [64]u8 = undefined;
    const co = try Checkout.createFromRing(std.testing.allocator, &ring, 0, n, tmpPath(&pa, &tmp, "w.wav"));
    defer co.destroy();
    try s.start();
    // `defer s.stop()` right after `start`, not a trailing explicit call:
    // if anything below this point returns an error (e.g. the writeFile
    // below), defers unwind top-down from here, and `co.destroy()` (a
    // few lines up) would free `co.frames` while the worker is still
    // live inside `doWrite` — a use-after-free in the very test proving
    // threading safety. `stop` is safe to call more than once, so this
    // costs nothing on the normal path either.
    defer s.stop();
    const other = tmpPath(&pb, &tmp, "c.wav");
    // Force the interleaving deterministically, not by scheduling luck:
    // take `write_mutex` BEFORE submitting the worker's job, so the
    // worker's `defaultWrite` (its `write_fn`) is guaranteed to block
    // trying to acquire this same mutex the instant it reaches its own
    // `wav.writeFile` call inside `doWrite` — the worker holds no
    // `Scratch.mutex` while blocked there, so this is not a lock
    // inversion. That means this thread's own `wav.writeFile` below is
    // guaranteed to land first, every run, while the worker's write is
    // still pending on the held lock: the exact contended interleaving
    // this test exists to prove serialises rather than deadlocking or
    // corrupting either file. Without forcing the order this way, the
    // worker could finish its entire write before this thread ever took
    // the mutex, making the run sequential and the test unable to tell.
    {
        wav.write_mutex.lockUncancelable(wav.io);
        defer wav.write_mutex.unlock(wav.io);
        s.submit(co, .write); // the worker now races toward the same mutex, held by this thread
        try wav.writeFile(other, in[0 .. n * 2], 48_000, 2, .float32);
    }
    s.waitJob(co);
    try std.testing.expectEqual(Checkout.WriteState.written, co.write_state.load(.acquire));
    inline for (.{ "w.wav", "c.wav" }) |name| {
        var pp: [64]u8 = undefined;
        var o = try wav.open(tmpPath(&pp, &tmp, name));
        defer o.file.close(wav.io);
        try std.testing.expectEqual(@as(u64, n), o.info.frames);
        var tail: [4]f32 = undefined;
        try wav.readFrames(o.file, o.info, n - 2, &tail);
        try std.testing.expectEqualSlices(f32, in[n * 2 - 4 ..], &tail);
    }
}

// h4 carry: a re-submitted write is not evicted mid-window. The eviction
// guard's `co.job != .none` clause (evictOverBudgetLocked) is the ONLY
// thing protecting a checkout in one real window — a re-submitted
// `.write` on a still-resident, already-`.written` checkout, between
// `run` popping the job (co.job stays `.write`, unchanged from submit)
// and `doWrite`'s first store of `.writing`. That window is a handful of
// instructions in real execution; it can't be hit by scheduling luck in
// a portable test. But its state is fully reproducible by hand: finish a
// real write (worker started, then stopped), then submit a second
// `.write` with NO worker running to ever pick it up. `co.job` becomes
// `.write` and `write_state` stays `.written` — exactly the window's
// shape — and stays that way indefinitely, long enough to probe the
// guard directly.
test "h4 carry: a re-submitted write is not evicted mid-window (job != .none is the only guard there)" {
    var p: Pair = undefined;
    try p.init();
    defer p.deinit();
    var s = Scratch.init(1 << 30);
    try p.writeBoth(&s); // both written, resident, linked; worker stopped after
    try std.testing.expectEqual(Checkout.WriteState.written, p.a.write_state.load(.acquire));
    try std.testing.expectEqual(Checkout.Job.none, p.a.job);

    s.submit(p.a, .write); // no worker running: job becomes .write and stays there
    try std.testing.expectEqual(Checkout.Job.write, p.a.job);
    try std.testing.expectEqual(Checkout.WriteState.written, p.a.write_state.load(.acquire)); // ws did NOT move to .writing

    s.setBudget(0); // over budget: only `co.job != .none` protects p.a here (write_state == .written passes the ws check too)
    try std.testing.expect(p.a.frames != null); // must survive: a write is still (re-)pending on it
    // Positive control: p.b (no job pending) WAS evicted at the same
    // budget-0 call. Without this, a broken guard that skips eviction
    // entirely would still pass the line above — this pins that the walk
    // actually ran and actually spared p.a specifically, not that it did
    // nothing.
    try std.testing.expect(p.b.frames == null);

    s.forget(p.a); // stranded job on an unstarted scratch: dequeue by hand before destroy
    s.forget(p.b); // p.b was already evicted by setBudget(0); a harmless no-op
}
