//! Single-producer, many-reader lock-free ring buffer.
//!
//! One writer (the audio callback) appends interleaved f32 frames and
//! publishes progress with a single release-store of `total_written`.
//! Readers copy a span, then re-check `total_written`: if the writer
//! wrapped the whole ring through their span mid-copy, the copy may be
//! torn and they retry. `total_written` is the ONLY source of truth —
//! the write position is derived (`total_written % capacity`), which is
//! what makes flush a single atomic store (Task 4).
const std = @import("std");

const Ring = @This();

allocator: std.mem.Allocator,
frames: []f32, // capacity * channels, interleaved, one allocation, forever
capacity: u64, // in frames
channels: u16,
sample_rate: u32,
total_written: std.atomic.Value(u64),
gain: std.atomic.Value(f32),

pub const Config = struct {
    sample_rate: u32,
    channels: u16,
    seconds: f64,
    summary_slot_frames: u32 = 4096,
};

pub const ReadError = error{ Overwritten, OutOfRange };

pub fn init(allocator: std.mem.Allocator, config: Config) !Ring {
    // The allocator is a PARAMETER, not a global: the caller decides the
    // allocation strategy (testing allocator in tests, one shared
    // allocator in the ABI shim). This is the core Zig memory idiom.
    // @intFromFloat TRUNCATES — deliberately, because Python's
    // `int(duration_seconds * sample_rate)` truncates the same f64
    // product, and buffer_size must agree across implementations.
    const capacity: u64 = @intFromFloat(config.seconds * @as(f64, @floatFromInt(config.sample_rate)));
    const frames = try allocator.alloc(f32, capacity * config.channels);
    errdefer allocator.free(frames); // runs only if a later `try` fails
    @memset(frames, 0);
    return .{
        .allocator = allocator,
        .frames = frames,
        .capacity = capacity,
        .channels = config.channels,
        .sample_rate = config.sample_rate,
        .total_written = std.atomic.Value(u64).init(0),
        .gain = std.atomic.Value(f32).init(1.0),
    };
}

pub fn deinit(self: *Ring) void {
    self.allocator.free(self.frames);
    self.* = undefined; // poison: use-after-deinit becomes loud in Debug
}

/// Discard all buffered audio. Because `total_written` is the single
/// source of truth and readers never address at-or-beyond it, resetting
/// it to zero makes every stale byte unreachable — no zeroing REQUIRED
/// for correctness. We zero anyway (hygiene: `.buffer` is exposed as a
/// zero-copy view to the Python host). Called from a control thread,
/// never the audio thread. Racing an active writer costs at most one
/// audio block rendered as silence — silence is a valid sample, never
/// torn garbage. Documented and accepted in the spec.
pub fn flush(self: *Ring) void {
    self.total_written.store(0, .release);
    @memset(self.frames, 0);
}

/// RT-SAFE: no locks, no allocation, no failure path. Called from the
/// audio callback thread. `interleaved.len` must be a multiple of channels.
pub fn write(self: *Ring, interleaved: []const f32) void {
    std.debug.assert(interleaved.len % self.channels == 0);
    const n: u64 = interleaved.len / self.channels;
    if (n == 0) return;
    const g = self.gain.load(.monotonic);
    // Single writer: a monotonic load of our own counter is enough.
    const tw = self.total_written.load(.monotonic);
    const total_floats: usize = @intCast(self.capacity * self.channels);
    var pos: usize = @intCast((tw % self.capacity) * self.channels);
    if (g == 1.0) {
        // Fast path: at most two straight memcpy spans across the wrap.
        var remaining = interleaved;
        while (remaining.len > 0) {
            const span = @min(remaining.len, total_floats - pos);
            @memcpy(self.frames[pos .. pos + span], remaining[0..span]);
            remaining = remaining[span..];
            pos = (pos + span) % total_floats;
        }
    } else {
        for (interleaved) |s| {
            self.frames[pos] = s * g;
            pos += 1;
            if (pos == total_floats) pos = 0;
        }
    }
    // The release-store PUBLISHES: everything written above becomes
    // visible to any reader that acquire-loads a value >= tw + n.
    // This one line is the whole synchronization protocol.
    self.total_written.store(tw + n, .release);
}

/// Seqlock read: copy, then re-check. `out.len` must be a multiple of
/// channels; the span is [abs_start, abs_start + out.len/channels).
pub fn read(self: *Ring, abs_start: u64, out: []f32) ReadError!void {
    std.debug.assert(out.len % self.channels == 0);
    const n: u64 = out.len / self.channels;
    if (n == 0) return;
    var attempt: u8 = 0;
    while (attempt < 3) : (attempt += 1) {
        const t1 = self.total_written.load(.acquire);
        if (abs_start + n > t1) return error.OutOfRange; // span not written yet
        if (t1 - abs_start > self.capacity) return error.Overwritten; // already lapped
        const total_floats: usize = @intCast(self.capacity * self.channels);
        const start_f: usize = @intCast((abs_start % self.capacity) * self.channels);
        if (start_f + out.len <= total_floats) {
            @memcpy(out, self.frames[start_f .. start_f + out.len]);
        } else {
            const first = total_floats - start_f;
            @memcpy(out[0..first], self.frames[start_f..]);
            @memcpy(out[first..], self.frames[0 .. out.len - first]);
        }
        // Seqlock verify: if the writer wrapped the whole ring through our
        // span while we copied, the copy may mix generations — retry.
        // (Formal-memory-model footnote: a canonical seqlock wants a
        // fence before this load; Zig removed @fence, so we lean on the
        // acquire load + the stress test below. If the pinned std has a
        // fence/compiler-barrier API, use it here.)
        const t2 = self.total_written.load(.acquire);
        if (t2 - abs_start <= self.capacity) return;
    }
    return error.Overwritten;
}

test "init allocates capacity*channels frames, starts empty" {
    var ring = try Ring.init(std.testing.allocator, .{
        .sample_rate = 48_000,
        .channels = 2,
        .seconds = 1.0,
    });
    defer ring.deinit();
    try std.testing.expectEqual(@as(u64, 48_000), ring.capacity);
    try std.testing.expectEqual(@as(usize, 96_000), ring.frames.len);
    try std.testing.expectEqual(@as(u64, 0), ring.total_written.load(.acquire));
    try std.testing.expectEqual(@as(f32, 1.0), ring.gain.load(.acquire));
}

test "write then read returns the same frames" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 2, .seconds = 2.0 }); // 16-frame ring
    defer ring.deinit();
    const in = [_]f32{ 0.1, -0.1, 0.2, -0.2, 0.3, -0.3 }; // 3 stereo frames
    ring.write(&in);
    try std.testing.expectEqual(@as(u64, 3), ring.total_written.load(.acquire));
    var out: [6]f32 = undefined;
    try ring.read(0, &out);
    try std.testing.expectEqualSlices(f32, &in, &out);
}

test "write wraps around the end of the ring" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 4, .channels = 1, .seconds = 1.0 }); // 4-frame ring
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2, 3 });
    ring.write(&[_]f32{ 4, 5, 6 }); // frames 3..6, wraps: positions 3,0,1
    var out: [4]f32 = undefined;
    try ring.read(2, &out); // abs 2..6 = values 3,4,5,6
    try std.testing.expectEqualSlices(f32, &[_]f32{ 3, 4, 5, 6 }, &out);
}

test "read past total_written is OutOfRange" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2 }); // total_written = 2
    // Exactly one frame past what's written (abs_start + n == t1 + 1) —
    // the tight off-by-one boundary, not just "way past".
    var out: [3]f32 = undefined;
    try std.testing.expectError(error.OutOfRange, ring.read(0, &out));
}

test "read of lapped span is Overwritten" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 4, .channels = 1, .seconds = 1.0 }); // 4-frame ring
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2, 3, 4, 5, 6 }); // total 6; abs 0/1 overwritten
    var out: [2]f32 = undefined;
    try std.testing.expectError(error.Overwritten, ring.read(0, &out));
    try ring.read(2, &out); // oldest valid
    try std.testing.expectEqualSlices(f32, &[_]f32{ 3, 4 }, &out);
}

test "gain scales frames at write time" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.gain.store(2.0, .monotonic);
    ring.write(&[_]f32{ 0.25, -0.25 });
    var out: [2]f32 = undefined;
    try ring.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0.5, -0.5 }, &out);
}

test "seqlock stress: concurrent writer never yields torn reads" {
    // Tuning history (both extremes taught something real):
    //
    // 1. The original attempt (1024-frame cap, 64-frame reads, ~768-frame
    //    margin) left so much slack that 50,000 reads across 5 runs never
    //    caught a tear even with the seqlock's t2 re-check deliberately
    //    deleted (mutation 3) — the read's tiny @memcpy (a few hundred
    //    bytes) completed far faster than the writer could advance 768
    //    frames, so the race window never opened. Too weak to detect
    //    anything.
    //
    // 2. Overcorrecting (margin_frames=64, block_frames=1024 — a margin
    //    THINNER than one writer block) made mutation 3 reliably red, but
    //    also intermittently reddened the CORRECT, unmutated code. That
    //    is a real property of this design, not a bug: `write()` publishes
    //    once per call, atomically, for the WHOLE block — while a call is
    //    still copying, the entire block can already be physically in
    //    memory before `total_written` reflects any of it. A margin
    //    thinner than the writer's block size lets a single in-flight
    //    write straddle the boundary invisibly. (Confirmed by instrumenting
    //    read()'s internal t1/t2: every false failure showed t1 == t2 —
    //    zero *observed* writer progress — ruling out a missing-fence/
    //    reordering explanation; full `.seq_cst` on every atomic op in
    //    both write() and read() did not change the failure rate either.)
    //    This never bites the real system: the audio callback writes tiny
    //    blocks (hundreds of frames) into a capacity sized in seconds
    //    (tens of thousands of frames) — margin is never thin relative to
    //    a block. The stress test keeps margin_frames a comfortable
    //    multiple of block_frames so it stays a `write`-inside-one-call
    //    non-issue, while still being thin relative to capacity/read size
    //    (and reads large enough for the @memcpy to take real wall-clock
    //    time) so mutation 3's actual target — a torn read straddling
    //    MULTIPLE writer calls — is still caught reliably.
    const cap_frames = 4096;
    const chans = 2;
    const block_frames = 128; // realistic: one audio-callback-sized block
    const margin_frames = 512; // 4x block_frames — clear of the single-call effect above
    const read_frames = cap_frames - margin_frames;
    var ring = try Ring.init(std.testing.allocator, .{
        .sample_rate = 48_000,
        .channels = chans,
        .seconds = @as(f64, cap_frames) / 48_000.0,
    });
    defer ring.deinit();

    const H = struct {
        // f32 holds integers exactly up to 2^24 — keep values inside that.
        fn expected(abs_frame: u64, ch: u64) f32 {
            return @floatFromInt((abs_frame * chans + ch) % (1 << 24));
        }
        fn writerLoop(r: *Ring, stop: *std.atomic.Value(bool)) void {
            var abs: u64 = 0;
            var block: [block_frames * chans]f32 = undefined;
            while (!stop.load(.monotonic)) {
                for (0..block_frames) |i| {
                    for (0..chans) |c| {
                        block[i * chans + c] = expected(abs + i, c);
                    }
                }
                r.write(&block);
                abs += block_frames;
            }
        }
    };

    var stop = std.atomic.Value(bool).init(false);
    const writer = try std.Thread.spawn(.{}, H.writerLoop, .{ &ring, &stop });
    defer writer.join();
    defer stop.store(true, .monotonic);

    var successes: u64 = 0;
    var out: [read_frames * chans]f32 = undefined;
    while (successes < 2_000) {
        const tw = ring.total_written.load(.acquire);
        if (tw < read_frames) continue;
        const abs_start = tw - read_frames; // thin margin from the Overwritten edge
        ring.read(abs_start, &out) catch continue; // Overwritten is FINE; torn is not
        for (0..read_frames) |i| {
            for (0..chans) |c| {
                const want = H.expected(abs_start + i, c);
                if (out[i * chans + c] != want) {
                    std.debug.print("TORN at abs {d} ch {d}: got {d}, want {d}\n", .{ abs_start + i, c, out[i * chans + c], want });
                    return error.TornRead;
                }
            }
        }
        successes += 1;
    }
}

test "flush empties the ring; writer restarts cleanly at abs 0" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2, 3, 4, 5 });
    ring.flush();
    try std.testing.expectEqual(@as(u64, 0), ring.total_written.load(.acquire));
    var out: [1]f32 = undefined;
    try std.testing.expectError(error.OutOfRange, ring.read(0, &out)); // nothing readable
    ring.write(&[_]f32{ 9, 8 });
    try ring.read(0, &out);
    try std.testing.expectEqual(@as(f32, 9), out[0]);
}
