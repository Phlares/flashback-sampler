//! flashback_core — lock-free audio ring engine.
//! Library root: everything public is re-exported here.
const std = @import("std");

// Re-exported here so `zig build test` (which only compiles tests reachable
// from this root module) actually pulls in Ring.zig's test blocks.
pub const Ring = @import("Ring.zig");
pub const Summary = @import("Summary.zig");
pub const wav = @import("wav.zig");
pub const convert = @import("convert.zig");
pub const peaks = @import("peaks.zig");
pub const abi = @import("abi.zig");
pub const Backend = @import("Backend.zig");
pub const FakeBackend = @import("FakeBackend.zig");
pub const ErrorSlot = @import("ErrorSlot.zig");
pub const Capture = @import("Capture.zig");
pub const Playback = @import("Playback.zig");
pub const Mixer = @import("Mixer.zig");

// OS-gated: these two files only compile for Windows targets. On other
// targets `wasapi`/`WasapiBackend` are empty structs and abi.zig's
// capture exports return null/0. builtin.os.tag is a comptime constant, so the
// dead branch is never analyzed on macOS/Linux — that is what keeps the
// cross-compile legs green.
const builtin = @import("builtin");
pub const wasapi = if (builtin.os.tag == .windows) @import("wasapi.zig") else struct {};
pub const WasapiBackend = if (builtin.os.tag == .windows) @import("WasapiBackend.zig") else struct {};

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
// `refAllDeclsRecursive`). It reaches Ring.zig's, Summary.zig's, and
// wav.zig's tests only because each is a direct pub decl of this file.
// The operative rule for every future module (Task 6's C ABI, ...): each
// new source file must be re-exported here as its own
// `pub const X = @import("X.zig");`, or its tests will silently not be
// compiled by `zig build test`.
test {
    std.testing.refAllDecls(@This());
}

// `refAllDecls` above (see the note it carries) reaches abi.zig's own
// test blocks because `abi` is a direct pub decl of THIS file — but it
// does nothing for abi.zig's `export fn` symbols themselves. Those are
// only emitted into the shared library if the compiler's Sema actually
// walks past their declarations, which happens as a side effect of
// resolving `abi`'s pub decls here. Force-reference it explicitly so
// this stays true even if refAllDecls's reach ever changes.
comptime {
    _ = abi;
}
