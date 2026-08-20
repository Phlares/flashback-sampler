//! Pure sample-format conversion for one capture packet. No OS calls,
//! no allocation — testable everywhere, used by WasapiBackend on Windows.
const std = @import("std");

pub const SampleTag = enum(u8) { f32 = 0, i16 = 1 };
pub const SourceFormat = struct { tag: SampleTag, channels: u16 };

test "f32 stereo → stereo is a straight copy" {
    const src = [_]f32{ 0.1, -0.1, 0.2, -0.2 };
    var out: [4]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 2, .{ .tag = .f32, .channels = 2 }, false, 2, &out);
    try std.testing.expectEqualSlices(f32, &src, got);
}

test "i16 scales by 1/32768" {
    const src = [_]i16{ 32767, -32768, 0, 16384 };
    var out: [4]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 2, .{ .tag = .i16, .channels = 2 }, false, 2, &out);
    try std.testing.expectApproxEqAbs(@as(f32, 32767.0 / 32768.0), got[0], 1e-6);
    try std.testing.expectApproxEqAbs(@as(f32, -1.0), got[1], 1e-6);
    try std.testing.expectApproxEqAbs(@as(f32, 0.0), got[2], 1e-6);
    try std.testing.expectApproxEqAbs(@as(f32, 0.5), got[3], 1e-6);
}

test "silent flag zero-fills regardless of source bytes" {
    const src = [_]f32{ 0.9, 0.9 };
    var out: [2]f32 = .{ 7, 7 };
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 1, .{ .tag = .f32, .channels = 2 }, true, 2, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0, 0 }, got);
}

test "stereo → mono averages" {
    const src = [_]f32{ 0.5, -0.5, 1.0, 0.0 };
    var out: [2]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 2, .{ .tag = .f32, .channels = 2 }, false, 1, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0.0, 0.5 }, got);
}

test "mono → stereo duplicates" {
    const src = [_]f32{ 0.25, -0.75 };
    var out: [4]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 2, .{ .tag = .f32, .channels = 1 }, false, 2, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 0.25, 0.25, -0.75, -0.75 }, got);
}

test "6 → 2 keeps the first two channels" {
    const src = [_]f32{ 1, 2, 3, 4, 5, 6 };
    var out: [2]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 1, .{ .tag = .f32, .channels = 6 }, false, 2, &out);
    try std.testing.expectEqualSlices(f32, &[_]f32{ 1, 2 }, got);
}

test "returned slice length is n_frames * dst_channels" {
    const src = [_]f32{ 0, 0, 0, 0 };
    var out: [8]f32 = undefined;
    const got = packet(std.mem.sliceAsBytes(&src).ptr, 2, .{ .tag = .f32, .channels = 2 }, false, 2, &out);
    try std.testing.expectEqual(@as(usize, 4), got.len);
}

pub fn packet(src: [*]const u8, n_frames: usize, fmt: SourceFormat, silent: bool, dst_channels: u16, out: []f32) []f32 {
    const dst_len = n_frames * dst_channels;
    std.debug.assert(out.len >= dst_len);
    const dst = out[0..dst_len];
    if (silent) {
        @memset(dst, 0);
        return dst;
    }
    const sc: usize = fmt.channels;
    const dc: usize = dst_channels;
    var f: usize = 0;
    while (f < n_frames) : (f += 1) {
        // One frame at a time; sample() hides the i16/f32 difference so the
        // channel rule below is written once.
        if (sc == dc) {
            for (0..dc) |c| dst[f * dc + c] = sample(src, fmt.tag, f * sc + c);
        } else if (sc == 2 and dc == 1) {
            dst[f] = 0.5 * (sample(src, fmt.tag, f * 2) + sample(src, fmt.tag, f * 2 + 1));
        } else if (sc == 1 and dc == 2) {
            const s = sample(src, fmt.tag, f);
            dst[f * 2] = s;
            dst[f * 2 + 1] = s;
        } else {
            const keep = @min(sc, dc);
            for (0..keep) |c| dst[f * dc + c] = sample(src, fmt.tag, f * sc + c);
            for (keep..dc) |c| dst[f * dc + c] = 0;
        }
    }
    return dst;
}

inline fn sample(src: [*]const u8, tag: SampleTag, index: usize) f32 {
    return switch (tag) {
        // The packet is a byte pointer from WASAPI; reinterpret per sample.
        // `[*]align(1) const T` — WASAPI does not promise 4-byte alignment.
        .f32 => @as([*]align(1) const f32, @ptrCast(src))[index],
        .i16 => @as(f32, @floatFromInt(@as([*]align(1) const i16, @ptrCast(src))[index])) / 32768.0,
    };
}
