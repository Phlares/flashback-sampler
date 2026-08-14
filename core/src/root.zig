//! flashback_core — lock-free audio ring engine.
//! Library root: everything public is re-exported here.
const std = @import("std");

test "scaffold: the test runner runs" {
    try std.testing.expect(smoke() == 42);
}

fn smoke() u8 {
    return 42;
}
