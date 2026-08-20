//! The audio-backend interface. `*anyopaque` + a vtable of function
//! pointers is the std idiom (std.mem.Allocator, std.Io) for "many
//! implementations, one caller": Capture never learns which backend it
//! runs on, so WasapiBackend and FakeBackend are interchangeable, and a
//! CoreAudio/ALSA backend later is one more file, not a Capture change.
const std = @import("std");

pub const Kind = enum(u8) { loopback = 0, input = 1, process = 2 };

pub const Error = error{ DeviceNotFound, FormatRejected, ActivationFailed, Unsupported, OutOfMemory };

/// extern so the ABI passes it through unchanged (Task 6). UTF-8, NUL-terminated, truncated to fit.
pub const Device = extern struct { kind: u8, is_default: u8, mix_rate: u32, mix_channels: u16, id: [128]u8, name: [128]u8 };

pub const Spec = struct { kind: Kind, device_id: []const u8, pid: u32 = 0, rate: u32, channels: u16 };

pub const Packet = struct { frames: []const f32, discontinuity: bool = false };

pub const Stream = struct {
    ptr: *anyopaque,
    vtable: *const VTable,

    pub const VTable = struct {
        /// Blocks up to timeout_ms. null = nothing arrived. Frames are valid until the next call.
        next: *const fn (*anyopaque, timeout_ms: u32) Error!?Packet,
        /// Idempotent. Unblocks a concurrent next(). Called from the control thread.
        stop: *const fn (*anyopaque) void,
        deinit: *const fn (*anyopaque) void,
        mixRate: *const fn (*anyopaque) u32,
    };

    pub fn next(s: Stream, timeout_ms: u32) Error!?Packet {
        return s.vtable.next(s.ptr, timeout_ms);
    }
    pub fn stop(s: Stream) void {
        return s.vtable.stop(s.ptr);
    }
    pub fn deinit(s: Stream) void {
        return s.vtable.deinit(s.ptr);
    }
    pub fn mixRate(s: Stream) u32 {
        return s.vtable.mixRate(s.ptr);
    }
};

pub const Backend = struct {
    ptr: *anyopaque,
    vtable: *const VTable,

    pub const VTable = struct {
        /// Fills `out`, returns count. Never fails; an empty machine returns 0.
        enumerate: *const fn (*anyopaque, out: []Device) usize,
        /// Opens AND starts the stream. Called on the capture thread.
        open: *const fn (*anyopaque, Spec) Error!Stream,
    };

    pub fn enumerate(b: Backend, out: []Device) usize {
        return b.vtable.enumerate(b.ptr, out);
    }
    pub fn open(b: Backend, spec: Spec) Error!Stream {
        return b.vtable.open(b.ptr, spec);
    }
};
