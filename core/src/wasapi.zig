//! Hand-written WASAPI/COM declarations. Zero external deps: no zigwin32,
//! no translate-c. A COM interface is a pointer to a struct whose first
//! field is a pointer to a vtable of function pointers; `extern struct`
//! gives C layout, `callconv(.winapi)` gives the stdcall/x64 convention
//! COM uses. Method order in each VTable is load-bearing — it IS the
//! binary interface. Reference: io/win32_process_loopback.py:181-425.
const std = @import("std");
const builtin = @import("builtin");

pub const HRESULT = i32;
pub const HANDLE = *anyopaque;
pub inline fn failed(hr: HRESULT) bool {
    return hr < 0;
}

pub const GUID = extern struct { d1: u32, d2: u16, d3: u16, d4: [8]u8 };

/// comptime "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}" → GUID.
pub fn guid(comptime s: []const u8) GUID {
    // NOTE (0.16 std/language drift): the brief wraps this body in an
    // inner `comptime { }` block. Zig 0.16.0 rejects that when guid() is
    // called from a runtime context (the tests below): "function called
    // at runtime cannot return value at comptime". The `comptime s`
    // parameter already forces compile-time evaluation everywhere the
    // brief actually needs it (the `pub const X = guid(...)` globals
    // below are container-scope consts, always comptime); dropping the
    // inner block keeps the exact same output for the exact same inputs.
    std.debug.assert(s.len == 38 and s[0] == '{' and s[37] == '}');
    const hex = std.fmt.parseInt;
    var d4: [8]u8 = undefined;
    d4[0] = hex(u8, s[20..22], 16) catch unreachable;
    d4[1] = hex(u8, s[22..24], 16) catch unreachable;
    for (0..6) |i| d4[2 + i] = hex(u8, s[25 + 2 * i .. 27 + 2 * i], 16) catch unreachable;
    return .{
        .d1 = hex(u32, s[1..9], 16) catch unreachable,
        .d2 = hex(u16, s[10..14], 16) catch unreachable,
        .d3 = hex(u16, s[15..19], 16) catch unreachable,
        .d4 = d4,
    };
}

/// NUL-terminated WTF-16 → UTF-8 into dst, truncated to fit, always NUL-terminated. Returns the bytes written (excluding NUL).
pub fn wtf16ToUtf8Z(dst: []u8, src: [*:0]const u16) []u8 {
    const wide = std.mem.span(src);
    var n: usize = 0;
    var it = std.unicode.Wtf16LeIterator.init(wide);
    while (it.nextCodepoint()) |cp| {
        var tmp: [4]u8 = undefined;
        const len = std.unicode.wtf8Encode(cp, &tmp) catch continue;
        if (n + len >= dst.len) break;
        @memcpy(dst[n .. n + len], tmp[0..len]);
        n += len;
    }
    dst[n] = 0;
    return dst[0..n];
}

test "guid parses IID_IUnknown byte-exact" {
    const g = guid("{00000000-0000-0000-C000-000000000046}");
    try std.testing.expectEqual(@as(u32, 0), g.d1);
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0xC0, 0, 0, 0, 0, 0, 0, 0x46 }, &g.d4);
}

test "guid parses IID_IAudioClient" {
    const g = guid("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}");
    try std.testing.expectEqual(@as(u32, 0x1CB9AD4C), g.d1);
    try std.testing.expectEqual(@as(u16, 0xDBFA), g.d2);
    try std.testing.expectEqual(@as(u16, 0x4C32), g.d3);
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0xB1, 0x78, 0xC2, 0xF5, 0x68, 0xA7, 0x03, 0xB2 }, &g.d4);
}

test "wtf16ToUtf8Z copies, truncates, and terminates" {
    const wide = [_:0]u16{ 'S', 'p', 'k', 0xE9 }; // "Spké"
    var dst: [8]u8 = undefined;
    const got = wtf16ToUtf8Z(&dst, &wide);
    try std.testing.expectEqualStrings("Spk\xC3\xA9", got);
    try std.testing.expectEqual(@as(u8, 0), dst[got.len]);
    var small: [3]u8 = undefined;
    const t = wtf16ToUtf8Z(&small, &wide);
    try std.testing.expectEqualStrings("Sp", t);
}

// ── ole32 / kernel32 externs ─────────────────────────────────────────
// `extern "ole32"` names the import library; build.zig links it. kernel32
// is always linked on Windows.
pub extern "ole32" fn CoInitializeEx(reserved: ?*anyopaque, coinit: u32) callconv(.winapi) HRESULT;
pub extern "ole32" fn CoUninitialize() callconv(.winapi) void;
pub extern "ole32" fn CoCreateInstance(clsid: *const GUID, outer: ?*anyopaque, ctx: u32, iid: *const GUID, out: *?*anyopaque) callconv(.winapi) HRESULT;
pub extern "ole32" fn CoTaskMemFree(p: ?*anyopaque) callconv(.winapi) void;
pub extern "ole32" fn PropVariantClear(p: *PROPVARIANT) callconv(.winapi) HRESULT;
pub extern "kernel32" fn Sleep(ms: u32) callconv(.winapi) void;
pub extern "kernel32" fn CloseHandle(h: HANDLE) callconv(.winapi) i32;

// Event-driven render: WASAPI signals this event once per engine period
// (SetEventHandle + AUDCLNT_STREAMFLAGS_EVENTCALLBACK). The render thread
// blocks in WaitForSingleObject at zero CPU until then. Auto-reset event
// (manual_reset = 0): one signal wakes one wait, no explicit ResetEvent.
pub extern "kernel32" fn CreateEventW(attrs: ?*anyopaque, manual_reset: i32, initial_state: i32, name: ?[*:0]const u16) callconv(.winapi) ?HANDLE;
pub extern "kernel32" fn WaitForSingleObject(h: HANDLE, timeout_ms: u32) callconv(.winapi) u32;
pub const WAIT_OBJECT_0: u32 = 0;
pub const WAIT_TIMEOUT: u32 = 0x102;
pub extern "kernel32" fn LoadLibraryW(name: [*:0]const u16) callconv(.winapi) ?HMODULE;
pub extern "kernel32" fn GetProcAddress(module: HMODULE, name: [*:0]const u8) callconv(.winapi) ?*const anyopaque;
pub const HMODULE = *anyopaque;

// WinRT apartment init. The capture threads use this, NOT CoInitializeEx:
// the port proved ActivateAudioInterfaceAsync (Task 9) returns
// E_ILLEGAL_METHOD_CALL (0x8000000E) under COM-only init, and RoInitialize
// is a superset of CoInitializeEx(MTA) — CoUninitialize is the paired
// teardown for both (win32_process_loopback.py:816-824). One init path
// for every capture kind. NOTE: RoInitialize takes ONE argument.
//
// Unlike ole32/kernel32, Zig 0.16's bundled Windows import libraries have
// no combase.lib (`zig build` fails: "unable to find dynamic system
// library 'combase'"), so this is resolved at runtime with
// LoadLibraryW/GetProcAddress instead of `extern "combase"` — the same
// approach win32_process_loopback.py takes with `ctypes.WinDLL("combase.dll")`
// (win32_process_loopback.py:822-824), and the pattern Task 9 reuses for
// Mmdevapi.dll's ActivateAudioInterfaceAsync. combase.dll is a core OS
// component present since Vista, so failure to resolve only surfaces as
// RoInitialize returning E_FAIL below — callers already treat any
// failed HRESULT the same way (open() propagates, enumerate() bails).
const E_FAIL: HRESULT = @bitCast(@as(u32, 0x80004005));
var combase_module: ?HMODULE = null;
var ro_initialize_fn: ?*const fn (u32) callconv(.winapi) HRESULT = null;

fn resolveRoInitialize() ?*const fn (u32) callconv(.winapi) HRESULT {
    if (ro_initialize_fn) |f| return f;
    const mod = combase_module orelse (LoadLibraryW(std.unicode.utf8ToUtf16LeStringLiteral("combase.dll")) orelse return null);
    combase_module = mod;
    const proc = GetProcAddress(mod, "RoInitialize") orelse return null;
    const f: *const fn (u32) callconv(.winapi) HRESULT = @ptrCast(proc);
    ro_initialize_fn = f;
    return f;
}

pub fn RoInitialize(init_type: u32) HRESULT {
    const f = resolveRoInitialize() orelse return E_FAIL;
    return f(init_type);
}
pub const RO_INIT_MULTITHREADED: u32 = 1;

pub const COINIT_MULTITHREADED: u32 = 0;
pub const CLSCTX_ALL: u32 = 0x17;
pub const eRender: u32 = 0;
pub const eCapture: u32 = 1;
pub const eConsole: u32 = 0;
pub const DEVICE_STATE_ACTIVE: u32 = 1;
pub const STGM_READ: u32 = 0;
pub const AUDCLNT_SHAREMODE_SHARED: u32 = 0;
pub const AUDCLNT_STREAMFLAGS_LOOPBACK: u32 = 0x00020000;
pub const AUDCLNT_STREAMFLAGS_EVENTCALLBACK: u32 = 0x00040000;
pub const AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM: u32 = 0x80000000;
pub const AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY: u32 = 0x08000000;
pub const AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY: u32 = 0x1;
pub const AUDCLNT_BUFFERFLAGS_SILENT: u32 = 0x2;
pub const WAVE_FORMAT_PCM: u16 = 1;
pub const WAVE_FORMAT_IEEE_FLOAT: u16 = 3;
pub const VT_LPWSTR: u16 = 31;
pub const REFTIME_MS: i64 = 10_000; // REFERENCE_TIME is 100 ns units

pub const CLSID_MMDeviceEnumerator = guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}");
pub const IID_IMMDeviceEnumerator = guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}");
pub const IID_IAudioClient = guid("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}");
pub const IID_IAudioCaptureClient = guid("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}");
pub const IID_IAudioRenderClient = guid("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}");
pub const IID_IUnknown = guid("{00000000-0000-0000-C000-000000000046}");

pub const PROPERTYKEY = extern struct { fmtid: GUID, pid: u32 };
pub const PKEY_Device_FriendlyName = PROPERTYKEY{ .fmtid = guid("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), .pid = 14 };

pub const PROPVARIANT = extern struct {
    vt: u16,
    r1: u16 = 0,
    r2: u16 = 0,
    r3: u16 = 0,
    // The union: we only ever read pwszVal (VT_LPWSTR) or write blob (VT_BLOB, Task 9). 16 bytes on x64.
    data: extern union { pwszVal: ?[*:0]u16, blob: extern struct { cbSize: u32, pBlobData: ?*anyopaque }, pad: [16]u8 },
};

pub const WAVEFORMATEX = extern struct {
    wFormatTag: u16,
    nChannels: u16,
    nSamplesPerSec: u32,
    nAvgBytesPerSec: u32,
    nBlockAlign: u16,
    wBitsPerSample: u16,
    cbSize: u16,
};

/// Build a plain (non-EXTENSIBLE) format. Shared-mode WASAPI accepts this
/// for ≤ 2 channels — the same shape the Python port negotiates with.
pub fn waveFormat(tag: u16, bits: u16, rate: u32, channels: u16) WAVEFORMATEX {
    const block_align: u16 = channels * bits / 8;
    return .{ .wFormatTag = tag, .nChannels = channels, .nSamplesPerSec = rate, .nAvgBytesPerSec = rate * block_align, .nBlockAlign = block_align, .wBitsPerSample = bits, .cbSize = 0 };
}

// ── COM interfaces ───────────────────────────────────────────────────
// Every interface: `vtbl: *const VTable` first, then helper methods that
// forward. `?*anyopaque` out-params mirror `void**`.

pub const IUnknownVTable = extern struct {
    QueryInterface: *const fn (*anyopaque, *const GUID, *?*anyopaque) callconv(.winapi) HRESULT,
    AddRef: *const fn (*anyopaque) callconv(.winapi) u32,
    Release: *const fn (*anyopaque) callconv(.winapi) u32,
};

pub const IMMDeviceEnumerator = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        EnumAudioEndpoints: *const fn (*IMMDeviceEnumerator, data_flow: u32, state_mask: u32, out: *?*IMMDeviceCollection) callconv(.winapi) HRESULT,
        GetDefaultAudioEndpoint: *const fn (*IMMDeviceEnumerator, data_flow: u32, role: u32, out: *?*IMMDevice) callconv(.winapi) HRESULT,
        GetDevice: *const fn (*IMMDeviceEnumerator, id: [*:0]const u16, out: *?*IMMDevice) callconv(.winapi) HRESULT,
        RegisterEndpointNotificationCallback: *const anyopaque,
        UnregisterEndpointNotificationCallback: *const anyopaque,
    };
    pub fn release(self: *IMMDeviceEnumerator) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IMMDeviceCollection = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        GetCount: *const fn (*IMMDeviceCollection, *u32) callconv(.winapi) HRESULT,
        Item: *const fn (*IMMDeviceCollection, u32, *?*IMMDevice) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IMMDeviceCollection) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IMMDevice = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        Activate: *const fn (*IMMDevice, iid: *const GUID, clsctx: u32, params: ?*PROPVARIANT, out: *?*anyopaque) callconv(.winapi) HRESULT,
        OpenPropertyStore: *const fn (*IMMDevice, stgm: u32, out: *?*IPropertyStore) callconv(.winapi) HRESULT,
        GetId: *const fn (*IMMDevice, out: *?[*:0]u16) callconv(.winapi) HRESULT,
        GetState: *const fn (*IMMDevice, *u32) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IMMDevice) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IPropertyStore = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        GetCount: *const anyopaque,
        GetAt: *const anyopaque,
        GetValue: *const fn (*IPropertyStore, key: *const PROPERTYKEY, out: *PROPVARIANT) callconv(.winapi) HRESULT,
        SetValue: *const anyopaque,
        Commit: *const anyopaque,
    };
    pub fn release(self: *IPropertyStore) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IAudioClient = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        Initialize: *const fn (*IAudioClient, share_mode: u32, flags: u32, buffer_duration: i64, periodicity: i64, fmt: *const WAVEFORMATEX, session: ?*const GUID) callconv(.winapi) HRESULT,
        GetBufferSize: *const fn (*IAudioClient, *u32) callconv(.winapi) HRESULT,
        GetStreamLatency: *const fn (*IAudioClient, *i64) callconv(.winapi) HRESULT,
        GetCurrentPadding: *const fn (*IAudioClient, *u32) callconv(.winapi) HRESULT,
        IsFormatSupported: *const fn (*IAudioClient, u32, *const WAVEFORMATEX, *?*WAVEFORMATEX) callconv(.winapi) HRESULT,
        GetMixFormat: *const fn (*IAudioClient, *?*WAVEFORMATEX) callconv(.winapi) HRESULT,
        GetDevicePeriod: *const fn (*IAudioClient, *i64, *i64) callconv(.winapi) HRESULT,
        Start: *const fn (*IAudioClient) callconv(.winapi) HRESULT,
        Stop: *const fn (*IAudioClient) callconv(.winapi) HRESULT,
        Reset: *const fn (*IAudioClient) callconv(.winapi) HRESULT,
        SetEventHandle: *const fn (*IAudioClient, HANDLE) callconv(.winapi) HRESULT,
        GetService: *const fn (*IAudioClient, *const GUID, *?*anyopaque) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IAudioClient) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IAudioCaptureClient = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        GetBuffer: *const fn (*IAudioCaptureClient, data: *?[*]u8, n_frames: *u32, flags: *u32, dev_pos: ?*u64, qpc_pos: ?*u64) callconv(.winapi) HRESULT,
        ReleaseBuffer: *const fn (*IAudioCaptureClient, u32) callconv(.winapi) HRESULT,
        GetNextPacketSize: *const fn (*IAudioCaptureClient, *u32) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IAudioCaptureClient) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IAudioRenderClient = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        /// Hands out `n_frames` frames of engine buffer to fill; ReleaseBuffer
        /// with AUDCLNT_BUFFERFLAGS_SILENT tells the engine to ignore the
        /// bytes and play silence.
        GetBuffer: *const fn (*IAudioRenderClient, n_frames: u32, data: *?[*]u8) callconv(.winapi) HRESULT,
        ReleaseBuffer: *const fn (*IAudioRenderClient, n_frames: u32, flags: u32) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IAudioRenderClient) void {
        _ = self.vtbl.base.Release(self);
    }
};

pub const IID_IActivateAudioInterfaceCompletionHandler = guid("{41D949AB-9862-444A-80F6-C261334DA5EB}");
pub const IID_IAgileObject = guid("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}");
pub const VT_BLOB: u16 = 0x41;
pub const AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK: u32 = 1;
pub const PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE: u32 = 0;
pub const VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = std.unicode.utf8ToUtf16LeStringLiteral("VAD\\Process_Loopback");

pub const AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS = extern struct { TargetProcessId: u32, ProcessLoopbackMode: u32 };
pub const AUDIOCLIENT_ACTIVATION_PARAMS = extern struct {
    ActivationType: u32,
    params: extern union { ProcessLoopbackParams: AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS },
};

pub const IActivateAudioInterfaceAsyncOperation = extern struct {
    vtbl: *const VTable,
    pub const VTable = extern struct {
        base: IUnknownVTable,
        GetActivateResult: *const fn (*IActivateAudioInterfaceAsyncOperation, *HRESULT, *?*anyopaque) callconv(.winapi) HRESULT,
    };
    pub fn release(self: *IActivateAudioInterfaceAsyncOperation) void {
        _ = self.vtbl.base.Release(self);
    }
};

/// A COM object WE implement: the completion handler. COM only needs the
/// vtable pointer first; the fields after it are ours. `done` is set from
/// COM's thread; the opener spins on it. Also claims IAgileObject so
/// ActivateAudioInterfaceAsync accepts a handler from an MTA thread.
/// `done` is a plain u32 accessed with @atomicStore/@atomicLoad —
/// std.atomic.Value is not an extern-compatible type, and this struct
/// must be extern for COM to read `vtbl` at offset 0.
pub const CompletionHandler = extern struct {
    vtbl: *const VTable = &vtable,
    refs: u32 = 1,
    done: u32 = 0,
    op: ?*IActivateAudioInterfaceAsyncOperation = null,

    pub const VTable = extern struct {
        base: IUnknownVTable,
        ActivateCompleted: *const fn (*CompletionHandler, *IActivateAudioInterfaceAsyncOperation) callconv(.winapi) HRESULT,
    };
    const vtable = VTable{
        .base = .{ .QueryInterface = qi, .AddRef = addRef, .Release = release },
        .ActivateCompleted = activateCompleted,
    };
    fn qi(this: *anyopaque, riid: *const GUID, out: *?*anyopaque) callconv(.winapi) HRESULT {
        if (std.meta.eql(riid.*, IID_IUnknown) or std.meta.eql(riid.*, IID_IActivateAudioInterfaceCompletionHandler) or std.meta.eql(riid.*, IID_IAgileObject)) {
            out.* = this;
            _ = addRef(this);
            return 0;
        }
        out.* = null;
        return @bitCast(@as(u32, 0x80004002)); // E_NOINTERFACE
    }
    fn addRef(this: *anyopaque) callconv(.winapi) u32 {
        const self: *CompletionHandler = @ptrCast(@alignCast(this));
        self.refs += 1;
        return self.refs;
    }
    fn release(this: *anyopaque) callconv(.winapi) u32 {
        const self: *CompletionHandler = @ptrCast(@alignCast(this));
        self.refs -= 1; // stack-owned by the opener; never freed here
        return self.refs;
    }
    fn activateCompleted(self: *CompletionHandler, op: *IActivateAudioInterfaceAsyncOperation) callconv(.winapi) HRESULT {
        _ = op.vtbl.base.AddRef(op);
        self.op = op;
        @atomicStore(u32, &self.done, 1, .release);
        return 0;
    }
};

pub const ActivateAudioInterfaceAsyncFn = *const fn ([*:0]const u16, *const GUID, ?*PROPVARIANT, *CompletionHandler, *?*IActivateAudioInterfaceAsyncOperation) callconv(.winapi) HRESULT;

pub extern "kernel32" fn GetCurrentProcessId() callconv(.winapi) u32;

/// Mmdevapi.dll is resolved at call time, not linked: the export exists
/// only on Windows 10 2004+, and a missing export must be a clean error,
/// not a failed process start. The module handle is deliberately never
/// freed — the function pointer must outlive this call.
///
/// Reuses the same LoadLibraryW/GetProcAddress declared above for
/// combase.dll's RoInitialize — one resolve-at-call-time mechanism for
/// every Windows-10-2004+-or-later export this file needs, not two.
pub fn activateAudioInterfaceAsync() ?ActivateAudioInterfaceAsyncFn {
    const module = LoadLibraryW(std.unicode.utf8ToUtf16LeStringLiteral("Mmdevapi.dll")) orelse return null;
    const p = GetProcAddress(module, "ActivateAudioInterfaceAsync") orelse return null;
    return @ptrCast(p);
}

// ── Toolhelp32 (process list) ────────────────────────────────────────
pub const TH32CS_SNAPPROCESS: u32 = 2;
pub const INVALID_HANDLE_VALUE: usize = std.math.maxInt(usize);
pub const PROCESSENTRY32W = extern struct {
    dwSize: u32,
    cntUsage: u32,
    th32ProcessID: u32,
    th32DefaultHeapID: usize,
    th32ModuleID: u32,
    cntThreads: u32,
    th32ParentProcessID: u32,
    pcPriClassBase: i32,
    dwFlags: u32,
    szExeFile: [260]u16,
};
pub extern "kernel32" fn CreateToolhelp32Snapshot(flags: u32, pid: u32) callconv(.winapi) ?HANDLE;
pub extern "kernel32" fn Process32FirstW(snap: HANDLE, entry: *PROCESSENTRY32W) callconv(.winapi) i32;
pub extern "kernel32" fn Process32NextW(snap: HANDLE, entry: *PROCESSENTRY32W) callconv(.winapi) i32;

test "AUDIOCLIENT_ACTIVATION_PARAMS is 12 bytes and PROCESS params sit at offset 4" {
    try std.testing.expectEqual(@as(usize, 12), @sizeOf(AUDIOCLIENT_ACTIVATION_PARAMS));
    try std.testing.expectEqual(@as(usize, 4), @offsetOf(AUDIOCLIENT_ACTIVATION_PARAMS, "params"));
}

test "PROCESSENTRY32W layout: szExeFile at 44, size 568" {
    try std.testing.expectEqual(@as(usize, 44), @offsetOf(PROCESSENTRY32W, "szExeFile"));
    try std.testing.expectEqual(@as(usize, 568), @sizeOf(PROCESSENTRY32W));
}

test "guid parses IID_IAudioRenderClient" {
    const g = IID_IAudioRenderClient;
    try std.testing.expectEqual(@as(u32, 0xF294ACFC), g.d1);
    try std.testing.expectEqual(@as(u16, 0x3146), g.d2);
    try std.testing.expectEqual(@as(u16, 0x4483), g.d3);
    try std.testing.expectEqualSlices(u8, &[_]u8{ 0xA7, 0xBF, 0xAD, 0xDC, 0xA7, 0xC2, 0x60, 0xE2 }, &g.d4);
}

test "IAudioRenderClient vtable: GetBuffer is slot 3, ReleaseBuffer slot 4 (after IUnknown's three)" {
    // Method order IS the binary interface; a swap here would call
    // ReleaseBuffer when we mean GetBuffer and corrupt the engine buffer.
    try std.testing.expectEqual(3 * @sizeOf(usize), @offsetOf(IAudioRenderClient.VTable, "GetBuffer"));
    try std.testing.expectEqual(4 * @sizeOf(usize), @offsetOf(IAudioRenderClient.VTable, "ReleaseBuffer"));
    try std.testing.expectEqual(@as(u32, 0x00040000), AUDCLNT_STREAMFLAGS_EVENTCALLBACK);
}
