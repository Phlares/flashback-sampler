//! flashback_core — lock-free audio ring engine.
//! Library root: everything public is re-exported here.
const std = @import("std");

// Re-exported here so `zig build test` (which only compiles tests reachable
// from this root module) actually pulls in Ring.zig's test blocks.
pub const Ring = @import("Ring.zig");

// A `pub const` import alone is not enough: Zig's lazy Sema only analyzes
// declarations that are actually used, so an unreferenced `Ring` re-export
// would leave its `test` blocks (and any compile errors inside them)
// undiscovered — a build could report green having never looked at
// Ring.zig at all. `refAllDecls` forces every pub decl reachable from here,
// recursively, to be analyzed, which is what actually pulls Ring.zig's
// tests into `zig build test`.
test {
    std.testing.refAllDecls(@This());
}

test "scaffold: the test runner runs" {
    try std.testing.expect(smoke() == 42);
}

fn smoke() u8 {
    return 42;
}
