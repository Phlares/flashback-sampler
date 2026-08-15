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
// Ring.zig at all. `refAllDecls` forces this file's own pub decls to be
// analyzed, which is what actually pulls Ring.zig's tests into
// `zig build test`.
//
// IMPORTANT — `refAllDecls` is NOT recursive (`lib/std/testing.zig`'s
// implementation walks exactly one level of pub decls; 0.16 has no
// `refAllDeclsRecursive`). It only reaches Ring.zig's tests today because
// `Ring` is a direct pub decl of this file. The operative rule for every
// future module (Task 4's Summary, Task 5, Task 6's C ABI, ...): each new
// source file must be re-exported here as its own
// `pub const X = @import("X.zig");`, or its tests will silently not be
// compiled by `zig build test`.
test {
    std.testing.refAllDecls(@This());
}
