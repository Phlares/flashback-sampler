//! Minimal RIFF/WAVE writer and reader. FLOAT32 payload is the ring's
//! bytes verbatim — a bit-perfect pull. 44-byte canonical header;
//! libsndfile and every DAW read it. Parity is checked by
//! `tests/fixtures/wavread.py`, an independent stdlib reader:
//! DECODE-equality (samples + format), not byte-equality (libsndfile
//! adds PEAK/fact chunks we deliberately don't).
const std = @import("std");
const builtin = @import("builtin");

comptime {
    // FLOAT32's memcpy path writes host-endian bits as file bytes.
    // Every supported target is little-endian; make that loud, not lucky.
    std.debug.assert(builtin.target.cpu.arch.endian() == .little);
}

test "golden 44-byte header: 48k stereo float32, 4 frames" {
    var h: [44]u8 = undefined;
    try writeHeader(&h, 48_000, 2, .float32, 4);
    try std.testing.expectEqualSlices(u8, "RIFF", h[0..4]);
    try std.testing.expectEqual(@as(u32, 68), std.mem.readInt(u32, h[4..8], .little)); // 36 + data(32)
    try std.testing.expectEqualSlices(u8, "WAVE", h[8..12]);
    try std.testing.expectEqualSlices(u8, "fmt ", h[12..16]);
    try std.testing.expectEqual(@as(u32, 16), std.mem.readInt(u32, h[16..20], .little));
    try std.testing.expectEqual(@as(u16, 3), std.mem.readInt(u16, h[20..22], .little)); // IEEE float
    try std.testing.expectEqual(@as(u16, 2), std.mem.readInt(u16, h[22..24], .little));
    try std.testing.expectEqual(@as(u32, 48_000), std.mem.readInt(u32, h[24..28], .little));
    try std.testing.expectEqual(@as(u32, 384_000), std.mem.readInt(u32, h[28..32], .little)); // byte rate
    try std.testing.expectEqual(@as(u16, 8), std.mem.readInt(u16, h[32..34], .little)); // block align
    try std.testing.expectEqual(@as(u16, 32), std.mem.readInt(u16, h[34..36], .little)); // bits
    try std.testing.expectEqualSlices(u8, "data", h[36..40]);
    try std.testing.expectEqual(@as(u32, 32), std.mem.readInt(u32, h[40..44], .little));
}

test "writeHeader rejects n_frames whose data size overflows the u32 RIFF size, instead of trapping" {
    // block_align = 8 (stereo float32); n_frames chosen so
    // n_frames * block_align alone already exceeds maxInt(u32) - 36 by a
    // wide margin, well past the point `@intCast` would trap in
    // ReleaseSafe on the raw product.
    var h: [44]u8 = undefined;
    try std.testing.expectError(error.TooLong, writeHeader(&h, 48_000, 2, .float32, 600_000_000));
}

/// The sample formats this module supports (writer and reader). Backed
/// by `u8` so the wire value (used by Task 6's C ABI: 0/1/2) is stable
/// across Zig versions.
pub const Subtype = enum(u8) {
    float32 = 0,
    pcm_24 = 1,
    pcm_16 = 2,

    // comptime-checked exhaustive dispatch: adding a subtype without
    // updating these tables is a compile error, not a runtime surprise.
    pub fn bytesPerSample(self: Subtype) u8 {
        return switch (self) {
            .float32 => 4,
            .pcm_24 => 3,
            .pcm_16 => 2,
        };
    }
    fn formatTag(self: Subtype) u16 {
        return switch (self) {
            .float32 => 3, // WAVE_FORMAT_IEEE_FLOAT
            .pcm_24, .pcm_16 => 1, // WAVE_FORMAT_PCM
        };
    }
};

pub const header_len = 44;

/// The one `std.Io` every wav call uses — the synchronous singleton
/// `writeFile` also reaches for (see its doc comment for why). Public so
/// callers that hold a `File` from `open` can close it with the same Io.
/// `open`/`readPositionalAll` use no state of the `Threaded` singleton —
/// the pinned std's `dirOpenFileWindows` and `fileReadPositional`
/// discard it (`_ = t;`) — so readers take no lock; writers serialise
/// under `write_mutex`, defined below.
pub const io = std.Io.Threaded.global_single_threaded.io();

/// Serialises every writer in this file (`writeFile`, `copyRange`).
/// `global_single_threaded` is documented as not supporting concurrency
/// (see `writeFile`'s doc comment); rather than trace every syscall
/// wrapper for shared state, one lock makes the question moot. Callers
/// lock it: `wav.write_mutex.lockUncancelable(wav.io)` /
/// `defer wav.write_mutex.unlock(wav.io)`. Never taken on an audio
/// thread. Lives here (not in abi.zig) because Scratch.zig must lock
/// it too and Scratch must not import abi.
pub var write_mutex: std.Io.Mutex = .init;

/// What a reader needs to pull samples: format, count, and where the
/// payload starts. `frames` is clamped to what the FILE holds, not what
/// the `data` size claims — a `.part` left by a crash reads its true
/// prefix instead of failing.
pub const Info = struct {
    rate: u32,
    channels: u16,
    subtype: Subtype,
    frames: u64,
    data_offset: u64,

    pub fn blockAlign(self: Info) u64 {
        return @as(u64, self.channels) * self.subtype.bytesPerSample();
    }
};

pub const ParseError = error{ NotWave, MissingFmt, MissingData, Unsupported };
pub const OpenError = ParseError || std.Io.File.OpenError || std.Io.File.ReadPositionalError || std.Io.File.LengthError;
pub const Opened = struct { file: std.Io.File, info: Info };

/// Open `path` and walk its chunks to the `data` chunk. Positional reads
/// (`readPositionalAll` takes an explicit offset, so no seek state is
/// shared between threads). DAW-written files put `bext`, `iXML` and
/// `LIST` chunks of kilobytes before `data`; the walk skips any chunk it
/// does not know, honouring the RIFF word-alignment pad byte.
pub fn open(path: []const u8) OpenError!Opened {
    const file = try std.Io.Dir.cwd().openFile(io, path, .{});
    errdefer file.close(io);
    return .{ .file = file, .info = try scan(file) };
}

const Fmt = struct { channels: u16, rate: u32, block_align: u16, subtype: Subtype };

fn scan(file: std.Io.File) OpenError!Info {
    const len = try file.length(io);
    var hdr: [12]u8 = undefined;
    if (try file.readPositionalAll(io, &hdr, 0) != 12) return error.NotWave;
    if (!std.mem.eql(u8, hdr[0..4], "RIFF") or !std.mem.eql(u8, hdr[8..12], "WAVE")) return error.NotWave;
    var pos: u64 = 12;
    var fmt: ?Fmt = null;
    while (pos + 8 <= len) {
        var ch: [8]u8 = undefined;
        if (try file.readPositionalAll(io, &ch, pos) != 8) break;
        const size: u64 = std.mem.readInt(u32, ch[4..8], .little);
        const body = pos + 8;
        if (std.mem.eql(u8, ch[0..4], "fmt ")) {
            var fb: [40]u8 = undefined;
            const want: usize = @intCast(@min(size, 40));
            if (want < 16 or try file.readPositionalAll(io, fb[0..want], body) != want) return error.MissingFmt;
            fmt = try parseFmt(fb[0..want]);
        } else if (std.mem.eql(u8, ch[0..4], "data")) {
            const f = fmt orelse return error.MissingFmt;
            const avail = @min(size, len - body);
            return .{
                .rate = f.rate,
                .channels = f.channels,
                .subtype = f.subtype,
                .frames = avail / f.block_align,
                .data_offset = body,
            };
        }
        pos = body + size + (size & 1); // chunks are word-aligned
    }
    return if (fmt == null) error.MissingFmt else error.MissingData;
}

/// The fmt body. Plain: tag u16, channels u16, rate u32, byte rate u32,
/// block align u16, bits u16. EXTENSIBLE (tag 0xFFFE): cbSize u16,
/// valid bits u16, channel mask u32, then the 16-byte SubFormat GUID
/// at offset 24 whose first two bytes carry the real tag. Same rule as
/// tests/fixtures/wavread.py — the independent oracle.
fn parseFmt(fb: []const u8) ParseError!Fmt {
    var tag = std.mem.readInt(u16, fb[0..2], .little);
    const channels = std.mem.readInt(u16, fb[2..4], .little);
    const rate = std.mem.readInt(u32, fb[4..8], .little);
    const block_align = std.mem.readInt(u16, fb[12..14], .little);
    const bits = std.mem.readInt(u16, fb[14..16], .little);
    if (tag == 0xFFFE) {
        if (fb.len < 26) return error.Unsupported;
        tag = std.mem.readInt(u16, fb[24..26], .little);
    }
    const subtype: Subtype = switch (tag) {
        3 => if (bits == 32) Subtype.float32 else return error.Unsupported,
        1 => switch (bits) {
            16 => Subtype.pcm_16,
            24 => Subtype.pcm_24,
            else => return error.Unsupported,
        },
        else => return error.Unsupported,
    };
    if (channels == 0 or rate == 0 or channels > max_channels) return error.Unsupported;
    // channels (u16) * bytesPerSample (u8) peer-types to u16 and overflows
    // above 16383 float32 channels — widen to u32 first so a malformed
    // header returns Unsupported instead of an integer-overflow trap.
    // With max_channels enforced above, this product fits u16 anyway;
    // the widening stays as defence so the two guards remain independent.
    if (block_align != @as(u32, channels) * subtype.bytesPerSample()) return error.Unsupported;
    return .{ .channels = channels, .rate = rate, .block_align = block_align, .subtype = subtype };
}

/// Read and write share one chunk size: 64 KiB on the stack, never the
/// heap. 16384 float32 mono samples, 8192 stereo frames per iteration.
pub const read_chunk_bytes = 16384 * 4;

/// Cap on channels: the tighter of two bounds. (1) `writeHeader`'s
/// `block_align: u16 = @intCast(bps * channels)` must fit a u16 — for
/// the widest subtype (float32, 4 bytes/sample) that caps channels at
/// 65535 / 4 = 16383 (16384 already overflows: 4 * 16384 = 65536). (2)
/// every chunk loop (`copyRange`, `peaks.peakBinsFile`) sizes its f32
/// buffer as `read_chunk_bytes / 4` samples and divides by `channels` to
/// get frames-per-chunk, which needs channels <= 16384 to stay >= 1.
/// (1) is the tighter bound, so it sets the cap. Enforced at the two
/// entry points (parseFmt, fb_wav_write) instead of in every loop.
pub const max_channels: u16 = std.math.maxInt(u16) / 4;

pub const ReadError = error{OutOfRange} || std.Io.File.ReadPositionalError;

/// Fill `out` (interleaved, `out.len / info.channels` frames) from
/// `start_frame`. Whole-span or nothing: a span past `info.frames`
/// returns OutOfRange before any read. Precondition: `out.len` must be
/// a multiple of `info.channels` (checked by the assert below).
pub fn readFrames(file: std.Io.File, info: Info, start_frame: u64, out: []f32) ReadError!void {
    const chans: u64 = info.channels;
    std.debug.assert(out.len % chans == 0);
    const n_frames: u64 = out.len / chans;
    // Subtraction form, not `start_frame + n_frames > info.frames`: the
    // addition can overflow-trap in ReleaseSafe on a hostile
    // start_frame, where this form cannot.
    if (start_frame > info.frames or n_frames > info.frames - start_frame) return error.OutOfRange;
    const block = info.blockAlign();
    const frames_per_chunk: u64 = read_chunk_bytes / block;
    var buf: [read_chunk_bytes]u8 = undefined;
    var done: u64 = 0;
    while (done < n_frames) {
        const take = @min(n_frames - done, frames_per_chunk);
        const nbytes: usize = @intCast(take * block);
        const offset = info.data_offset + (start_frame + done) * block;
        const got = try file.readPositionalAll(io, buf[0..nbytes], offset);
        // `frames` was clamped to the file at open; a short read here
        // means the file shrank underneath us. Treat it as the span
        // being gone, not as silence.
        if (got != nbytes) return error.OutOfRange;
        const o_start: usize = @intCast(done * chans);
        const o_end: usize = @intCast((done + take) * chans);
        decodeSamples(info.subtype, buf[0..nbytes], out[o_start..o_end]);
        done += take;
    }
}

/// The inverse of `encodeSamples`. PCM codes divide by 2^(bits-1) — the
/// libsndfile convention `tests/fixtures/wavread.py` pins (32767 reads
/// as 32767/32768) — so encode→decode is exact at the codes, not at the
/// original floats. FLOAT32 is a memcpy of the bits. Precondition:
/// `bytes.len` must be at least `out.len * bytesPerSample(st)`.
pub fn decodeSamples(st: Subtype, bytes: []const u8, out: []f32) void {
    switch (st) {
        .float32 => @memcpy(std.mem.sliceAsBytes(out), bytes[0 .. out.len * 4]),
        .pcm_16 => for (out, 0..) |*s, i| {
            const v = std.mem.readInt(i16, bytes[i * 2 ..][0..2], .little);
            s.* = @as(f32, @floatFromInt(v)) / 32768.0;
        },
        .pcm_24 => for (out, 0..) |*s, i| {
            // Three little-endian bytes; shift into the top of an i32 so
            // the arithmetic shift back sign-extends bit 23.
            const raw: u32 = @as(u32, bytes[i * 3]) | (@as(u32, bytes[i * 3 + 1]) << 8) | (@as(u32, bytes[i * 3 + 2]) << 16);
            const v: i32 = @as(i32, @bitCast(raw << 8)) >> 8;
            s.* = @as(f32, @floatFromInt(v)) / 8388608.0;
        },
    }
}

/// Write a canonical 44-byte RIFF/WAVE header for `n_frames` frames of
/// `channels`-channel audio at `rate` Hz in subtype `st`. A "frame" is
/// one sample per channel, so the data chunk size is
/// `n_frames * channels * bytesPerSample`.
///
/// `n_frames` arrives raw from callers (some, eventually, straight off a
/// ctypes boundary) — a huge value must fail with `error.TooLong`, not
/// trap. Two overflow points guard against that: the `n_frames *
/// block_align` product itself (checked with `std.math.mul`, since a
/// plain `*` would be illegal behavior — a ReleaseSafe trap — on
/// overflow), and the RIFF chunk size field, which stores `36 + data_len`
/// in a u32 and so needs `data_len <= maxInt(u32) - 36`.
pub fn writeHeader(out: *[header_len]u8, rate: u32, channels: u16, st: Subtype, n_frames: u64) error{TooLong}!void {
    const bps: u32 = st.bytesPerSample();
    const block_align: u16 = @intCast(bps * channels);
    const data_len_wide: u64 = std.math.mul(u64, n_frames, block_align) catch return error.TooLong;
    if (data_len_wide > std.math.maxInt(u32) - 36) return error.TooLong;
    const data_len: u32 = @intCast(data_len_wide);
    @memcpy(out[0..4], "RIFF");
    std.mem.writeInt(u32, out[4..8], 36 + data_len, .little);
    @memcpy(out[8..12], "WAVE");
    @memcpy(out[12..16], "fmt ");
    std.mem.writeInt(u32, out[16..20], 16, .little);
    std.mem.writeInt(u16, out[20..22], st.formatTag(), .little);
    std.mem.writeInt(u16, out[22..24], channels, .little);
    std.mem.writeInt(u32, out[24..28], rate, .little);
    std.mem.writeInt(u32, out[28..32], rate * block_align, .little);
    std.mem.writeInt(u16, out[32..34], block_align, .little);
    std.mem.writeInt(u16, out[34..36], @as(u16, @intCast(bps)) * 8, .little);
    @memcpy(out[36..40], "data");
    std.mem.writeInt(u32, out[40..44], data_len, .little);
}

/// Encode f32 samples into `out` per the subtype's quantization
/// contract. Returns bytes written. `out` must hold at least
/// `samples.len * st.bytesPerSample()` bytes.
///
/// FLOAT32 is a raw memcpy of the f32 bits — no per-sample conversion —
/// so the ring's payload lands byte-identical in the file; this is the
/// bit-perfect pull the module doc references, and it depends on the
/// little-endian comptime assert above.
///
/// pcm_16 / pcm_24 quantize x in [-1, 1] to the widest signed range
/// that keeps `round(1.0 * scale)` in range: scale = 32767 / 8388607
/// (not 32768 / 8388608), so +full-scale and -full-scale are NOT
/// symmetric — -1.0 lands one LSB short of the negative rail. That
/// asymmetry is the documented contract `tests/unit/test_native_smoke.py`
/// pins through `wavread.py`, not a bug.
///
/// A NaN sample silently becomes full-scale positive here: `clamp`'s
/// `@min`/`@max` always return the finite bound when compared against
/// NaN, so NaN never reaches `@intFromFloat` as NaN — it reads as
/// "loud", not silence or an error.
pub fn encodeSamples(st: Subtype, samples: []const f32, out: []u8) usize {
    switch (st) {
        .float32 => {
            const bytes = std.mem.sliceAsBytes(samples);
            @memcpy(out[0..bytes.len], bytes);
            return bytes.len;
        },
        .pcm_16 => {
            for (samples, 0..) |s, i| {
                const clamped = std.math.clamp(s, -1.0, 1.0);
                // The outer clamp's lower rail (-32768) is dead for every
                // finite input: the inner clamp above already bounds
                // `clamped` to ±1.0, so the most negative value @round
                // ever sees is -1.0 * 32767 = -32767, one LSB inside this
                // floor. Kept anyway as a defensive guard at this
                // ABI-adjacent boundary, in case the inner clamp is ever
                // moved or removed — see the "did not redden" mutation
                // finding in the Task 5 report.
                const v: i16 = @intFromFloat(std.math.clamp(@round(clamped * 32767.0), -32768.0, 32767.0));
                std.mem.writeInt(i16, out[i * 2 ..][0..2], v, .little);
            }
            return samples.len * 2;
        },
        .pcm_24 => {
            for (samples, 0..) |s, i| {
                const clamped = std.math.clamp(s, -1.0, 1.0);
                // Same dead lower rail as pcm_16 above (-8388608 here):
                // unreachable given the inner clamp, kept as a defensive
                // guard rather than relied on.
                const v: i32 = @intFromFloat(std.math.clamp(@round(clamped * 8388607.0), -8388608.0, 8388607.0));
                const bits: u32 = @bitCast(v);
                out[i * 3] = @truncate(bits);
                out[i * 3 + 1] = @truncate(bits >> 8);
                out[i * 3 + 2] = @truncate(bits >> 16);
            }
            return samples.len * 3;
        },
    }
}

/// Stream `n_frames` from `start_frame` of `src` into a new `dst` in
/// subtype `st`. One 64 KiB read buffer, one 64 KiB encode buffer, both
/// on the stack. The range is validated against the source BEFORE dst
/// is created, so an OutOfRange leaves no file behind. Serialisation
/// with other writers is the caller's job (PR h: `write_mutex`).
pub fn copyRange(src: []const u8, dst: []const u8, start_frame: u64, n_frames: u64, st: Subtype) !void {
    var o = try open(src);
    defer o.file.close(io);
    if (start_frame > o.info.frames or n_frames > o.info.frames - start_frame) return error.OutOfRange;
    const chans: u64 = o.info.channels;
    const data_len_wide: u64 = n_frames * chans * st.bytesPerSample();
    if (data_len_wide > std.math.maxInt(u32) - header_len) return error.TooLong;
    var out = try std.Io.Dir.cwd().createFile(io, dst, .{});
    defer out.close(io);
    var header: [header_len]u8 = undefined;
    try writeHeader(&header, o.info.rate, o.info.channels, st, n_frames);
    try out.writeStreamingAll(io, &header);
    const frames_per_chunk: u64 = read_chunk_bytes / (4 * chans);
    var samples: [read_chunk_bytes / 4]f32 = undefined;
    var enc: [read_chunk_bytes]u8 = undefined;
    var done: u64 = 0;
    while (done < n_frames) {
        const take = @min(n_frames - done, frames_per_chunk);
        const ns: usize = @intCast(take * chans);
        try readFrames(o.file, o.info, start_frame + done, samples[0..ns]);
        const n = encodeSamples(st, samples[0..ns], &enc);
        try out.writeStreamingAll(io, enc[0..n]);
        done += take;
    }
}

/// Stream samples to `path` through a fixed 64 KiB stack buffer — no
/// allocation regardless of clip length (a 15-minute grab never doubles
/// memory). Chunk boundary is sample-aligned for every subtype
/// (16384 samples * 4 bytes max = the buffer size).
///
/// Zig 0.16 reworked file I/O behind `std.Io`: every `Dir`/`File`
/// operation now takes an explicit `Io` implementation instead of going
/// through a hidden global, the way `std.fs.cwd().createFile(...)` did
/// pre-0.15. This module has no caller-supplied `Io` to thread through
/// (the brief's signature, and Task 6's C ABI on top of it, take just a
/// path) and file writing here is a one-shot synchronous leaf
/// operation, not something that benefits from being woven into an
/// async runtime — so we reach for
/// `std.Io.Threaded.global_single_threaded`, the singleton std reserves
/// for exactly this "hardcode a synchronous Io" case. It needs no
/// `deinit`.
///
/// Concurrency caveat: `global_single_threaded` is documented by std
/// (`std/Io/Threaded.zig`) as not supporting concurrency or
/// cancelation — that doc comment is written against `Io.async` /
/// `Io.concurrent` / task groups, none of which `writeFile` uses, but
/// it has not been exhaustively traced against every syscall wrapper
/// this function does call (`createFile`, `writeStreamingAll`,
/// `close`) for other shared mutable state in the singleton. Whether
/// concurrent or re-entrant calls to `writeFile` from multiple threads
/// are safe is therefore an open question, not a verified guarantee.
/// A caller that may issue concurrent writes (Task 6's C ABI is a
/// plausible one) must serialize them itself.
pub fn writeFile(path: []const u8, samples: []const f32, rate: u32, channels: u16, st: Subtype) !void {
    const data_len_wide: u64 = @as(u64, samples.len) * st.bytesPerSample();
    if (data_len_wide > std.math.maxInt(u32) - header_len) return error.TooLong;
    var file = try std.Io.Dir.cwd().createFile(io, path, .{});
    defer file.close(io);
    var header: [header_len]u8 = undefined;
    try writeHeader(&header, rate, channels, st, samples.len / channels);
    try file.writeStreamingAll(io, &header);
    var buf: [read_chunk_bytes]u8 = undefined;
    var remaining = samples;
    while (remaining.len > 0) {
        const take = @min(remaining.len, read_chunk_bytes / 4);
        const n = encodeSamples(st, remaining[0..take], &buf);
        try file.writeStreamingAll(io, buf[0..n]);
        remaining = remaining[take..];
    }
}

test "float32 encode is the raw bits" {
    const in = [_]f32{ 0.5, -1.0 };
    var out: [8]u8 = undefined;
    try std.testing.expectEqual(@as(usize, 8), encodeSamples(.float32, &in, &out));
    try std.testing.expectEqualSlices(u8, std.mem.sliceAsBytes(&in), &out);
}

test "pcm16 quantization: round-half-away, clamped" {
    const in = [_]f32{ 0.0, 1.0, -1.0, 0.5, 1.5 }; // 1.5 must clamp
    var out: [10]u8 = undefined;
    _ = encodeSamples(.pcm_16, &in, &out);
    try std.testing.expectEqual(@as(i16, 0), std.mem.readInt(i16, out[0..2], .little));
    try std.testing.expectEqual(@as(i16, 32767), std.mem.readInt(i16, out[2..4], .little));
    try std.testing.expectEqual(@as(i16, -32767), std.mem.readInt(i16, out[4..6], .little));
    try std.testing.expectEqual(@as(i16, 16384), std.mem.readInt(i16, out[6..8], .little)); // round(0.5*32767)=16384 (16383.5 → away from zero)
    try std.testing.expectEqual(@as(i16, 32767), std.mem.readInt(i16, out[8..10], .little));
}

test "pcm16 quantization: negative exact-half rounds away from zero" {
    // The positive-direction half case above (0.5 -> 16384) only pins
    // @round's away-from-zero behavior for positive inputs. -0.5 * 32767
    // = -16383.5, an exact half in the negative direction; round-half-
    // away-from-zero must give -16384, not -16383. Verified this test
    // actually distinguishes the contract by mutating @round to @trunc
    // (round-toward-zero), which gives -16383 and reddens here —
    // round-half-to-even was tried first and rejected as a mutation
    // because it coincidentally also picks -16384 (the even neighbor)
    // for this particular value, so it wouldn't have caught a
    // half-to-even regression here.
    const in = [_]f32{-0.5};
    var out: [2]u8 = undefined;
    _ = encodeSamples(.pcm_16, &in, &out);
    try std.testing.expectEqual(@as(i16, -16384), std.mem.readInt(i16, out[0..2], .little));
}

test "pcm24 writes 3 little-endian bytes per sample" {
    const in = [_]f32{ 1.0, -1.0 };
    var out: [6]u8 = undefined;
    _ = encodeSamples(.pcm_24, &in, &out);
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0xFF, 0xFF, 0x7F }, out[0..3]); // 8388607
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0x01, 0x00, 0x80 }, out[3..6]); // -8388607
}

// `writeFile` takes a path, not a `Dir` — that's the shape Task 6's C
// ABI needs (a C caller has no `Dir` handle to hand in), so it must
// stay that way. `std.testing.tmpDir` hands back an already-open
// `Dir` for reading, but `writeFile` needs a *string* to write
// through. `tmpDir` builds its directory at a fixed, cwd-relative
// spot — `.zig-cache/tmp/<random sub_path>/` — and returns that
// `sub_path`, so joining it back into the same relative form gives
// `writeFile` a path that resolves to the exact directory `tmpDir`
// already created and opened. This is portable (no machine-specific
// absolute path baked in), self-cleaning (`tmp.cleanup()` deletes the
// whole subtree — no more `defer ... deleteFile(...) catch {}`), and
// still never touches a tracked path in the repo, since `.zig-cache`
// is gitignored build output.
fn tmpWritePath(buf: []u8, tmp: *const std.testing.TmpDir, filename: []const u8) []const u8 {
    return std.fmt.bufPrint(buf, ".zig-cache/tmp/{s}/{s}", .{ tmp.sub_path, filename }) catch unreachable;
}

test "writeFile round-trips float32 through a real file" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [64]u8 = undefined;
    const path = tmpWritePath(&path_buf, &tmp, "roundtrip.wav");

    const in = [_]f32{ 0.1, -0.2, 0.3, -0.4 }; // 2 stereo frames
    try writeFile(path, &in, 48_000, 2, .float32);
    var buf: [44 + 16]u8 = undefined;
    const got = try tmp.dir.readFile(std.testing.io, "roundtrip.wav", &buf);
    try std.testing.expectEqual(@as(usize, 60), got.len);
    try std.testing.expectEqualSlices(u8, std.mem.sliceAsBytes(&in), got[44..]);
}

test "writeFile with zero samples writes a header-only file" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [64]u8 = undefined;
    const path = tmpWritePath(&path_buf, &tmp, "empty.wav");

    const in = [_]f32{};
    try writeFile(path, &in, 48_000, 2, .pcm_16);
    var buf: [header_len]u8 = undefined;
    const got = try tmp.dir.readFile(std.testing.io, "empty.wav", &buf);
    try std.testing.expectEqual(@as(usize, header_len), got.len);
    try std.testing.expectEqual(@as(u32, 36), std.mem.readInt(u32, got[4..8], .little)); // 36 + 0 data bytes
    try std.testing.expectEqual(@as(u32, 0), std.mem.readInt(u32, got[40..44], .little)); // data chunk size 0
}

test "writeFile spans more than one chunk-buffer iteration" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [64]u8 = undefined;
    const path = tmpWritePath(&path_buf, &tmp, "chunked.wav");

    // writeFile's fixed buffer caps a chunk at 16384 samples; +5 forces
    // a second, partial chunk through the `while` loop — the round-trip
    // test above only ever exercises a single chunk.
    const n = 16384 + 5;
    var samples: [n]f32 = undefined;
    for (&samples, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i)) / @as(f32, n);
    try writeFile(path, &samples, 44_100, 1, .float32);
    var buf: [header_len + n * 4]u8 = undefined;
    const got = try tmp.dir.readFile(std.testing.io, "chunked.wav", &buf);
    try std.testing.expectEqual(@as(usize, header_len + n * 4), got.len);
    try std.testing.expectEqualSlices(u8, std.mem.sliceAsBytes(&samples), got[header_len..]);
}

test "writeFile rejects a data chunk that would overflow the u32 RIFF sizes without touching disk" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var path_buf: [64]u8 = undefined;
    const path = std.fmt.bufPrintZ(&path_buf, ".zig-cache/tmp/{s}/never.wav", .{tmp.sub_path}) catch unreachable;
    // A slice header with an impossible length: we never read it — the
    // guard must fire on the arithmetic alone.
    const huge: []const f32 = @as([*]const f32, @ptrFromInt(0x1000))[0 .. (std.math.maxInt(u32) / 4) + 1];
    try std.testing.expectError(error.TooLong, writeFile(path, huge, 48_000, 1, .float32));
}

test "pcm16/pcm24 negative extreme inputs share -1.0's quantized floor" {
    // The quantization contract's outer clamp floor (-32768 / -8388608)
    // is unreachable through normal input: the inner clamp restricts x
    // to [-1, 1] before scaling, so the most negative value @round ever
    // sees is -1.0 * scale — one LSB above the outer floor. An input
    // far below -1.0 (an upstream clipping bug) lands on the same
    // result as legitimate full-scale-negative audio, not on the
    // theoretical floor.
    var out16: [4]u8 = undefined;
    _ = encodeSamples(.pcm_16, &[_]f32{ -1.0, -50.0 }, &out16);
    try std.testing.expectEqual(
        std.mem.readInt(i16, out16[0..2], .little),
        std.mem.readInt(i16, out16[2..4], .little),
    );

    var out24: [6]u8 = undefined;
    _ = encodeSamples(.pcm_24, &[_]f32{ -1.0, -50.0 }, &out24);
    try std.testing.expectEqualSlices(u8, out24[0..3], out24[3..6]);
}

/// Test helper: a minimal RIFF/WAVE byte image. `fmt_body` is the raw
/// fmt chunk body (16 bytes plain, 40 bytes EXTENSIBLE); `pre_data` is
/// any chunk bytes to place between fmt and data; `data` is the payload.
fn wavImage(buf: []u8, fmt_body: []const u8, pre_data: []const u8, data: []const u8) []const u8 {
    var w: usize = 0;
    @memcpy(buf[w .. w + 4], "RIFF");
    w += 4;
    const riff_len: u32 = @intCast(4 + 8 + fmt_body.len + pre_data.len + 8 + data.len);
    std.mem.writeInt(u32, buf[w..][0..4], riff_len, .little);
    w += 4;
    @memcpy(buf[w .. w + 4], "WAVE");
    w += 4;
    @memcpy(buf[w .. w + 4], "fmt ");
    w += 4;
    std.mem.writeInt(u32, buf[w..][0..4], @intCast(fmt_body.len), .little);
    w += 4;
    @memcpy(buf[w .. w + fmt_body.len], fmt_body);
    w += fmt_body.len;
    @memcpy(buf[w .. w + pre_data.len], pre_data);
    w += pre_data.len;
    @memcpy(buf[w .. w + 4], "data");
    w += 4;
    std.mem.writeInt(u32, buf[w..][0..4], @intCast(data.len), .little);
    w += 4;
    @memcpy(buf[w .. w + data.len], data);
    w += data.len;
    return buf[0..w];
}

fn fmtPlain(tag: u16, channels: u16, rate: u32, bits: u16) [16]u8 {
    var b: [16]u8 = undefined;
    const block: u16 = channels * (bits / 8);
    std.mem.writeInt(u16, b[0..2], tag, .little);
    std.mem.writeInt(u16, b[2..4], channels, .little);
    std.mem.writeInt(u32, b[4..8], rate, .little);
    std.mem.writeInt(u32, b[8..12], rate * block, .little);
    std.mem.writeInt(u16, b[12..14], block, .little);
    std.mem.writeInt(u16, b[14..16], bits, .little);
    return b;
}

/// WAVE_FORMAT_EXTENSIBLE: tag 0xFFFE, cbSize 22, validBits, channelMask,
/// then a 16-byte SubFormat GUID whose first two bytes are the real tag.
fn fmtExtensible(real_tag: u16, channels: u16, rate: u32, bits: u16) [40]u8 {
    var b: [40]u8 = undefined;
    const head = fmtPlain(0xFFFE, channels, rate, bits);
    @memcpy(b[0..16], &head);
    std.mem.writeInt(u16, b[16..18], 22, .little); // cbSize
    std.mem.writeInt(u16, b[18..20], bits, .little); // valid bits
    std.mem.writeInt(u32, b[20..24], 3, .little); // channel mask L|R
    @memset(b[24..40], 0);
    std.mem.writeInt(u16, b[24..26], real_tag, .little);
    return b;
}

fn writeTmp(tmp: *const std.testing.TmpDir, name: []const u8, bytes: []const u8) !void {
    try tmp.dir.writeFile(std.testing.io, .{ .sub_path = name, .data = bytes });
}

test "open: plain float32 header" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var img: [128]u8 = undefined;
    const data = [_]u8{0} ** 32; // 4 stereo float frames
    const bytes = wavImage(&img, &fmtPlain(3, 2, 48_000, 32), &.{}, &data);
    try writeTmp(&tmp, "plain.wav", bytes);
    var pb: [64]u8 = undefined;
    var o = try open(tmpWritePath(&pb, &tmp, "plain.wav"));
    defer o.file.close(io);
    try std.testing.expectEqual(@as(u32, 48_000), o.info.rate);
    try std.testing.expectEqual(@as(u16, 2), o.info.channels);
    try std.testing.expectEqual(Subtype.float32, o.info.subtype);
    try std.testing.expectEqual(@as(u64, 4), o.info.frames);
    try std.testing.expectEqual(@as(u64, 44), o.info.data_offset);
}

test "open: EXTENSIBLE pcm24 takes the tag from the SubFormat GUID" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var img: [160]u8 = undefined;
    const data = [_]u8{0} ** 18; // 3 stereo pcm24 frames
    const bytes = wavImage(&img, &fmtExtensible(1, 2, 96_000, 24), &.{}, &data);
    try writeTmp(&tmp, "ext.wav", bytes);
    var pb: [64]u8 = undefined;
    var o = try open(tmpWritePath(&pb, &tmp, "ext.wav"));
    defer o.file.close(io);
    try std.testing.expectEqual(Subtype.pcm_24, o.info.subtype);
    try std.testing.expectEqual(@as(u64, 3), o.info.frames);
    try std.testing.expectEqual(@as(u64, 12 + 8 + 40 + 8), o.info.data_offset);
}

test "open: an odd-sized unknown chunk before data is skipped with its pad byte" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var img: [160]u8 = undefined;
    // 'junk' chunk of 3 bytes + 1 pad byte
    const junk = [_]u8{ 'j', 'u', 'n', 'k', 3, 0, 0, 0, 1, 2, 3, 0 };
    const data = [_]u8{0} ** 8; // 2 mono float frames
    const bytes = wavImage(&img, &fmtPlain(3, 1, 44_100, 32), &junk, &data);
    try writeTmp(&tmp, "junk.wav", bytes);
    var pb: [64]u8 = undefined;
    var o = try open(tmpWritePath(&pb, &tmp, "junk.wav"));
    defer o.file.close(io);
    try std.testing.expectEqual(@as(u64, 2), o.info.frames);
    try std.testing.expectEqual(@as(u64, 44 + 12), o.info.data_offset);
}

test "open: data chunk longer than the file clamps frames (a crash-truncated .part)" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var img: [128]u8 = undefined;
    const data = [_]u8{0} ** 16; // header says 16 bytes ...
    const bytes = wavImage(&img, &fmtPlain(3, 1, 8_000, 32), &.{}, &data);
    // ... but write only 44 + 10 bytes: 2 whole frames + 2 stray bytes
    try writeTmp(&tmp, "trunc.wav", bytes[0 .. 44 + 10]);
    var pb: [64]u8 = undefined;
    var o = try open(tmpWritePath(&pb, &tmp, "trunc.wav"));
    defer o.file.close(io);
    try std.testing.expectEqual(@as(u64, 2), o.info.frames);
}

test "open: not RIFF/WAVE, missing fmt, missing data" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    try writeTmp(&tmp, "not.wav", "RIFX....WAVE");
    try std.testing.expectError(error.NotWave, open(tmpWritePath(&pb, &tmp, "not.wav")));
    // fmt absent: a 'junk' chunk then data
    const no_fmt = "RIFF" ++ [_]u8{ 20, 0, 0, 0 } ++ "WAVE" ++ "data" ++ [_]u8{ 4, 0, 0, 0 } ++ [_]u8{ 0, 0, 0, 0 };
    try writeTmp(&tmp, "nofmt.wav", no_fmt);
    try std.testing.expectError(error.MissingFmt, open(tmpWritePath(&pb, &tmp, "nofmt.wav")));
    // data absent
    var img: [64]u8 = undefined;
    const f = fmtPlain(3, 1, 8_000, 32);
    const hdr_only = wavImage(&img, &f, &.{}, &.{});
    try writeTmp(&tmp, "nodata.wav", hdr_only[0 .. hdr_only.len - 8]); // drop the data chunk header
    try std.testing.expectError(error.MissingData, open(tmpWritePath(&pb, &tmp, "nodata.wav")));
}

test "open: pcm32 and float64 are Unsupported" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var img: [128]u8 = undefined;
    var pb: [64]u8 = undefined;
    const d = [_]u8{0} ** 8;
    try writeTmp(&tmp, "p32.wav", wavImage(&img, &fmtPlain(1, 1, 8_000, 32), &.{}, &d));
    try std.testing.expectError(error.Unsupported, open(tmpWritePath(&pb, &tmp, "p32.wav")));
    try writeTmp(&tmp, "f64.wav", wavImage(&img, &fmtPlain(3, 1, 8_000, 64), &.{}, &d));
    try std.testing.expectError(error.Unsupported, open(tmpWritePath(&pb, &tmp, "f64.wav")));
}

test "open: a missing file surfaces the OS error" {
    try std.testing.expectError(error.FileNotFound, open(".zig-cache/tmp/does-not-exist.wav"));
}

test "decodeSamples: pcm16 and pcm24 scale by 2^(bits-1); float32 is the raw bits" {
    var out: [2]f32 = undefined;
    decodeSamples(.pcm_16, &[_]u8{ 0xFF, 0x7F, 0x00, 0x80 }, &out); // 32767, -32768
    try std.testing.expectApproxEqAbs(@as(f32, 32767.0 / 32768.0), out[0], 1e-9);
    try std.testing.expectEqual(@as(f32, -1.0), out[1]);
    decodeSamples(.pcm_24, &[_]u8{ 0xFF, 0xFF, 0x7F, 0x00, 0x00, 0x80 }, &out); // 8388607, -8388608
    try std.testing.expectApproxEqAbs(@as(f32, 8388607.0 / 8388608.0), out[0], 1e-9);
    try std.testing.expectEqual(@as(f32, -1.0), out[1]);
    const in = [_]f32{ 0.5, -0.25 };
    decodeSamples(.float32, std.mem.sliceAsBytes(&in), &out);
    try std.testing.expectEqualSlices(f32, &in, &out);
}

test "writeFile -> readFrames is sample-exact for float32 and code-exact for pcm" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    // 3 stereo frames, values that survive pcm quantization exactly
    const in = [_]f32{ 0.0, 0.5, -0.5, 1.0, -1.0, 0.25 };
    inline for (.{ Subtype.float32, Subtype.pcm_16, Subtype.pcm_24 }) |st| {
        const path = tmpWritePath(&pb, &tmp, "rt.wav");
        try writeFile(path, &in, 48_000, 2, st);
        var o = try open(path);
        defer o.file.close(io);
        try std.testing.expectEqual(@as(u64, 3), o.info.frames);
        var out: [6]f32 = undefined;
        try readFrames(o.file, o.info, 0, &out);
        // encode then decode: q(x) = round(x * (scale)) / 2^(bits-1)
        const scale: f32 = switch (st) {
            .float32 => 1.0,
            .pcm_16 => 32767.0,
            .pcm_24 => 8388607.0,
        };
        const denom: f32 = switch (st) {
            .float32 => 1.0,
            .pcm_16 => 32768.0,
            .pcm_24 => 8388608.0,
        };
        for (in, out) |x, y| {
            const expect = if (st == .float32) x else @round(x * scale) / denom;
            try std.testing.expectApproxEqAbs(expect, y, 1e-7);
        }
    }
}

test "readFrames: a sub-span starts at start_frame" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    var in: [10]f32 = undefined; // 5 stereo frames: L = i, R = 10 + i
    for (0..5) |i| {
        in[i * 2] = @floatFromInt(i);
        in[i * 2 + 1] = @floatFromInt(10 + i);
    }
    const path = tmpWritePath(&pb, &tmp, "span.wav");
    try writeFile(path, &in, 8_000, 2, .float32);
    var o = try open(path);
    defer o.file.close(io);
    var out: [4]f32 = undefined; // frames 2 and 3
    try readFrames(o.file, o.info, 2, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 2, 12, 3, 13 }, &out);
}

test "readFrames: past the end is OutOfRange, nothing partial" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3 };
    const path = tmpWritePath(&pb, &tmp, "oor.wav");
    try writeFile(path, &in, 8_000, 1, .float32);
    var o = try open(path);
    defer o.file.close(io);
    var out: [2]f32 = undefined;
    try std.testing.expectError(error.OutOfRange, readFrames(o.file, o.info, 2, &out));
}

test "readFrames: a trailing chunk after data is not decoded as samples" {
    // W1: the OutOfRange guard's real job is a file whose bytes CONTINUE
    // after the data chunk (a DAW may append cue/smpl/LIST chunks). A
    // short read alone can't catch that case, since the file has bytes
    // past `info.frames` to read successfully and decode as junk.
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3 };
    const path = tmpWritePath(&pb, &tmp, "trailer.wav");
    try writeFile(path, &in, 8_000, 1, .float32);
    var f = try std.Io.Dir.cwd().openFile(io, path, .{ .mode = .read_write });
    const len = try f.length(io);
    const trailer = "LIST" ++ [_]u8{ 8, 0, 0, 0 } ++ [_]u8{0x7F} ** 8;
    try f.writePositionalAll(io, trailer, len);
    f.close(io);
    var o = try open(path);
    defer o.file.close(io);
    try std.testing.expectEqual(@as(u64, 3), o.info.frames); // the walk stops at `data`
    var out: [2]f32 = undefined;
    try std.testing.expectError(error.OutOfRange, readFrames(o.file, o.info, 2, &out));
}

test "readFrames spans more than one chunk-buffer iteration" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    // read_chunk_bytes / 4 = 16384 float32 mono frames per chunk; +5 forces a second, partial chunk
    const n = read_chunk_bytes / 4 + 5;
    var samples: [n]f32 = undefined;
    for (&samples, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i)) / @as(f32, n);
    const path = tmpWritePath(&pb, &tmp, "big.wav");
    try writeFile(path, &samples, 44_100, 1, .float32);
    var o = try open(path);
    defer o.file.close(io);
    var out: [n]f32 = undefined;
    try readFrames(o.file, o.info, 0, &out);
    try std.testing.expectEqualSlices(f32, &samples, &out);
}

test "readFrames: a truncated .part reads its clamped prefix" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pb: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3, 4 };
    const path = tmpWritePath(&pb, &tmp, "part.wav");
    try writeFile(path, &in, 8_000, 1, .float32);
    // chop the file to header + 2 frames + 2 stray bytes
    var f = try std.Io.Dir.cwd().openFile(io, path, .{ .mode = .read_write });
    try f.setLength(io, header_len + 8 + 2);
    f.close(io);
    var o = try open(path);
    defer o.file.close(io);
    try std.testing.expectEqual(@as(u64, 2), o.info.frames);
    var out: [2]f32 = undefined;
    try readFrames(o.file, o.info, 0, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 2 }, &out);
}

test "copyRange: a sub-span, float32 -> pcm16, reads back as the quantized slice" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    const in = [_]f32{ 0.0, 0.5, -0.5, 1.0, 0.25, -0.25 }; // 6 mono frames
    const src = tmpWritePath(&pa, &tmp, "src.wav");
    try writeFile(src, &in, 8_000, 1, .float32);
    const dst = tmpWritePath(&pd, &tmp, "dst.wav");
    try copyRange(src, dst, 1, 3, .pcm_16); // frames 1..4 = 0.5, -0.5, 1.0
    var o = try open(dst);
    defer o.file.close(io);
    try std.testing.expectEqual(Subtype.pcm_16, o.info.subtype);
    try std.testing.expectEqual(@as(u64, 3), o.info.frames);
    try std.testing.expectEqual(@as(u32, 8_000), o.info.rate);
    var out: [3]f32 = undefined;
    try readFrames(o.file, o.info, 0, &out);
    try std.testing.expectApproxEqAbs(@as(f32, 16384.0 / 32768.0), out[0], 1e-7);
    try std.testing.expectApproxEqAbs(@as(f32, -16384.0 / 32768.0), out[1], 1e-7);
    try std.testing.expectApproxEqAbs(@as(f32, 32767.0 / 32768.0), out[2], 1e-7);
}

test "copyRange: float32 -> float32 is byte-identical to writeFile of the slice" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    var pe: [64]u8 = undefined;
    var in: [40]f32 = undefined; // 20 stereo frames
    for (&in, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i)) * 0.01;
    const src = tmpWritePath(&pa, &tmp, "a.wav");
    try writeFile(src, &in, 48_000, 2, .float32);
    try copyRange(src, tmpWritePath(&pd, &tmp, "b.wav"), 5, 10, .float32);
    try writeFile(tmpWritePath(&pe, &tmp, "c.wav"), in[10..30], 48_000, 2, .float32);
    var b: [header_len + 80]u8 = undefined;
    var c: [header_len + 80]u8 = undefined;
    const gb = try tmp.dir.readFile(std.testing.io, "b.wav", &b);
    const gc = try tmp.dir.readFile(std.testing.io, "c.wav", &c);
    try std.testing.expectEqualSlices(u8, gc, gb);
}

test "copyRange: past the source end is OutOfRange and creates no file" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3 };
    const src = tmpWritePath(&pa, &tmp, "s.wav");
    try writeFile(src, &in, 8_000, 1, .float32);
    try std.testing.expectError(error.OutOfRange, copyRange(src, tmpWritePath(&pd, &tmp, "never.wav"), 2, 5, .float32));
    try std.testing.expectError(error.FileNotFound, tmp.dir.statFile(std.testing.io, "never.wav", .{}));
}

test "copyRange: a hostile start_frame is OutOfRange, not an unsigned-subtraction trap" {
    // The subtraction form (`n_frames > o.info.frames - start_frame`)
    // needs `start_frame > o.info.frames` to short-circuit first — this
    // pins that maxInt(u64) never reaches the subtraction.
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    var pa: [64]u8 = undefined;
    var pd: [64]u8 = undefined;
    const in = [_]f32{ 1, 2, 3 };
    const src = tmpWritePath(&pa, &tmp, "s2.wav");
    try writeFile(src, &in, 8_000, 1, .float32);
    try std.testing.expectError(error.OutOfRange, copyRange(src, tmpWritePath(&pd, &tmp, "never2.wav"), std.math.maxInt(u64), 1, .float32));
    try std.testing.expectError(error.FileNotFound, tmp.dir.statFile(std.testing.io, "never2.wav", .{}));
}

test "open: a huge channel count is Unsupported, not an integer-overflow trap" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    // Hand-built fmt body (not fmtPlain, which would itself overflow its
    // own u16 block-align intermediate at this channel count): tag 3
    // (float), channels 20000, bits 32. channels(u16) * bytesPerSample
    // (u8) = 20000 * 4 = 80000, which overflows u16 (max 65535) if
    // multiplied without widening first — this pins that the malformed
    // header is rejected, not a crash.
    var fmt_body: [16]u8 = undefined;
    std.mem.writeInt(u16, fmt_body[0..2], 3, .little); // tag: IEEE float
    std.mem.writeInt(u16, fmt_body[2..4], 20000, .little); // channels
    std.mem.writeInt(u32, fmt_body[4..8], 8_000, .little); // rate
    std.mem.writeInt(u32, fmt_body[8..12], 0, .little); // byte rate, unchecked
    std.mem.writeInt(u16, fmt_body[12..14], 4, .little); // block_align
    std.mem.writeInt(u16, fmt_body[14..16], 32, .little); // bits
    var img: [128]u8 = undefined;
    const data = [_]u8{0} ** 8;
    const bytes = wavImage(&img, &fmt_body, &.{}, &data);
    try writeTmp(&tmp, "hugechan.wav", bytes);
    var pb: [64]u8 = undefined;
    try std.testing.expectError(error.Unsupported, open(tmpWritePath(&pb, &tmp, "hugechan.wav")));
}

test "open: a channel count above max_channels is Unsupported, not a chunk loop that never advances" {
    var tmp = std.testing.tmpDir(.{});
    defer tmp.cleanup();
    // 20000 channels, pcm16: block_align = 20000 * 2 = 40000, which fits
    // a u16 and equals channels * bytesPerSample, so the existing
    // block_align cross-check in parseFmt does NOT reject this header —
    // only the max_channels cap does. 0 frames (empty data chunk) keeps
    // the image small; a real frame's worth of data at this channel
    // count would not fit on the stack.
    const fmt_body = fmtPlain(1, 20000, 8_000, 16);
    var img: [64]u8 = undefined;
    const bytes = wavImage(&img, &fmt_body, &.{}, &.{});
    try writeTmp(&tmp, "widechan.wav", bytes);
    var pb: [64]u8 = undefined;
    try std.testing.expectError(error.Unsupported, open(tmpWritePath(&pb, &tmp, "widechan.wav")));
}
