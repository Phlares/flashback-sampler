const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const mod = b.addModule("flashback_core", .{
        .root_source_file = b.path("src/root.zig"),
        .target = target,
        .optimize = optimize,
    });

    // The WASAPI backend calls ole32 (CoCreateInstance & co). Zig ships an
    // import library for it, so this links without an SDK. combase
    // (RoInitialize) has no bundled import library, so wasapi.zig resolves
    // it at runtime via LoadLibraryW/GetProcAddress instead — see the
    // comment on RoInitialize there. Only meaningful for Windows targets;
    // harmless elsewhere because wasapi.zig is not even analyzed there
    // (see root.zig).
    if (target.result.os.tag == .windows) {
        mod.linkSystemLibrary("ole32", .{});
    }

    // Shared library: the ctypes host loads this. `linkage = .dynamic`
    // is what makes it a .dll/.so/.dylib instead of a static archive.
    const lib = b.addLibrary(.{
        .name = "flashback_core",
        .root_module = mod,
        .linkage = .dynamic,
    });
    b.installArtifact(lib);

    const tests = b.addTest(.{ .root_module = mod });
    const run_tests = b.addRunArtifact(tests);
    const test_step = b.step("test", "Run unit tests");
    test_step.dependOn(&run_tests.step);
}
