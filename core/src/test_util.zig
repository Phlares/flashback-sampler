//! test_util.zig — shared test-only helpers. `tmpPath` is lifted out of
//! peaks.zig, its only prior owner, so later modules' tests (h2+) share
//! this one copy instead of growing their own private duplicates.
const std = @import("std");

/// Builds the path `tmpDir` really created: `.zig-cache/tmp/<sub_path>/
/// <name>`, relative to the project root (never a machine-specific
/// absolute path), so it's safe to hand to APIs (like `wav.writeFile`)
/// that take a path string rather than an already-open `Dir`.
pub fn tmpPath(buf: []u8, tmp: *const std.testing.TmpDir, name: []const u8) []const u8 {
    return std.fmt.bufPrint(buf, ".zig-cache/tmp/{s}/{s}", .{ tmp.sub_path, name }) catch unreachable;
}
