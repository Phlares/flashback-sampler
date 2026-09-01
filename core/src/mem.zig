//! Physical memory query for the host's footprint check (#41). One
//! call answers both "how much RAM is there" (the 25 % default footprint)
//! and "how much is free right now" (refuse a ring the OS could not
//! commit). A field is 0 when the platform cannot say; the host skips
//! that clause. Allocation-free, no state, safe from any thread.
const std = @import("std");
const builtin = @import("builtin");

/// Mirrors FbMemInfo in flashback_core.h.
pub const Info = extern struct { total: u64, available: u64 };

pub fn query() Info {
    return switch (builtin.os.tag) {
        .windows => queryWindows(),
        .linux => queryLinux(),
        else => .{ .total = std.process.totalSystemMemory() catch 0, .available = 0 },
    };
}

// ── Windows ─────────────────────────────────────────────────────────
// GlobalMemoryStatusEx fills one struct with totals and free counts.
// `dwLength` must hold the struct size on entry (the Win32 versioning
// idiom); extern struct keeps the C layout, so @sizeOf is that size.
const MemoryStatusEx = extern struct {
    dwLength: u32,
    dwMemoryLoad: u32,
    ullTotalPhys: u64,
    ullAvailPhys: u64,
    ullTotalPageFile: u64,
    ullAvailPageFile: u64,
    ullTotalVirtual: u64,
    ullAvailVirtual: u64,
    ullAvailExtendedVirtual: u64,
};

// Declared here rather than imported from wasapi.zig: memory is not an
// audio concern, and this file must compile on every target (the extern
// is only referenced inside the Windows branch, so other targets never
// link it).
extern "kernel32" fn GlobalMemoryStatusEx(buf: *MemoryStatusEx) callconv(.winapi) i32;

fn queryWindows() Info {
    var s: MemoryStatusEx = undefined;
    s.dwLength = @sizeOf(MemoryStatusEx);
    if (GlobalMemoryStatusEx(&s) == 0) return .{ .total = 0, .available = 0 };
    return .{ .total = s.ullTotalPhys, .available = s.ullAvailPhys };
}

// ── Linux ───────────────────────────────────────────────────────────
// sysinfo(2) is one syscall, no /proc parse. freeram + bufferram is the
// kernel's own "free" plus reclaimable buffers — the same shape `free`
// prints, short of the MemAvailable heuristic (which needs /proc).
fn queryLinux() Info {
    var info: std.os.linux.Sysinfo = undefined;
    if (std.os.linux.errno(std.os.linux.sysinfo(&info)) != .SUCCESS) return .{ .total = 0, .available = 0 };
    const unit: u64 = info.mem_unit;
    return .{
        .total = @as(u64, info.totalram) * unit,
        .available = (@as(u64, info.freeram) + @as(u64, info.bufferram)) * unit,
    };
}

test "query reports a total and an available count that fits inside it" {
    const m = query();
    try std.testing.expect(m.available <= m.total);
    // The two platforms this file queries directly must report both;
    // elsewhere total comes from std and 0 (unknown) is a legal answer.
    if (builtin.os.tag == .windows or builtin.os.tag == .linux) {
        try std.testing.expect(m.total > 0);
        try std.testing.expect(m.available > 0);
    }
}

test "MEMORYSTATUSEX is 64 bytes, the size Win32 versions the call on" {
    try std.testing.expectEqual(@as(usize, 64), @sizeOf(MemoryStatusEx));
}

test "Info layout matches native.py's FbMemInfo ctypes struct" {
    try std.testing.expectEqual(@as(usize, 16), @sizeOf(Info));
    try std.testing.expectEqual(@as(usize, 8), @offsetOf(Info, "available"));
}
