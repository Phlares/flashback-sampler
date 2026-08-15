//! Minimal RIFF/WAVE writer. FLOAT32 payload is the ring's bytes
//! verbatim — a bit-perfect pull. 44-byte canonical header; libsndfile
//! and every DAW read it. Parity vs soundfile is DECODE-equality
//! (samples + format), not byte-equality (libsndfile adds PEAK/fact
//! chunks we deliberately don't).
const std = @import("std");
const builtin = @import("builtin");

comptime {
    // FLOAT32's memcpy path writes host-endian bits as file bytes.
    // Every supported target is little-endian; make that loud, not lucky.
    std.debug.assert(builtin.target.cpu.arch.endian() == .little);
}

test "golden 44-byte header: 48k stereo float32, 4 frames" {
    var h: [44]u8 = undefined;
    writeHeader(&h, 48_000, 2, .float32, 4);
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

/// The sample formats this writer supports. Backed by `u8` so the wire
/// value (used by Task 6's C ABI: 0/1/2) is stable across Zig versions.
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

/// Write a canonical 44-byte RIFF/WAVE header for `n_frames` frames of
/// `channels`-channel audio at `rate` Hz in subtype `st`. A "frame" is
/// one sample per channel, so the data chunk size is
/// `n_frames * channels * bytesPerSample`.
pub fn writeHeader(out: *[header_len]u8, rate: u32, channels: u16, st: Subtype, n_frames: u64) void {
    const bps: u32 = st.bytesPerSample();
    const block_align: u16 = @intCast(bps * channels);
    const data_len: u32 = @intCast(n_frames * block_align);
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
/// asymmetry is the documented contract Task 7 checks against
/// soundfile, not a bug.
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
                const v: i16 = @intFromFloat(std.math.clamp(@round(clamped * 32767.0), -32768.0, 32767.0));
                std.mem.writeInt(i16, out[i * 2 ..][0..2], v, .little);
            }
            return samples.len * 2;
        },
        .pcm_24 => {
            for (samples, 0..) |s, i| {
                const clamped = std.math.clamp(s, -1.0, 1.0);
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
pub fn writeFile(path: []const u8, samples: []const f32, rate: u32, channels: u16, st: Subtype) !void {
    const io = std.Io.Threaded.global_single_threaded.io();
    var file = try std.Io.Dir.cwd().createFile(io, path, .{});
    defer file.close(io);
    var header: [header_len]u8 = undefined;
    writeHeader(&header, rate, channels, st, samples.len / channels);
    try file.writeStreamingAll(io, &header);
    var buf: [16384 * 4]u8 = undefined;
    var remaining = samples;
    while (remaining.len > 0) {
        const take = @min(remaining.len, 16384);
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

test "pcm24 writes 3 little-endian bytes per sample" {
    const in = [_]f32{ 1.0, -1.0 };
    var out: [6]u8 = undefined;
    _ = encodeSamples(.pcm_24, &in, &out);
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0xFF, 0xFF, 0x7F }, out[0..3]); // 8388607
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0x01, 0x00, 0x80 }, out[3..6]); // -8388607
}

// Scratchpad path, not a repo-relative one: this test writes a real file
// and the brief's bare relative filename would litter core/ if `zig
// build test` is ever invoked from a different cwd (or the deferred
// cleanup fails). The scratchpad is session-local and already outside
// version control.
const roundtrip_test_path = "C:/Users/user/AppData/Local/Temp/claude/C--Users-user-Documents-dev/3b2cf763-0187-47aa-9da3-7c089ed81d99/scratchpad/zig-wav-roundtrip-test.wav";

test "writeFile round-trips float32 through a real file" {
    defer std.Io.Dir.cwd().deleteFile(std.testing.io, roundtrip_test_path) catch {};
    const in = [_]f32{ 0.1, -0.2, 0.3, -0.4 }; // 2 stereo frames
    try writeFile(roundtrip_test_path, &in, 48_000, 2, .float32);
    var buf: [44 + 16]u8 = undefined;
    const got = try std.Io.Dir.cwd().readFile(std.testing.io, roundtrip_test_path, &buf);
    try std.testing.expectEqual(@as(usize, 60), got.len);
    try std.testing.expectEqualSlices(u8, std.mem.sliceAsBytes(&in), got[44..]);
}

test "writeFile with zero samples writes a header-only file" {
    const path = "C:/Users/user/AppData/Local/Temp/claude/C--Users-user-Documents-dev/3b2cf763-0187-47aa-9da3-7c089ed81d99/scratchpad/zig-wav-empty-test.wav";
    defer std.Io.Dir.cwd().deleteFile(std.testing.io, path) catch {};
    const in = [_]f32{};
    try writeFile(path, &in, 48_000, 2, .pcm_16);
    var buf: [header_len]u8 = undefined;
    const got = try std.Io.Dir.cwd().readFile(std.testing.io, path, &buf);
    try std.testing.expectEqual(@as(usize, header_len), got.len);
    try std.testing.expectEqual(@as(u32, 36), std.mem.readInt(u32, got[4..8], .little)); // 36 + 0 data bytes
    try std.testing.expectEqual(@as(u32, 0), std.mem.readInt(u32, got[40..44], .little)); // data chunk size 0
}

test "writeFile spans more than one chunk-buffer iteration" {
    const path = "C:/Users/user/AppData/Local/Temp/claude/C--Users-user-Documents-dev/3b2cf763-0187-47aa-9da3-7c089ed81d99/scratchpad/zig-wav-chunked-test.wav";
    defer std.Io.Dir.cwd().deleteFile(std.testing.io, path) catch {};
    // writeFile's fixed buffer caps a chunk at 16384 samples; +5 forces
    // a second, partial chunk through the `while` loop — the round-trip
    // test above only ever exercises a single chunk.
    const n = 16384 + 5;
    var samples: [n]f32 = undefined;
    for (&samples, 0..) |*s, i| s.* = @as(f32, @floatFromInt(i)) / @as(f32, n);
    try writeFile(path, &samples, 44_100, 1, .float32);
    var buf: [header_len + n * 4]u8 = undefined;
    const got = try std.Io.Dir.cwd().readFile(std.testing.io, path, &buf);
    try std.testing.expectEqual(@as(usize, header_len + n * 4), got.len);
    try std.testing.expectEqualSlices(u8, std.mem.sliceAsBytes(&samples), got[header_len..]);
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
