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
