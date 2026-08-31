//! Single-producer, many-reader lock-free ring buffer.
//!
//! One writer (the audio callback) appends interleaved f32 frames and
//! publishes progress with a single release-store of `total_written`.
//! Readers copy a span, then re-check `total_written`: if the writer
//! wrapped the whole ring through their span mid-copy, the copy may be
//! torn and they retry. `total_written` is the ONLY source of truth.
//!
//! `capacity` (the readable window a caller can request — what Python's
//! `buffer_size` reports, what `get_latest` clamps against) and
//! `storage_frames` (the PHYSICAL allocation backing `frames`, and what
//! the write position is actually derived from,
//! `total_written % storage_frames`) are deliberately different sizes.
//! See the guard-band note above `read` for why.
const std = @import("std");
const Summary = @import("Summary.zig");

const Ring = @This();

allocator: std.mem.Allocator,
frames: []f32, // storage_frames * channels, interleaved, one allocation, forever
capacity: u64, // in frames — the READABLE window; unchanged by the guard band below
// GUARD BAND (see the note above `read`): the physical allocation,
// always `capacity + max_write_frames`. Every wrap/modulo computation in
// `write` and `read` is keyed off THIS, not `capacity` — the surplus is
// exactly one maximum write block, which is what makes a reader's
// accepted span provably disjoint from whatever the writer might
// currently be mid-copy into.
storage_frames: u64,
channels: u16,
sample_rate: u32,
total_written: std.atomic.Value(u64),
gain: std.atomic.Value(f32),
writer_active: std.atomic.Value(bool), // owned by the control thread that starts/stops the writer thread; tells flush() whether to defer
flush_pending: std.atomic.Value(bool), // control thread asked for a flush while a writer was active; write() drains it
summary: Summary, // pre-decimated stats ring; fed per-chunk by write(), poisoned by flush()

pub const Config = struct {
    sample_rate: u32,
    channels: u16,
    seconds: f64,
    // Must equal Python's AudioCircularBuffer._SUMMARY_SLOT_SAMPLES
    // (flashback_sampler/core/buffer.py) for get_summary_bins/rmsBins
    // parity -- per-bin RMS of a constant-amplitude signal is the SAME
    // number regardless of slot size, so the existing constant-amplitude
    // parity test cannot detect the two constants drifting apart. A
    // change to either number is a parity change; change both together.
    summary_slot_frames: u32 = 4096,
};

pub const ReadError = error{ Overwritten, OutOfRange };

// The guard band's unit: the largest number of frames `write()` will
// ever publish under ONE release-store, regardless of how large a
// caller's own call is — see the chunking loop in `write` and the
// guard-band note above `read`. A file-level constant, not a per-call
// or per-`Config` choice: it's the ring's own publish granularity, not
// something a caller varies. 4096 frames is generous headroom over a
// real audio callback (typically 64-2048 frames).
pub const max_write_frames: u64 = 4096;

pub fn init(allocator: std.mem.Allocator, config: Config) !Ring {
    // The ABI used to be the only guard (issue #21). Hosts that construct
    // a Ring directly (Capture, a future CLAP host) now get the same
    // protection, and fb_ring_create's guard becomes a pass-through.
    if (config.sample_rate == 0 or config.channels == 0 or config.channels > 2 or
        !std.math.isFinite(config.seconds) or config.seconds <= 0) return error.InvalidArgument;
    // The allocator is a PARAMETER, not a global: the caller decides the
    // allocation strategy (testing allocator in tests, one shared
    // allocator in the ABI shim). This is the core Zig memory idiom.
    // @intFromFloat TRUNCATES — deliberately, because Python's
    // `int(duration_seconds * sample_rate)` truncates the same f64
    // product, and buffer_size must agree across implementations.
    const capacity: u64 = @intFromFloat(config.seconds * @as(f64, @floatFromInt(config.sample_rate)));
    // Over-allocate rather than shrink the readable window: `capacity`
    // stays exactly what a caller asked for (so `get_latest(seconds)`
    // requesting the full window still succeeds, and native/Python
    // parity on `capacity` holds), and the extra `max_write_frames`
    // slots absorb the guard band instead.
    const storage_frames: u64 = capacity + max_write_frames;
    const frames = try allocator.alloc(f32, storage_frames * config.channels);
    errdefer allocator.free(frames); // runs only if a later `try` fails
    @memset(frames, 0);
    // Summary.init is constructed with `capacity` (the READABLE window),
    // never `storage_frames` — rmsBins clamps n_avail against
    // capacity_frames to mirror Python's clamp to buffer_size exactly;
    // using storage_frames here would silently diverge from the Python
    // reference whenever capacity isn't slot-aligned.
    var summary = try Summary.init(allocator, capacity, config.summary_slot_frames, config.channels);
    // No fallible operation follows before the return below, so this
    // errdefer never actually fires today — kept as the same
    // multi-errdefer pattern as `frames` above (each fallible allocation
    // paired with its own unwind) so the struct stays safe to extend
    // with another `try` later without re-deriving the pattern.
    errdefer summary.deinit();
    return .{
        .allocator = allocator,
        .frames = frames,
        .capacity = capacity,
        .storage_frames = storage_frames,
        .channels = config.channels,
        .sample_rate = config.sample_rate,
        .total_written = std.atomic.Value(u64).init(0),
        .gain = std.atomic.Value(f32).init(1.0),
        .writer_active = std.atomic.Value(bool).init(false),
        .flush_pending = std.atomic.Value(bool).init(false),
        .summary = summary,
    };
}

pub fn deinit(self: *Ring) void {
    self.allocator.free(self.frames);
    self.summary.deinit();
    self.* = undefined; // poison: use-after-deinit becomes loud in Debug
}

/// Discard all buffered audio. If a writer is registered
/// (`writer_active`), the flush is handed to the writer thread, which
/// performs it before its next write (`drainPendingFlush`) — so a
/// writer that already loaded `total_written` can never republish over
/// the reset (issue #20). With no writer, it happens here, immediately.
/// Called from a control thread, never the audio thread.
///
/// OWNERSHIP: `writer_active` belongs to the CONTROL thread that owns
/// the writer thread — `Capture.start`/`stop`, and `Mixer.start`/`stop`
/// for the mixed-slot target. Stored true BEFORE the spawn, false AFTER
/// the join; the writer thread never touches it. Storing before the
/// spawn closes the start window: a flush that lands while the worker
/// is still opening its stream is deferred to the loop top, not
/// executed under a writer about to appear. Every writer of a ring
/// registers this way; a host that wrote through `fb_ring_write` without
/// it would race both this reset and `Summary.gen` (issue #23) — no such
/// host remains once the mixer runs in Zig.
pub fn flush(self: *Ring) void {
    if (self.writer_active.load(.acquire)) {
        self.flush_pending.store(true, .release);
        return;
    }
    self.flushNow();
}

/// The actual reset, shared by both call sites: here directly (no writer
/// active) and from `write()` (draining a deferred `flush_pending`, on
/// the writer thread itself). Because `total_written` is the single
/// source of truth and readers never address at-or-beyond it, resetting
/// it to zero makes every stale byte unreachable — no zeroing REQUIRED
/// for correctness. We zero anyway (hygiene: `.buffer` is exposed as a
/// zero-copy view to the Python host).
fn flushNow(self: *Ring) void {
    // Poison BEFORE the total_written store, same ordering rationale as
    // the frames-then-store below: a racing writer that wins a slot's
    // tag write between this poison and the store leaves one slot
    // transiently mixing pre- and post-flush data (~85 ms at typical
    // slot sizes) — it self-heals on the writer's next pass through that
    // slot's new generation. When `flushNow` runs FROM `write()` there
    // is no other writer to race in the first place (the caller IS the
    // writer) — this ordering is kept anyway so both call sites share
    // one implementation instead of diverging into two.
    self.summary.poison();
    self.total_written.store(0, .release);
    @memset(self.frames, 0);
}

/// Drains a flush deferred by an active writer (see `flush`'s doc
/// comment) — the memset is a bounded, allocation-free, lock-free
/// operation (~50 ms for a 345 MB ring; WASAPI's buffer is 200 ms, so a
/// flush mid-capture costs no frames). This is the only way a flush can
/// never be undone by the writer that raced it (issue #20).
///
/// Four call sites, because `write()` alone is not enough: a silent
/// loopback/process source can go arbitrarily long between packets (or
/// forever), and a flush must never wait on the audio source to be busy.
///  - `write()` below, before publishing its own data — a writer that IS
///    producing frames.
///  - `Capture.runStream`'s loop top, once per iteration BEFORE calling
///    `stream.next`, INCLUDING the no-packet path (`next` returning null).
///  - `Capture.idleUntilStopped`, for a worker that lost its stream (or
///    finished) but still holds the writer registration until stop().
///  - `Mixer.run`'s loop top, the same rule for the mixed target.
/// All four run on the writer thread: no lock, no allocation, no error
/// path. `Capture.stop` and `Mixer.stop` also drain, AFTER the join and
/// after clearing `writer_active` — those run on the control thread,
/// which is by then the only thread touching the ring.
pub fn drainPendingFlush(self: *Ring) void {
    if (self.flush_pending.load(.acquire)) {
        self.flushNow();
        self.flush_pending.store(false, .release);
    }
}

/// RT-SAFE: no locks, no allocation, no failure path. Called from the
/// audio callback thread. `interleaved.len` must be a multiple of
/// channels. Callers may pass ANY size — no assert, no error path — the
/// call is CHUNKED internally at `max_write_frames` per publish. This is
/// what keeps the guard band's proof (see the note above `read`) valid
/// no matter how large a single caller-side call is: every individual
/// release-store still only ever advances `total_written` by at most
/// `max_write_frames`.
pub fn write(self: *Ring, interleaved: []const f32) void {
    std.debug.assert(interleaved.len % self.channels == 0);
    // See `drainPendingFlush`'s doc comment: this call site covers a
    // writer that IS producing frames. A `Capture` writer also drains
    // from its own loop top, so an idle source still gets flushed
    // (`write` here never runs while the source is silent).
    self.drainPendingFlush();
    const g = self.gain.load(.monotonic);
    // Physical wrap is keyed off storage_frames (capacity + the guard
    // band), not capacity — see the struct-level comment and the note
    // above `read`.
    const total_floats: usize = @intCast(self.storage_frames * self.channels);

    var remaining = interleaved;
    while (remaining.len > 0) {
        const chunk_frames: u64 = @min(@as(u64, remaining.len / self.channels), max_write_frames);
        // In an asserts-off build (ReleaseFast/ReleaseSmall — shipped
        // mode is ReleaseSafe, so this is latent, not live), a leftover
        // partial frame (interleaved.len not a multiple of channels)
        // would otherwise leave `chunk_frames == 0`, `remaining` never
        // shrinking, and this loop republishing `tw + 0` forever — a
        // hang on the RT audio thread, worse than the silently-dropped
        // partial frame this guards against instead.
        if (chunk_frames == 0) return;
        const chunk_floats = chunk_frames * self.channels;
        const chunk = remaining[0..chunk_floats];
        remaining = remaining[chunk_floats..];

        // Single writer: a monotonic load of our own counter is enough.
        const tw = self.total_written.load(.monotonic);
        var pos: usize = @intCast((tw % self.storage_frames) * self.channels);
        if (g == 1.0) {
            // Fast path: at most two straight memcpy spans across the wrap.
            var chunk_remaining = chunk;
            while (chunk_remaining.len > 0) {
                const span = @min(chunk_remaining.len, total_floats - pos);
                @memcpy(self.frames[pos .. pos + span], chunk_remaining[0..span]);
                chunk_remaining = chunk_remaining[span..];
                pos = (pos + span) % total_floats;
            }
        } else {
            for (chunk) |s| {
                self.frames[pos] = s * g;
                pos += 1;
                if (pos == total_floats) pos = 0;
            }
        }
        // Summary.update takes PRE-gain data (`chunk`, never mutated by
        // either write path above) and re-applies gain itself — see the
        // doc comment on Summary.update. Critically this is called with
        // `tw`, THIS chunk's own start_abs, not the original call's tw:
        // write() chunks internally at max_write_frames, so a caller
        // passing a multi-second block would otherwise tag every chunk
        // past the first with the wrong slot generation.
        self.summary.update(chunk, g, tw);
        // The release-store PUBLISHES this chunk: everything written
        // above becomes visible to any reader that acquire-loads a
        // value >= tw + chunk_frames. Publishing once per CHUNK, not
        // once per call, is what bounds every publish to
        // max_write_frames regardless of the caller's own block size.
        self.total_written.store(tw + chunk_frames, .release);
    }
}

/// Seqlock read: copy, then re-check. `out.len` must be a multiple of
/// channels; the span is [abs_start, abs_start + out.len/channels).
///
/// GUARD BAND: `write()` publishes once per CHUNK of at most
/// `max_write_frames` frames (see `write`'s own doc comment) — while a
/// chunk is still copying, that whole chunk can already be physically
/// sitting in `frames` before `total_written` reflects any of it. A
/// read whose validity check uses
/// the full physical size as its threshold can be fooled: it can land
/// exactly inside an in-flight, unpublished block and observe an
/// identical `t1`/`t2` (zero *apparent* writer progress) while the bytes
/// underneath it have already been overwritten with the next
/// generation's data — a torn read that publishes as success.
///
/// The fix is to allocate MORE physical storage than the readable
/// window, not to shrink what a reader can request: `frames` holds
/// `storage_frames = capacity + max_write_frames` frames, but the
/// validity checks below stay written against `capacity`, unchanged.
/// With P = storage_frames, C = capacity, M = max_write_frames (P = C +
/// M): a writer publishing at `t` and about to write `b <= M` frames
/// will, once that block completes, occupy the physical slots that used
/// to hold frames `[t-P, t+b-P)` (the previous generation at those same
/// positions, P frames back). A read accepted here satisfies
/// `abs_start >= t - C = t - P + M >= t + b - P` for any `b <= M`, so
/// its span can never overlap the region a single in-flight write could
/// still be touching — the reader's window and the writer's in-flight
/// block are provably disjoint. `capacity` itself — what `get_latest`
/// clamps against, what Python's `buffer_size` reports — is untouched,
/// so a caller asking for the full window still gets the full window.
/// This is a correctness gap, not a probability one — see the stress
/// test below, which is now unable to produce a false positive on the
/// correct implementation because this guard makes the window
/// structurally unreachable rather than merely making it rare.
pub fn read(self: *Ring, abs_start: u64, out: []f32) ReadError!void {
    std.debug.assert(out.len % self.channels == 0);
    const n: u64 = out.len / self.channels;
    if (n == 0) return;
    var attempt: u8 = 0;
    while (attempt < 3) : (attempt += 1) {
        const t1 = self.total_written.load(.acquire);
        if (abs_start + n > t1) return error.OutOfRange; // span not written yet
        // Safe: t1 >= abs_start + n > abs_start (n >= 1 here), so this
        // subtraction cannot underflow.
        if (t1 - abs_start > self.capacity) return error.Overwritten; // already lapped
        // Physical indexing uses storage_frames (see the guard-band note
        // above) — capacity is the READABLE window, not the physical size.
        const total_floats: usize = @intCast(self.storage_frames * self.channels);
        const start_f: usize = @intCast((abs_start % self.storage_frames) * self.channels);
        if (start_f + out.len <= total_floats) {
            @memcpy(out, self.frames[start_f .. start_f + out.len]);
        } else {
            const first = total_floats - start_f;
            @memcpy(out[0..first], self.frames[start_f..]);
            @memcpy(out[first..], self.frames[0 .. out.len - first]);
        }
        // Seqlock verify: if the writer wrapped the whole ring through our
        // span while we copied, the copy may mix generations — retry.
        //
        // `t2 >= abs_start + n` is checked FIRST and is NOT redundant
        // with the `t1` check above: `t2` can move BACKWARDS relative to
        // `t1` if `flush()` (Ring.zig, above) races in between — it
        // resets `total_written` to 0. Without this guard,
        // `t2 - abs_start` underflows (`t2` unsigned, `t2 < abs_start`),
        // which is illegal behavior in Zig and traps in Debug AND
        // ReleaseSafe (the shipped mode) — an abort of the whole host
        // process, not a catchable error. With the guard, a racing flush
        // just falls through to another attempt; a fresh `t1` next
        // attempt will be small and correctly return `error.OutOfRange`.
        //
        // (Formal-memory-model footnote, resolved: Zig 0.16 exposes no
        // user-callable fence — `@fence` is an invalid builtin, and
        // `std.atomic` has no free-standing barrier function; `.acquire`
        // on `load` is the strongest ordering primitive available for
        // this. An acquire load only orders operations AFTER it in
        // program order — it does not order the copy ABOVE it relative
        // to itself. On x86-64 (this workstation, and CI's x86 runners)
        // TSO makes that a non-issue for same-thread loads in practice
        // — confirmed empirically: promoting every atomic op in both
        // `write` and `read` to `.seq_cst` did not change this test's
        // failure rate on a genuine tear, ruling out a reordering
        // explanation for what WAS actually the guard-band gap above.
        // On a weakly-ordered target such as CI's aarch64 macOS runner,
        // no such hardware guarantee exists, and the no-tear property
        // for that platform rests on the stress test below, not on the
        // memory model.)
        const t2 = self.total_written.load(.acquire);
        if (t2 >= abs_start + n and t2 - abs_start <= self.capacity) return;
    }
    return error.Overwritten;
}

/// One (min, max) pair per channel per display bin. `extern` fixes the
/// layout to two consecutive f32 — the ctypes host maps a numpy
/// float32[n_bins][channels][2] view straight onto `[*]PeakBin`.
pub const PeakBin = extern struct { min: f32, max: f32 };

pub const PeakBinsError = error{ InvalidArgument, Overwritten };

/// Cap on samples inspected per bin: above it the bin is stride-sampled so
/// the per-tick cost stays bounded regardless of ring size (256 × 360 bins
/// × stereo ≈ 46k reads, sub-millisecond).
pub const peak_bins_max_samples_per_bin: u64 = 256;
/// Slack left below capacity on a saturated ring so the writer can advance
/// during the in-place scan without tripping the verify (4096 frames ≈
/// 85 ms at 48 kHz, larger than a WASAPI period). Applied only when
/// capacity > 2 × headroom so tiny test rings still expose every frame.
pub const peak_bins_read_headroom: u64 = 4096;

/// Downsample the newest `n_frames_req` frames into `n_bins` (min, max)
/// pairs per channel for the waveform display. `out.len == n_bins *
/// channels`, laid out `out[bin * channels + ch]`.
///
/// Port of Python's `_peak_bins_impl`: same window clamp and headroom,
/// same bin edges (numpy `linspace` — `i * step` in f64, truncated), same
/// stride grid anchored to ABSOLUTE frame indices (so rolling the window
/// by a few frames does not re-pick a bin's samples), same physical
/// modulus (`storage_frames`, never `capacity`). Two ported quirks stay:
/// the stride branch reads `k` positions per bin regardless of the bin's
/// end (it may overshoot into the next bin, or past `total_written` on
/// the last bin), and NaN samples are ignored by `@min`/`@max` where
/// numpy would propagate them.
///
/// Reads `frames` IN PLACE through the seqlock — no copy of a multi-
/// hundred-MB ring at 30 Hz — then verifies `total_written` exactly like
/// `read`, but STRICTER: two clauses, not one. The first guards the
/// unsigned subtraction against a racing flush (ReleaseSafe traps on
/// underflow); the second is the lap check. A flush during the scan
/// retries here where numpy's single-clause check would have accepted
/// the read — both end in zeros or a retry, never torn data. On three
/// torn attempts `out` is all zeros and the error says so; an empty
/// window is all zeros and success.
pub fn peakBins(self: *Ring, n_frames_req: u64, n_bins: usize, out: []PeakBin) PeakBinsError!void {
    if (n_bins == 0) return error.InvalidArgument;
    std.debug.assert(out.len == n_bins * self.channels);
    const chans: usize = self.channels;
    const modulus = self.storage_frames;
    var attempt: u8 = 0;
    while (attempt < 3) : (attempt += 1) {
        @memset(out, .{ .min = 0, .max = 0 });
        const tw = self.total_written.load(.acquire);
        var n_avail = @min(tw, self.capacity);
        if (n_avail >= self.capacity and self.capacity > 2 * peak_bins_read_headroom)
            n_avail = self.capacity - peak_bins_read_headroom;
        const n = @min(n_frames_req, n_avail);
        if (n == 0) return;
        const abs_start = tw - n;
        // numpy: step = n / n_bins in f64, edge_i = trunc(i * step). The
        // multiply must be `float(i) * step`, not `i * n / n_bins` — a
        // different rounding order moves edges by one frame and shifts
        // every waveform golden (spec "Risks", peak-bin parity).
        const step: f64 = @as(f64, @floatFromInt(n)) / @as(f64, @floatFromInt(n_bins));
        const span_ref = binEdge(step, 1, n, n_bins) - binEdge(step, 0, n, n_bins);
        const stride: u64 = @max(1, span_ref / peak_bins_max_samples_per_bin);

        if (stride == 1) {
            for (0..n_bins) |i| {
                const a = binEdge(step, i, n, n_bins);
                const b = binEdge(step, i + 1, n, n_bins);
                if (b <= a) {
                    if (i > 0) @memcpy(out[i * chans .. (i + 1) * chans], out[(i - 1) * chans .. i * chans]);
                    continue;
                }
                var idx: u64 = (abs_start + a) % modulus;
                var first = true;
                var f: u64 = a;
                while (f < b) : (f += 1) {
                    reduceFrame(self, idx, out[i * chans .. (i + 1) * chans], &first);
                    idx += 1;
                    if (idx == modulus) idx = 0;
                }
            }
        } else {
            // k covers span_ref at any grid alignment; tail bins may pull
            // 1–2 positions from the next bin (ported behaviour).
            const k: usize = @intCast(span_ref / stride + 1);
            for (0..n_bins) |i| {
                const bin_abs = abs_start + binEdge(step, i, n, n_bins);
                const first_abs = ((bin_abs + stride - 1) / stride) * stride; // ceil to the grid
                var first = true;
                for (0..k) |j| {
                    const idx = (first_abs + @as(u64, j) * stride) % modulus;
                    reduceFrame(self, idx, out[i * chans .. (i + 1) * chans], &first);
                }
            }
        }
        // Seqlock verify, same two clauses as `read`: the first guards the
        // unsigned subtraction against a racing flush (ReleaseSafe traps
        // on underflow); the second is the lap check.
        const t2 = self.total_written.load(.acquire);
        if (t2 >= abs_start and t2 - abs_start <= self.capacity) return;
    }
    @memset(out, .{ .min = 0, .max = 0 });
    return error.Overwritten;
}

fn binEdge(step: f64, i: usize, n: u64, n_bins: usize) u64 {
    if (i == n_bins) return n; // numpy sets the last edge to `stop` exactly
    // @intFromFloat truncates toward zero == numpy's int64 cast here (non-negative).
    return @intFromFloat(@as(f64, @floatFromInt(i)) * step);
}

/// Fold one physical frame into a bin's per-channel (min, max).
fn reduceFrame(self: *const Ring, frame_idx: u64, bin: []PeakBin, first: *bool) void {
    const base: usize = @intCast(frame_idx * self.channels);
    for (bin, 0..) |*pb, c| {
        const v = self.frames[base + c];
        if (first.*) {
            pb.* = .{ .min = v, .max = v };
        } else {
            pb.min = @min(pb.min, v);
            pb.max = @max(pb.max, v);
        }
    }
    first.* = false;
}

test "init rejects sample_rate == 0" {
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 0, .channels = 2, .seconds = 1.0 }));
}
test "init rejects channels == 0" {
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 0, .seconds = 1.0 }));
}
test "init rejects channels == 3" {
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 3, .seconds = 1.0 }));
}
test "init rejects seconds <= 0, NaN, and +inf" {
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = 0.0 }));
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = std.math.nan(f64) }));
    try std.testing.expectError(error.InvalidArgument, Ring.init(std.testing.allocator, .{ .sample_rate = 48_000, .channels = 2, .seconds = std.math.inf(f64) }));
}

test "init: capacity is the readable window, storage_frames is capacity + max_write_frames" {
    var ring = try Ring.init(std.testing.allocator, .{
        .sample_rate = 48_000,
        .channels = 2,
        .seconds = 1.0,
    });
    defer ring.deinit();
    try std.testing.expectEqual(@as(u64, 48_000), ring.capacity); // unchanged by the guard band
    try std.testing.expectEqual(@as(u64, 48_000) + Ring.max_write_frames, ring.storage_frames);
    try std.testing.expectEqual(@as(u64, 48_000 + 4096), ring.storage_frames); // default max_write_frames
    try std.testing.expectEqual(@as(usize, (48_000 + 4096) * 2), ring.frames.len);
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

test "write wraps around the physical end of the storage buffer" {
    // Physical wrap now happens at storage_frames = capacity +
    // max_write_frames (the guard band), not at capacity — see the
    // struct-level comment and the note above `read`. max_write_frames
    // is a fixed 4096-frame constant (not per-Config anymore), so
    // exercising an actual physical wrap takes thousands of frames,
    // not the handful the brief's original numbers used. One chunked
    // write() call (chunked internally at max_write_frames, so this
    // also exercises the chunking loop) pushes total_written past
    // storage_frames and back around to the low end of `frames`.
    const sample_rate = 8;
    const capacity = sample_rate; // seconds=1.0, so capacity == sample_rate exactly
    const storage_frames = capacity + Ring.max_write_frames;
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = sample_rate, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();

    // Frame k's value is k+1 (1-based, so it's never confusable with
    // pre-zeroed memory) — lets the wrapped region be checked without a
    // hand-maintained literal array at this scale.
    var buf: [storage_frames + 4]f32 = undefined;
    for (&buf, 0..) |*s, i| s.* = @floatFromInt(i + 1);
    ring.write(&buf);

    const last_abs: u64 = storage_frames + 4; // == total_written after the write above
    var out: [capacity]f32 = undefined; // capacity == 8: the oldest-valid, zero-margin boundary
    try ring.read(last_abs - capacity, &out); // straddles the physical wrap: positions [storage_frames-4, storage_frames) then [0, 4)
    var want: [capacity]f32 = undefined;
    for (&want, 0..) |*s, i| s.* = @floatFromInt(last_abs - capacity + i + 1);
    try std.testing.expectEqualSlices(f32, &want, &out);
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
    ring.write(&[_]f32{ 1, 2, 3, 4, 5, 6 }); // total 6; abs 0/1 past the readable capacity
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

test "write with zero-length input is a no-op" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{});
    try std.testing.expectEqual(@as(u64, 0), ring.total_written.load(.acquire));
}

test "read with zero-length output is a no-op, even at an out-of-range start" {
    // Reading at abs_start=0 with a zero-length `out` would pass trivially
    // regardless of whether `read`'s `if (n == 0) return;` special case
    // exists — the OutOfRange check further down never gets a chance to
    // fire either way. abs_start=100, past what's been written, is what
    // actually PINS the special case: without it, this call would fall
    // through to `abs_start + n > t1` (100 + 0 > 2) and return
    // error.OutOfRange instead of succeeding.
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 2 }); // total_written = 2
    var out: [0]f32 = undefined;
    try ring.read(100, &out);
}

test "gain path wraps around the physical end of the storage buffer" {
    // Forces write()'s SCALAR (gain != 1.0) path specifically through its
    // own wrap-reset (`if (pos == total_floats) pos = 0;`) — the fast
    // (gain == 1.0) path's wrap test above never exercises this branch,
    // and a mutation to `pos = 1` there leaves the rest of the suite
    // green.
    const sample_rate = 8;
    const capacity = sample_rate; // seconds=1.0, so capacity == sample_rate exactly
    const storage_frames = capacity + Ring.max_write_frames;
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = sample_rate, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();

    // Fill to 2 frames short of the physical end via the fast (gain==1.0)
    // path — irrelevant to what's under test, just positions us near
    // the wrap.
    var filler: [storage_frames - 2]f32 = undefined;
    for (&filler, 0..) |*s, i| s.* = @floatFromInt(i + 1);
    ring.write(&filler);

    ring.gain.store(2.0, .monotonic); // forces the scalar path below
    ring.write(&[_]f32{ 10, 20, 30, 40 }); // lands at storage_frames-2, wraps to physical 0,1 mid-call

    var out: [4]f32 = undefined;
    try ring.read(storage_frames - 2, &out); // straddles the physical wrap
    try std.testing.expectEqualSlices(f32, &[_]f32{ 20, 40, 60, 80 }, &out);
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
    //    also intermittently reddened the CORRECT, unmutated code. This
    //    turned out to be a REAL bug, not a test artifact: `write()`
    //    published once per call, atomically, for the WHOLE block (this
    //    was PRE-CHUNKING — see the doc comments on `write`/`read` above
    //    for the current, chunked-at-max_write_frames behavior) — while
    //    a call was still copying, the entire block could already be
    //    physically in memory before `total_written` reflected any of it.
    //    A margin thinner than the writer's block size let a single
    //    in-flight write straddle the read's validity boundary invisibly
    //    (confirmed by instrumenting read()'s internal t1/t2: every false
    //    failure showed t1 == t2 — zero *observed* writer progress —
    //    ruling out a missing-fence/reordering explanation; full
    //    `.seq_cst` on every atomic op in both write() and read() did not
    //    change the failure rate either). `read()` now closes this
    //    structurally by over-allocating `frames` beyond `capacity` (the
    //    guard band — see the note above `read`) rather than by shrinking
    //    what a reader can request or via test-side margin tuning — the
    //    false positive this section describes is no longer POSSIBLE,
    //    not just improbable.
    //
    // 3. `margin_frames` is NOT the lever for mutation-3 detection, and it
    //    was wrong to imply otherwise. With the guard band in place,
    //    mutation 3 (deleting the t2 re-check) can only be caught by a
    //    FULL-LAP overrun during the copy: the reader's oldest slot
    //    `A = tw - read_frames` is only clobbered when the writer reaches
    //    `A + storage_frames = tw + margin_frames + max_write_frames`,
    //    while acceptance caps the writer at `tw + margin_frames` when
    //    `t1` is loaded — so the writer must advance exactly
    //    `max_write_frames` (4096) frames DURING the copy, independent of
    //    `margin_frames`. Margin only helps indirectly, by growing
    //    `read_frames = cap_frames - margin_frames` and thus copy
    //    duration; shrinking it from 512 to 0 buys roughly 14%, not worth
    //    tuning on its own. `cap_frames` was predicted to be the real
    //    lever instead: the writer's requirement stays pinned at
    //    `max_write_frames` regardless of ring size, while the reader's
    //    copy-and-verify work scales linearly with `cap_frames` — a
    //    bigger copy should mean more wall-clock time for the writer to
    //    overlap it.
    //
    // 4. That prediction did NOT hold up empirically. `cap_frames` was
    //    raised to 32768 (8x) and measured (see the numbers below this
    //    comment block): holding the attempt budget roughly constant
    //    (10,000 successes at both sizes) gives statistically the SAME
    //    ~30% mutation-3 detection rate at cap_frames=32768 as it did at
    //    4096 — the bigger copy does not measurably raise per-attempt
    //    detection probability in this implementation. What actually
    //    changes with `cap_frames` is the verify loop's cost, which
    //    forces `target_successes` down to keep CI runtime bounded —
    //    and that reduction in attempts is NOT offset by any per-attempt
    //    gain, so the runtime-bounded configuration below measures
    //    WORSE (5%) than the original cap_frames=4096/10,000-successes
    //    baseline (30%). More importantly, removing the guard band
    //    entirely (mutating `storage_frames = capacity` in `init`) was
    //    caught by NEITHER 2,000 nor 10,000 successes at cap_frames=32768
    //    — the guard band remains pinned only by the init test's
    //    allocation-arithmetic assertion, not by any test that exercises
    //    the race it exists to prevent. `cap_frames` is NOT the fix for
    //    "the guard band has no behavioral test" — see the separate
    //    "guard band: reader targets the lap boundary" test below, which
    //    is aimed at that instead.
    //
    // `cap_frames = 32768` was measured (round 2 of this PR's review) and
    // REJECTED: 5% mutation-3 detection under the runtime-bounded
    // config it required, no better than 4096 when the attempt budget is
    // held constant (30% either way). Reverted to 4096/10,000 successes
    // below — the measurably better config for this metric. Do not
    // re-run this experiment; the numbers are recorded here so the next
    // person doesn't have to pay for it again.
    //
    // This test deliberately does NOT race a flusher (see the separate
    // "flush racing..." test below): a flush racing an in-flight write
    // is a DOCUMENTED, ACCEPTED race (flush()'s doc comment) that can
    // legitimately scramble which total_written generation a block's
    // content belongs to — content that's "wrong" for that reason is
    // not a seqlock tear, and trying to make ONE test verify byte-exact
    // content AND survive a racing flush means every content mismatch
    // becomes ambiguous between "real bug" and "accepted race artifact".
    // Splitting the two properties into two tests keeps each one able to
    // fail unambiguously.
    const cap_frames = 4096;
    const chans = 2;
    const block_frames = 128; // realistic: one audio-callback-sized block
    const margin_frames = 512; // stress parameter, not a safety margin — see points 3-4 above
    const read_frames = cap_frames - margin_frames;
    var ring = try Ring.init(std.testing.allocator, .{
        .sample_rate = 48_000,
        .channels = chans,
        .seconds = @as(f64, cap_frames) / 48_000.0,
    }); // block_frames (128) stays well under Ring.max_write_frames (4096), so write() never chunks here
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

    // A stress test that can spin forever is a defect in its own right,
    // independent of the target count below — cap total attempts at a
    // generous multiple of the target (most attempts legitimately fail
    // OutOfRange/Overwritten while the writer catches up) so a
    // regression that makes reads never converge fails fast instead of
    // hanging CI.
    // Measured (warm cache, this workstation, cap_frames=4096 — the
    // reverted, measurably-better config; see the rejected cap_frames=
    // 32768 numbers in the tuning-history comment above): ~0.585s at
    // 10,000 successes. Mutation 3 (delete the t2 re-check) reddens
    // 6/20 runs (30%) at this count — a real, not-great detection rate;
    // noted honestly rather than picked to look good. The guard band
    // itself is NOT pinned by this test at any geometry tried — see the
    // separate "guard band: reader targets the lap boundary" test below.
    const target_successes: u64 = 10_000;
    const max_attempts: u64 = target_successes * 2000;
    var attempts: u64 = 0;
    var successes: u64 = 0;
    var last_err: ?ReadError = null;
    var out: [read_frames * chans]f32 = undefined;
    while (successes < target_successes and attempts < max_attempts) {
        attempts += 1;
        const tw = ring.total_written.load(.acquire);
        if (tw < read_frames) {
            // Standard hardware hint for a spin-wait loop (Intel's own
            // guidance for exactly this shape). Harmless and cheap, but
            // NOT a fix for the flake once observed at cap_frames=32768
            // (see the task report): that flake was `last_err == null`
            // after exhausting max_attempts, meaning total_written never
            // reached read_frames at all — consistent with the writer
            // thread going unscheduled for on the order of hundreds of
            // milliseconds under real system load on a shared, busy
            // dev workstation (confirmed present at diagnosis time via
            // `Get-Process`; this is a spin-wait *hint*, operating at
            // nanosecond/microsecond granularity — it does not yield to
            // the OS scheduler and cannot plausibly close a gap that
            // size). It is kept here as ordinary hygiene for a spin-wait
            // loop, not represented as the reason the flake hasn't
            // recurred. What actually reduces the flake's blast radius
            // is the much larger `max_attempts` headroom that came back
            // with reverting to cap_frames=4096 (20,000,000 vs. the
            // 4,000,000 that tripped) — more wall-clock time for a
            // transient scheduling delay to resolve before the bounded
            // cap gives up.
            std.atomic.spinLoopHint();
            continue;
        }
        const abs_start = tw - read_frames;
        ring.read(abs_start, &out) catch |err| {
            last_err = err; // OutOfRange/Overwritten are FINE; torn is not
            continue;
        };
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
    if (successes < target_successes) {
        std.debug.print(
            "seqlock stress: only {d}/{d} successes in {d} attempts (cap {d}); last error: {?}\n",
            .{ successes, target_successes, attempts, max_attempts, last_err },
        );
        return error.StressTestDidNotConverge;
    }
}

test "guard band: reader targets the lap boundary" {
    // The mutation-3 test above samples abs_start = tw - cap_frames +
    // margin_frames, which sits margin_frames (512) below the "already
    // lapped" boundary — nowhere near where the guard band actually
    // does its work. That is why removing the guard band entirely
    // (storage_frames = capacity, mutating out the entire fix) reddens
    // NEITHER that test NOR the init test at any cap_frames tried (see
    // the task report) — the physical collision the guard band prevents
    // never falls inside a window that test's reader samples.
    //
    // This test targets the boundary on purpose. abs_start sits exactly
    // `offset` frames inside the "already lapped" edge (n = capacity -
    // offset), so acceptance tolerates the writer having advanced a
    // LITTLE between the outer snapshot and read()'s own t1 load, but no
    // more. Worked through by hand before implementing (P =
    // storage_frames, C = capacity, M = max_write_frames):
    //
    //   WITH the guard band (P = C + M): the oldest byte in the read
    //   window physically collides with a later write once total_written
    //   reaches abs_start + P + 1 = tw + offset + M + 1 — at least M+1
    //   frames of writer progress needed from t1, far more than a
    //   same-copy writer can produce; provably disjoint by construction.
    //
    //   WITHOUT the guard band (P = C): the same collision needs only
    //   tw + offset + 1 — the "logical" validity boundary and the
    //   "physical" corruption boundary are exactly 1 frame apart, pushed
    //   M frames apart by the guard band instead.
    //
    // First implementation used the SAME symbol for both the writer's
    // own chunk size and this offset (as originally proposed) and
    // measured 0/20 detection even with the guard band fully removed —
    // worse than the mutation-3 test's 0/20 at capacity-based sampling,
    // despite being aimed at the right boundary. Diagnosed with
    // temporary t1/t2 instrumentation: coupling offset to the writer's
    // block size means n = capacity - block_frames shrinks the read
    // (hence its copy duration) in exact lockstep as block_frames grows
    // the writer's own per-call duration — the two effects cancel
    // regardless of the shared value chosen, and the maximum writer
    // progress ever observed during a copy was exactly one call, which
    // by the formula's own construction lands EXACTLY on the safe side
    // of the boundary (t2 - abs_start == capacity, which passes) rather
    // than past it. Fixed by DECOUPLING the two: `block_frames` (the
    // writer's own chunk size — large, so a single in-flight call's
    // written-but-unpublished window is wide) from `offset` (the read's
    // tolerance margin — small, so little writer progress is needed to
    // breach it). That combination is what actually reddens this test;
    // see the numbers below.
    //
    // Most attempts return Overwritten by design (the writer racing
    // ahead of a read that intentionally sits at the edge of validity)
    // — expected, not a failure; only a successful read with wrong
    // content is. Kept as its own test (rather than folded into the
    // mutation-3 test above) so that test's own 30% measurement stays
    // undisturbed by a completely different sampling strategy.
    //
    // Measured (this workstation, 20 runs each):
    //   Unmutated (guard band present): 20/20 clean (100%), ~0.11s/run.
    //   Guard band removed (storage_frames = capacity): 12/20 runs (60%)
    //     produced a genuine TornRead (e.g. "got 2000000, want 1991808",
    //     a diff of exactly 2*capacity in floats — one full lap ahead,
    //     the guard-band-removal signature), 0 convergence-cap trips.
    // This is the first test in this file that actually pins the guard
    // band behaviorally, not just via the init test's allocation
    // arithmetic.
    const cap_frames = 4096;
    const chans = 2;
    const block_frames = 3968; // large: widens a single in-flight chunk's unpublished window
    const offset = 64; // small and INDEPENDENT of block_frames — see above for why coupling them fails
    const n = cap_frames - offset; // full window minus the (small, independent) tolerance offset
    var ring = try Ring.init(std.testing.allocator, .{
        .sample_rate = 48_000,
        .channels = chans,
        .seconds = @as(f64, cap_frames) / 48_000.0,
    });
    defer ring.deinit();

    const H = struct {
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

    // Most attempts return Overwritten by design (see above), so the
    // attempt budget needs to be much larger relative to the success
    // target than the mutation-3 test's. Measured numbers (this
    // workstation): see the task report.
    const target_successes: u64 = 500;
    const max_attempts: u64 = target_successes * 20_000;
    var attempts: u64 = 0;
    var successes: u64 = 0;
    var overwritten: u64 = 0;
    var out: [n * chans]f32 = undefined;
    while (successes < target_successes and attempts < max_attempts) {
        attempts += 1;
        const tw = ring.total_written.load(.acquire);
        if (tw < cap_frames - offset) {
            std.atomic.spinLoopHint();
            continue;
        }
        const abs_start = tw + offset - cap_frames; // add before subtract: avoids an intermediate underflow
        ring.read(abs_start, &out) catch |err| {
            if (err == error.Overwritten) overwritten += 1;
            continue;
        };
        for (0..n) |i| {
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
    if (successes < target_successes) {
        std.debug.print(
            "guard band boundary test: only {d}/{d} successes in {d} attempts ({d} Overwritten); cap {d}\n",
            .{ successes, target_successes, attempts, overwritten, max_attempts },
        );
        return error.StressTestDidNotConverge;
    }
}

test "flush racing a concurrent writer and reader never panics" {
    // `writer_active` (see flush()'s doc comment, issue #20) is
    // registered by a writer thread's OWNER — the control thread that
    // spawns and joins it (`Capture.start`/`stop`, `Mixer.start`/`stop`).
    // The raw writer thread below has no such owner and never sets it, so
    // `ring.flush()` here always takes the immediate branch, same as
    // before #20's fix — a writer that already loaded `tw` before the
    // flush still publishes `tw + n` afterward, so total_written and
    // the physical buffer contents can end up in ANY relative state
    // once a flush races an in-flight write. That is expected in THIS
    // configuration (no active writer registered) and is not what #20
    // closes. This is also, precisely, the "mixed slot" configuration
    // `flush()`'s doc comment (issue #23) describes for `Summary`: a
    // raw writer that never sets `writer_active` plus a concurrent
    // control-thread flusher races BOTH `total_written` here and
    // `Summary.gen` the same way — this test only pins the `Ring` half
    // (no panic, no trap), not the `Summary` half.
    //
    // Content correctness is therefore not meaningfully
    // verifiable while this race is live; this test instead verifies
    // the property that DOES matter:
    // read() never traps (Critical 1: an unsigned underflow when a
    // flush yanks total_written to 0 mid-read-attempt) and never
    // returns anything other than success or a documented `ReadError`.
    // This is the test the review said would have caught Critical 1
    // immediately — a deterministic single-threaded unit test cannot
    // reach that race window at all.
    const cap_frames = 4096;
    const chans = 2;
    const block_frames = 128;
    var ring = try Ring.init(std.testing.allocator, .{
        .sample_rate = 48_000,
        .channels = chans,
        .seconds = @as(f64, cap_frames) / 48_000.0,
    }); // block_frames (128) stays well under Ring.max_write_frames (4096), so write() never chunks here
    defer ring.deinit();

    const H = struct {
        fn writerLoop(r: *Ring, stop: *std.atomic.Value(bool)) void {
            var block: [block_frames * chans]f32 = [_]f32{1} ** (block_frames * chans);
            while (!stop.load(.monotonic)) r.write(&block);
        }
        fn flusherLoop(r: *Ring, stop: *std.atomic.Value(bool)) void {
            // std.Thread has no synchronous sleep in 0.16 (it moved
            // under the new std.Io async interface); yield + a short
            // spin gives "every so often" without pulling that in.
            var spin: u32 = 0;
            while (!stop.load(.monotonic)) {
                r.flush();
                std.Thread.yield() catch {};
                while (spin < 200_000) : (spin += 1) std.atomic.spinLoopHint();
                spin = 0;
            }
        }
    };

    var stop = std.atomic.Value(bool).init(false);
    const writer = try std.Thread.spawn(.{}, H.writerLoop, .{ &ring, &stop });
    const flusher = try std.Thread.spawn(.{}, H.flusherLoop, .{ &ring, &stop });
    // Defers run LIFO: stop must be signaled FIRST (declared LAST) so
    // both joins below actually observe it and return. Declaring the
    // signal before the joins (join-then-signal in source order, so
    // signal-then-join in LIFO execution) deadlocked during development:
    // flusher.join() ran before stop.store(true, ...) and waited forever
    // on a thread whose loop condition could never go false.
    defer writer.join();
    defer flusher.join();
    defer stop.store(true, .monotonic);

    var out: [64 * chans]f32 = undefined;
    var attempts: u32 = 0;
    while (attempts < 50_000) : (attempts += 1) {
        const tw = ring.total_written.load(.acquire);
        if (tw < 64) continue;
        _ = ring.read(tw - 64, &out) catch |err| switch (err) {
            error.OutOfRange, error.Overwritten => continue,
        };
    }
    // Reaching here — 50,000 read attempts against an actively racing
    // writer and flusher, none of them trapping the process — is the
    // pass condition.
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

test "write feeds the summary; flush poisons it" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 16, .channels = 1, .seconds = 1.0, .summary_slot_frames = 4 });
    defer ring.deinit();
    ring.write(&[_]f32{ 0.5, 0.5, 0.5, 0.5 });
    var out: [1]f32 = undefined;
    ring.summary.rmsBins(ring.total_written.load(.acquire), 0, 0, &out);
    try std.testing.expectApproxEqAbs(@as(f32, 0.5), out[0], 1e-6);
    ring.flush();
    // Direct check on the mechanism, not rmsBins: rmsBins(0, ...) here
    // would early-return on n_samples == 0 right after its leading
    // @memset(out, 0), before ever reading slot_abs — it can't tell
    // poison() ran from poison() being skipped (see the finding in the
    // task report / the follow-up test below, which pins the observable
    // consequence). slot_abs is a public field; check it directly.
    try std.testing.expectEqual(@as(i64, -1), ring.summary.slot_abs[0]);
}

test "flush poisons stale slot generations, not just resets total_written" {
    // The test above checks slot_abs directly, since rmsBins(0, ...)
    // would early-return on n_samples == 0 before ever reading slot_abs
    // — it cannot tell poison() ran from poison() being skipped. This
    // test targets the actual bug poisoning prevents: post-flush, abs
    // indices restart at 0, so a slot touched again by a NEW write can
    // collide with its OWN stale pre-flush tag (same numeric abs value)
    // and wrongly take
    // the "same generation, accumulate" branch instead of "new
    // generation, overwrite" — silently merging pre- and post-flush
    // audio into one slot's stats.
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 16, .channels = 1, .seconds = 1.0, .summary_slot_frames = 4 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 1, 1, 1 }); // fills slot 0, tag=0, ss=4, count=4
    ring.flush(); // total_written -> 0; slot 0's tag must become -1
    ring.write(&[_]f32{ 3, 3 }); // abs 0..2, slot 0 again, tag recomputed to 0
    var out: [1]f32 = undefined;
    ring.summary.rmsBins(ring.total_written.load(.acquire), 0, 0, &out);
    // If poisoned correctly: slot 0 was reset fresh, holding only the
    // two post-flush 3.0 samples -> RMS = 3.0. If poison() were a
    // no-op, slot 0's stale tag (0) still equals the recomputed
    // slot_start_abs (0), so update() would take the "same generation"
    // accumulate branch and merge in the pre-flush 1.0 samples too ->
    // RMS = sqrt((4*1 + 2*9) / 6) ~= 1.915, not 3.0.
    try std.testing.expectApproxEqAbs(@as(f32, 3.0), out[0], 1e-6);
}

test "flush while a writer is active is deferred to the writer and cannot be undone" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 2.0 }); // capacity 16
    defer ring.deinit();
    ring.writer_active.store(true, .release);
    ring.write(&[_]f32{ 1, 1, 1, 1 });
    ring.flush(); // deferred: total_written must NOT drop yet
    try std.testing.expectEqual(@as(u64, 4), ring.total_written.load(.acquire));
    try std.testing.expect(ring.flush_pending.load(.acquire));
    ring.write(&[_]f32{ 2, 2 }); // the writer executes the flush, then writes
    try std.testing.expectEqual(@as(u64, 2), ring.total_written.load(.acquire));
    try std.testing.expect(!ring.flush_pending.load(.acquire));
    var out: [2]f32 = undefined;
    try ring.read(0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 2, 2 }, &out);
}

test "flush with no active writer is immediate (unchanged behaviour)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 8, .channels = 1, .seconds = 2.0 });
    defer ring.deinit();
    ring.write(&[_]f32{ 1, 1 });
    ring.flush();
    try std.testing.expectEqual(@as(u64, 0), ring.total_written.load(.acquire));
    try std.testing.expect(!ring.flush_pending.load(.acquire));
}

test "write chunks a single large call correctly, tagging each summary chunk with its own start_abs" {
    const slot_frames = 1000;
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 10_000, .channels = 1, .seconds = 1.0, .summary_slot_frames = slot_frames });
    defer ring.deinit();
    // 9000 frames > 2 * max_write_frames (4096), so write() chunks this
    // single call into three release-stores: [0,4096), [4096,8192),
    // [8192,9000). Slot 4 (frames [4000,5000)) straddles the FIRST
    // chunk boundary at 4096 -- frames 4000..4095 come from chunk 0,
    // frames 4096..4999 from chunk 1. If write() passed each chunk's
    // summary.update() the ORIGINAL call's tw (0) instead of THAT
    // chunk's own start_abs, every frame from chunk 1 onward would be
    // mis-tagged into the wrong slot and this slot would come up short
    // or with the wrong bounds.
    var buf: [9000]f32 = undefined;
    for (&buf, 0..) |*s, i| s.* = @floatFromInt(i);
    ring.write(&buf);
    try std.testing.expectEqual(@as(i64, 4000), ring.summary.slot_abs[4]);
    try std.testing.expectEqual(@as(f32, 4000), ring.summary.min[4]);
    try std.testing.expectEqual(@as(f32, 4999), ring.summary.max[4]);
    try std.testing.expectEqual(@as(u64, 1000), ring.summary.count[4]);
}

fn peakRamp(ring: *Ring, n: usize) void {
    // Writes frames whose ch0 value is the absolute index; ch1 (if any) too.
    var buf: [1024]f32 = undefined;
    var abs: u64 = ring.total_written.load(.acquire);
    var left = n;
    while (left > 0) {
        const take = @min(left, 1024 / @as(usize, ring.channels));
        for (0..take) |i| {
            for (0..ring.channels) |c| buf[i * ring.channels + c] = @floatFromInt(abs + i);
        }
        ring.write(buf[0 .. take * ring.channels]);
        abs += take;
        left -= take;
    }
}

fn expectBin(out: []const PeakBin, i: usize, min: f32, max: f32) !void {
    try std.testing.expectEqual(min, out[i].min);
    try std.testing.expectEqual(max, out[i].max);
}

test "peakBins: exact edges, stride 1 (case A)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 1000);
    var out: [4]PeakBin = undefined;
    try ring.peakBins(1000, 4, &out);
    try expectBin(&out, 0, 0, 249);
    try expectBin(&out, 1, 250, 499);
    try expectBin(&out, 2, 500, 749);
    try expectBin(&out, 3, 750, 999);
}

test "peakBins: uneven edges truncate like numpy linspace (case B)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 10);
    var out: [4]PeakBin = undefined;
    try ring.peakBins(10, 4, &out);
    try expectBin(&out, 0, 0, 1);
    try expectBin(&out, 1, 2, 4);
    try expectBin(&out, 2, 5, 6);
    try expectBin(&out, 3, 7, 9);
}

test "peakBins: an empty bin copies its predecessor; an empty bin 0 stays zero (case C)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 3);
    var out: [5]PeakBin = undefined;
    try ring.peakBins(3, 5, &out);
    try expectBin(&out, 0, 0, 0);
    try expectBin(&out, 1, 0, 0);
    try expectBin(&out, 2, 0, 0);
    try expectBin(&out, 3, 1, 1);
    try expectBin(&out, 4, 2, 2);
    // Second ring: 5 frames, 7 bins, step 5/7 -> edges 0,0,1,2,2,3,4,5.
    // Bin 3 ([2,2)) is empty with a NON-zero predecessor (1,1): this is
    // what pins the copy -- in the first ring the predecessor is (0,0),
    // so deleting the copy would not redden it.
    var ring2 = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring2.deinit();
    peakRamp(&ring2, 5);
    var out7: [7]PeakBin = undefined;
    try ring2.peakBins(5, 7, &out7);
    try expectBin(&out7, 2, 1, 1);
    try expectBin(&out7, 3, 1, 1);
    try expectBin(&out7, 6, 4, 4);
}

test "peakBins: wraps at storage_frames, not capacity (case D)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 6000); // past storage_frames (1000 + 4096)
    var out: [2]PeakBin = undefined;
    try ring.peakBins(1000, 2, &out);
    try expectBin(&out, 0, 5000, 5499);
    try expectBin(&out, 1, 5500, 5999);
}

test "peakBins: headroom clamp and absolute stride grid (cases E, F)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 10_000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 10_000);
    var out: [2]PeakBin = undefined;
    try ring.peakBins(10_000, 2, &out);
    try expectBin(&out, 0, 4103, 7051);
    try expectBin(&out, 1, 7051, 9999);
    // Pins the headroom constant itself (4096): at 2 bins a one-frame
    // shift is invisible, at 100 bins it moves an edge.
    var out100: [100]PeakBin = undefined;
    try ring.peakBins(10_000, 100, &out100);
    try expectBin(&out100, 1, 4155, 4213); // edge 1 = floor(59.04) = 59 -> abs 4155; a 4095 headroom gives 4154
    peakRamp(&ring, 1); // roll the window by one frame: the grid does not move
    try ring.peakBins(10_000, 2, &out);
    try expectBin(&out, 0, 4103, 7051);
}

test "peakBins: edges are trunc(float(i) * step), not trunc(i * n / n_bins) (case G)" {
    // n = 30, n_bins = 22: step = 30/22 in f64 = 1.3636...; 11 * step = 14.999... -> 14.
    // The exact rational 11 * 30 / 22 = 15. numpy's linspace takes the f64
    // product, so the port must too; a different multiply order moves this
    // edge by one frame and shifts every waveform golden.
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 30);
    var out: [22]PeakBin = undefined;
    try ring.peakBins(30, 22, &out);
    try expectBin(&out, 10, 13, 13); // [13, 14)
    try expectBin(&out, 11, 14, 15); // [14, 16)
}

test "peakBins: channels reduce independently (case H)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 2, .seconds = 1.0 });
    defer ring.deinit();
    var frames: [1000]f32 = undefined;
    for (0..500) |i| {
        frames[i * 2] = 0;
        frames[i * 2 + 1] = @floatFromInt(i);
    }
    ring.write(&frames);
    var out: [2]PeakBin = undefined;
    try ring.peakBins(500, 1, &out);
    try expectBin(&out, 0, 0, 0);
    try expectBin(&out, 1, 0, 499);
}

test "peakBins: empty window zeroes out and succeeds; n_bins == 0 is InvalidArgument (case I)" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    var out: [3]PeakBin = .{ .{ .min = 9, .max = 9 }, .{ .min = 9, .max = 9 }, .{ .min = 9, .max = 9 } };
    try ring.peakBins(1000, 3, &out);
    for (out) |b| try std.testing.expectEqual(@as(f32, 0), b.max);
    try std.testing.expectError(error.InvalidArgument, ring.peakBins(1000, 0, out[0..0]));
}

test "peakBins: a request shorter than the available window reads only the newest frames" {
    var ring = try Ring.init(std.testing.allocator, .{ .sample_rate = 1000, .channels = 1, .seconds = 1.0 });
    defer ring.deinit();
    peakRamp(&ring, 1000);
    var out: [2]PeakBin = undefined;
    try ring.peakBins(100, 2, &out); // newest 100 of 1000: abs 900..999
    try expectBin(&out, 0, 900, 949);
    try expectBin(&out, 1, 950, 999);
}
