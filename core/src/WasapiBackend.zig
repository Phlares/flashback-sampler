//! Backend over WASAPI shared-mode capture. One code path for loopback
//! (an eRender endpoint opened with the LOOPBACK flag), mic/line-in (an
//! eCapture endpoint) and — Task 9 — per-process loopback. Polling, not
//! event-driven: event-driven loopback has a known WASAPI quirk (events
//! stop unless a render stream is also active), and a 10 ms poll is
//! nothing next to a 200 ms WASAPI buffer. One loop for every kind.
//! Render (PR e) is event-driven: the loopback quirk does not apply to a
//! real output stream.
const std = @import("std");
const builtin = @import("builtin");
const w = @import("wasapi.zig");
const Backend = @import("Backend.zig");
const convert = @import("convert.zig");
const WasapiBackend = @This();

pub var instance: WasapiBackend = .{};

pub fn backend() Backend.Backend {
    return .{ .ptr = &instance, .vtable = &backend_vtable };
}

const backend_vtable = Backend.Backend.VTable{ .enumerate = enumerate, .open = open, .openRender = openRender };
const stream_vtable = Backend.Stream.VTable{ .next = next, .stop = stop, .deinit = deinit, .mixRate = mixRate };

const poll_ms: u32 = 10;
const buffer_duration_ms: i64 = 200;
const max_streams = 16;

/// Format candidates in preference order. First = "what we asked for";
/// AUTOCONVERTPCM makes the engine convert to it. The rest are the
/// Python port's fallbacks for the process-loopback client, which has
/// no device attached so GetMixFormat is not meaningful there — the
/// port never queries it and tries this chain instead
/// (win32_process_loopback.py:955-975).
pub fn candidates(rate: u32, channels: u16) [5]w.WAVEFORMATEX {
    return .{
        w.waveFormat(w.WAVE_FORMAT_IEEE_FLOAT, 32, rate, channels),
        w.waveFormat(w.WAVE_FORMAT_IEEE_FLOAT, 32, 48_000, 2),
        w.waveFormat(w.WAVE_FORMAT_IEEE_FLOAT, 32, 44_100, 2),
        w.waveFormat(w.WAVE_FORMAT_PCM, 16, 44_100, 2),
        w.waveFormat(w.WAVE_FORMAT_PCM, 16, 48_000, 2),
    };
}

/// One open stream. Fixed pool, no allocator: the RT rule reaches the
/// backend too. `scratch` holds one converted packet — sized from
/// GetBufferSize (the largest packet WASAPI can hand us) × dst channels.
const Stream = struct {
    in_use: bool = false,
    client: ?*w.IAudioClient = null,
    capture: ?*w.IAudioCaptureClient = null,
    src_fmt: convert.SourceFormat = .{ .tag = .f32, .channels = 2 },
    dst_channels: u16 = 2,
    mix_rate: u32 = 0,
    stopped: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
    scratch: [scratch_len]f32 = undefined,
    // 200 ms at 192 kHz stereo = 76 800 floats; round up. If GetBufferSize
    // reports more than this, open() fails with Unsupported rather than
    // ever overrunning.
    const scratch_len = 96 * 1024;
};

/// One open render stream. Same fixed-pool rule as `Stream`: no allocator
/// on the audio path. The engine owns the sample buffer (GetBuffer hands
/// us a pointer into it), so no scratch is needed here.
const Render = struct {
    // Render is the first pool with concurrent openers: each Playback's
    // render thread calls openRender independently, so two decks starting
    // together can race to claim the same slot. in_use must be atomic and
    // claimed with a compare-and-swap, not a plain read-then-write.
    in_use: std.atomic.Value(bool) = std.atomic.Value(bool).init(false),
    client: ?*w.IAudioClient = null,
    render: ?*w.IAudioRenderClient = null,
    event: ?w.HANDLE = null,
    buffer_frames: u32 = 0,
    channels: u16 = 2,
    mix_rate: u32 = 0,
};

const max_renders = 4;

// Both stream pools live here, side by side: 0.16 disallows a declaration
// between struct fields, so every const/fn above stays before this line.
streams: [max_streams]Stream = [_]Stream{.{}} ** max_streams,
renders: [max_renders]Render = [_]Render{.{}} ** max_renders,

const render_vtable = Backend.RenderStream.VTable{ .wait = renderWait, .available = renderAvailable, .write = renderWrite, .stop = renderStop, .deinit = renderDeinit, .mixRate = renderMixRate };

/// The clip's own format. The engine resamples to its mix rate under
/// AUTOCONVERTPCM | SRC_DEFAULT_QUALITY — the same borrowed resampler
/// capture uses in the other direction (open(), above).
pub fn renderFormat(rate: u32, channels: u16) w.WAVEFORMATEX {
    return w.waveFormat(w.WAVE_FORMAT_IEEE_FLOAT, 32, rate, channels);
}

fn acquireRender(self: *WasapiBackend) ?*Render {
    for (&self.renders) |*r| {
        // cmpxchgStrong returns null on success (the old value it swapped
        // out matched `false`) — that is the "I won the claim" case.
        if (r.in_use.cmpxchgStrong(false, true, .acq_rel, .acquire) == null) {
            return r;
        }
    }
    return null;
}

fn enumerate(ptr: *anyopaque, out: []Backend.Device) usize {
    _ = ptr;
    // Called on the host's thread (Qt's main thread is STA): CoInitializeEx
    // then returns RPC_E_CHANGED_MODE, COM stays usable, and we must NOT
    // pair it with CoUninitialize. Only balance a successful init.
    const hr_init = w.CoInitializeEx(null, w.COINIT_MULTITHREADED);
    defer if (!w.failed(hr_init)) w.CoUninitialize();
    var enumr: ?*anyopaque = null;
    if (w.failed(w.CoCreateInstance(&w.CLSID_MMDeviceEnumerator, null, w.CLSCTX_ALL, &w.IID_IMMDeviceEnumerator, &enumr))) return 0;
    const en: *w.IMMDeviceEnumerator = @ptrCast(@alignCast(enumr.?));
    defer en.release();
    var n: usize = 0;
    // Loopback devices are the RENDER endpoints; inputs are the CAPTURE endpoints.
    n += listFlow(en, w.eRender, .loopback, out[n..]);
    n += listFlow(en, w.eCapture, .input, out[n..]);
    // The same render endpoints again, as playback outputs. One endpoint,
    // two roles; two rows keeps the Python filters one-liners.
    n += listFlow(en, w.eRender, .render, out[n..]);
    return n;
}

fn listFlow(en: *w.IMMDeviceEnumerator, flow: u32, kind: Backend.Kind, out: []Backend.Device) usize {
    var coll: ?*w.IMMDeviceCollection = null;
    if (w.failed(en.vtbl.EnumAudioEndpoints(en, flow, w.DEVICE_STATE_ACTIVE, &coll))) return 0;
    defer coll.?.release();
    var default_id: [128]u8 = undefined;
    var default_len: usize = 0;
    var def: ?*w.IMMDevice = null;
    if (!w.failed(en.vtbl.GetDefaultAudioEndpoint(en, flow, w.eConsole, &def))) {
        defer def.?.release();
        default_len = deviceId(def.?, &default_id).len;
    }
    var count: u32 = 0;
    _ = coll.?.vtbl.GetCount(coll.?, &count);
    var n: usize = 0;
    var i: u32 = 0;
    while (i < count and n < out.len) : (i += 1) {
        var dev: ?*w.IMMDevice = null;
        if (w.failed(coll.?.vtbl.Item(coll.?, i, &dev))) continue;
        defer dev.?.release();
        var d = std.mem.zeroes(Backend.Device);
        d.kind = @intFromEnum(kind);
        const id = deviceId(dev.?, &d.id);
        d.is_default = @intFromBool(std.mem.eql(u8, id, default_id[0..default_len]));
        _ = friendlyName(dev.?, &d.name);
        mixFormat(dev.?, &d.mix_rate, &d.mix_channels);
        out[n] = d;
        n += 1;
    }
    return n;
}

fn deviceId(dev: *w.IMMDevice, dst: []u8) []u8 {
    var wide: ?[*:0]u16 = null;
    if (w.failed(dev.vtbl.GetId(dev, &wide))) {
        dst[0] = 0;
        return dst[0..0];
    }
    defer w.CoTaskMemFree(wide);
    return w.wtf16ToUtf8Z(dst, wide.?);
}

fn friendlyName(dev: *w.IMMDevice, dst: []u8) []u8 {
    var store: ?*w.IPropertyStore = null;
    if (w.failed(dev.vtbl.OpenPropertyStore(dev, w.STGM_READ, &store))) {
        dst[0] = 0;
        return dst[0..0];
    }
    defer store.?.release();
    var pv: w.PROPVARIANT = .{ .vt = 0, .data = .{ .pad = [_]u8{0} ** 16 } };
    if (w.failed(store.?.vtbl.GetValue(store.?, &w.PKEY_Device_FriendlyName, &pv)) or pv.vt != w.VT_LPWSTR or pv.data.pwszVal == null) {
        dst[0] = 0;
        return dst[0..0];
    }
    defer _ = w.PropVariantClear(&pv);
    return w.wtf16ToUtf8Z(dst, pv.data.pwszVal.?);
}

/// Mix format = what the engine runs this endpoint at. Rate 0 = unknown.
fn mixFormat(dev: *w.IMMDevice, rate: *u32, channels: *u16) void {
    var raw: ?*anyopaque = null;
    if (w.failed(dev.vtbl.Activate(dev, &w.IID_IAudioClient, w.CLSCTX_ALL, null, &raw))) return;
    const client: *w.IAudioClient = @ptrCast(@alignCast(raw.?));
    defer client.release();
    var fmt: ?*w.WAVEFORMATEX = null;
    if (w.failed(client.vtbl.GetMixFormat(client, &fmt))) return;
    defer w.CoTaskMemFree(fmt);
    rate.* = fmt.?.nSamplesPerSec;
    channels.* = fmt.?.nChannels;
}

fn open(ptr: *anyopaque, spec: Backend.Spec) Backend.Error!Backend.Stream {
    const self: *WasapiBackend = @ptrCast(@alignCast(ptr));
    if (spec.channels == 0 or spec.channels > 2) return error.Unsupported;
    // Called on the capture thread, which stays alive for the stream's
    // life — so the init here pairs with CoUninitialize in deinit.
    // RoInitialize, not CoInitializeEx: see the declaration in wasapi.zig
    // — process activation (Task 9) hard-requires the WinRT apartment,
    // and it is a superset of COM MTA for the other kinds.
    _ = w.RoInitialize(w.RO_INIT_MULTITHREADED);
    errdefer w.CoUninitialize();
    const slot = self.acquireSlot() orelse return error.OutOfMemory;
    errdefer slot.in_use = false;
    const client = try activate(spec);
    errdefer client.release();
    // Mix rate first: on a real endpoint this works; on the process
    // client (Task 9) it returns E_NOTIMPL and we report 0.
    var mix: ?*w.WAVEFORMATEX = null;
    if (!w.failed(client.vtbl.GetMixFormat(client, &mix))) {
        slot.mix_rate = mix.?.nSamplesPerSec;
        w.CoTaskMemFree(mix);
    } else slot.mix_rate = 0;
    var flags: u32 = w.AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | w.AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
    if (spec.kind != .input) flags |= w.AUDCLNT_STREAMFLAGS_LOOPBACK;
    const cands = candidates(spec.rate, spec.channels);
    var chosen: ?w.WAVEFORMATEX = null;
    for (cands) |c| {
        if (!w.failed(client.vtbl.Initialize(client, w.AUDCLNT_SHAREMODE_SHARED, flags, buffer_duration_ms * w.REFTIME_MS, 0, &c, null))) {
            chosen = c;
            break;
        }
    }
    const fmt = chosen orelse return error.FormatRejected;
    var buf_frames: u32 = 0;
    _ = client.vtbl.GetBufferSize(client, &buf_frames);
    if (@as(usize, buf_frames) * spec.channels > Stream.scratch_len) return error.Unsupported;
    var raw: ?*anyopaque = null;
    if (w.failed(client.vtbl.GetService(client, &w.IID_IAudioCaptureClient, &raw))) return error.ActivationFailed;
    const cap: *w.IAudioCaptureClient = @ptrCast(@alignCast(raw.?));
    errdefer cap.release();
    if (w.failed(client.vtbl.Start(client))) return error.ActivationFailed;
    slot.* = .{
        .in_use = true,
        .client = client,
        .capture = cap,
        .src_fmt = .{ .tag = if (fmt.wFormatTag == w.WAVE_FORMAT_PCM) .i16 else .f32, .channels = fmt.nChannels },
        .dst_channels = spec.channels,
        .mix_rate = slot.mix_rate,
        .scratch = undefined,
    };
    return .{ .ptr = slot, .vtable = &stream_vtable };
}

/// Called on the render thread, which lives for the stream's life: the
/// RoInitialize here pairs with CoUninitialize in renderDeinit, exactly
/// as open()/deinit() do for capture (same apartment rule, one mechanism).
fn openRender(ptr: *anyopaque, spec: Backend.Spec) Backend.Error!Backend.RenderStream {
    const self: *WasapiBackend = @ptrCast(@alignCast(ptr));
    if (spec.kind != .render) return error.Unsupported;
    if (spec.channels == 0 or spec.channels > 2 or spec.rate == 0) return error.Unsupported;
    _ = w.RoInitialize(w.RO_INIT_MULTITHREADED);
    errdefer w.CoUninitialize();
    const slot = self.acquireRender() orelse return error.OutOfMemory;
    errdefer slot.* = .{};
    // activate() picks eRender for every kind but .input and resolves
    // "" to the default endpoint — nothing render-specific to add.
    const client = try activate(spec);
    errdefer client.release();
    var mix: ?*w.WAVEFORMATEX = null;
    if (!w.failed(client.vtbl.GetMixFormat(client, &mix))) {
        slot.mix_rate = mix.?.nSamplesPerSec;
        w.CoTaskMemFree(mix);
    } else slot.mix_rate = 0;
    const flags: u32 = w.AUDCLNT_STREAMFLAGS_EVENTCALLBACK | w.AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | w.AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY;
    const fmt = renderFormat(spec.rate, spec.channels);
    // Duration 0 / period 0: shared-mode event-driven, the engine picks
    // its own period and the smallest buffer that covers it. Measure the
    // resulting GetBufferSize on hardware (spec: "Risks to measure").
    if (w.failed(client.vtbl.Initialize(client, w.AUDCLNT_SHAREMODE_SHARED, flags, 0, 0, &fmt, null))) return error.FormatRejected;
    const event = w.CreateEventW(null, 0, 0, null) orelse return error.ActivationFailed;
    errdefer _ = w.CloseHandle(event);
    if (w.failed(client.vtbl.SetEventHandle(client, event))) return error.ActivationFailed;
    var buf_frames: u32 = 0;
    if (w.failed(client.vtbl.GetBufferSize(client, &buf_frames)) or buf_frames == 0) return error.ActivationFailed;
    var raw: ?*anyopaque = null;
    if (w.failed(client.vtbl.GetService(client, &w.IID_IAudioRenderClient, &raw))) return error.ActivationFailed;
    const rc: *w.IAudioRenderClient = @ptrCast(@alignCast(raw.?));
    errdefer rc.release();
    if (w.failed(client.vtbl.Start(client))) return error.ActivationFailed;
    slot.* = .{
        .in_use = std.atomic.Value(bool).init(true),
        .client = client,
        .render = rc,
        .event = event,
        .buffer_frames = buf_frames,
        .channels = spec.channels,
        .mix_rate = slot.mix_rate,
    };
    return .{ .ptr = slot, .vtable = &render_vtable };
}

/// Task 5: default or named endpoint via IMMDeviceEnumerator. Task 9 adds
/// the `.process` arm (ActivateAudioInterfaceAsync).
fn activate(spec: Backend.Spec) Backend.Error!*w.IAudioClient {
    if (spec.kind == .process) return activateProcess(spec.pid);
    var enumr: ?*anyopaque = null;
    if (w.failed(w.CoCreateInstance(&w.CLSID_MMDeviceEnumerator, null, w.CLSCTX_ALL, &w.IID_IMMDeviceEnumerator, &enumr))) return error.ActivationFailed;
    const en: *w.IMMDeviceEnumerator = @ptrCast(@alignCast(enumr.?));
    defer en.release();
    const flow: u32 = if (spec.kind == .input) w.eCapture else w.eRender;
    var dev: ?*w.IMMDevice = null;
    if (spec.device_id.len == 0) {
        if (w.failed(en.vtbl.GetDefaultAudioEndpoint(en, flow, w.eConsole, &dev))) return error.DeviceNotFound;
    } else {
        // UTF-8 id → wide, NUL-terminated, on the stack.
        var wide: [256:0]u16 = undefined;
        const n = std.unicode.wtf8ToWtf16Le(&wide, spec.device_id) catch return error.DeviceNotFound;
        wide[n] = 0;
        if (w.failed(en.vtbl.GetDevice(en, &wide, &dev))) return error.DeviceNotFound;
    }
    defer dev.?.release();
    var raw: ?*anyopaque = null;
    if (w.failed(dev.?.vtbl.Activate(dev.?, &w.IID_IAudioClient, w.CLSCTX_ALL, null, &raw))) return error.ActivationFailed;
    return @ptrCast(@alignCast(raw.?));
}

/// Mirrors win32_process_loopback.py:855-935: activation params in a
/// VT_BLOB PROPVARIANT, our CompletionHandler, then spin (bounded) until
/// ActivateCompleted fires, then GetActivateResult → IAudioClient.
/// Apartment: this call HARD-REQUIRES a WinRT apartment — the port
/// measured E_ILLEGAL_METHOD_CALL under plain CoInitializeEx
/// (win32_process_loopback.py:816-819). open() already ran
/// RoInitialize(RO_INIT_MULTITHREADED) on this thread (Task 5), so no
/// per-kind branch is needed here.
fn activateProcess(pid: u32) Backend.Error!*w.IAudioClient {
    const activate_fn = w.activateAudioInterfaceAsync() orelse return error.Unsupported;
    var params = w.AUDIOCLIENT_ACTIVATION_PARAMS{
        .ActivationType = w.AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        .params = .{ .ProcessLoopbackParams = .{ .TargetProcessId = pid, .ProcessLoopbackMode = w.PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE } },
    };
    var pv = w.PROPVARIANT{ .vt = w.VT_BLOB, .data = .{ .blob = .{ .cbSize = @sizeOf(w.AUDIOCLIENT_ACTIVATION_PARAMS), .pBlobData = &params } } };
    var handler = w.CompletionHandler{};
    var op: ?*w.IActivateAudioInterfaceAsyncOperation = null;
    if (w.failed(activate_fn(w.VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, &w.IID_IAudioClient, &pv, &handler, &op))) return error.ActivationFailed;
    defer if (op) |o| o.release();
    var waited: u32 = 0;
    while (@atomicLoad(u32, &handler.done, .acquire) == 0) : (waited += 10) {
        if (waited >= 5_000) return error.ActivationFailed;
        w.Sleep(10);
    }
    const done_op = handler.op orelse return error.ActivationFailed;
    defer done_op.release();
    var hr_act: w.HRESULT = 0;
    var raw: ?*anyopaque = null;
    if (w.failed(done_op.vtbl.GetActivateResult(done_op, &hr_act, &raw))) return error.ActivationFailed;
    if (w.failed(hr_act) or raw == null) return error.ActivationFailed;
    return @ptrCast(@alignCast(raw.?));
}

pub const Process = extern struct { pid: u32, ppid: u32, name: [128]u8 };

pub fn enumerateProcesses(out: []Process) usize {
    const snap = w.CreateToolhelp32Snapshot(w.TH32CS_SNAPPROCESS, 0) orelse return 0;
    if (@intFromPtr(snap) == w.INVALID_HANDLE_VALUE) return 0;
    defer _ = w.CloseHandle(snap);
    var entry: w.PROCESSENTRY32W = undefined;
    entry.dwSize = @sizeOf(w.PROCESSENTRY32W);
    if (w.Process32FirstW(snap, &entry) == 0) return 0;
    var n: usize = 0;
    while (n < out.len) {
        if (entry.th32ProcessID != 0) {
            out[n].pid = entry.th32ProcessID;
            out[n].ppid = entry.th32ParentProcessID;
            const z: [*:0]const u16 = @ptrCast(&entry.szExeFile);
            _ = w.wtf16ToUtf8Z(&out[n].name, z);
            n += 1;
        }
        if (w.Process32NextW(snap, &entry) == 0) break;
    }
    return n;
}

fn acquireSlot(self: *WasapiBackend) ?*Stream {
    for (&self.streams) |*s| {
        if (!s.in_use) {
            s.in_use = true;
            return s;
        }
    }
    return null;
}

fn next(ptr: *anyopaque, timeout_ms: u32) Backend.Error!?Backend.Packet {
    const s: *Stream = @ptrCast(@alignCast(ptr));
    var waited: u32 = 0;
    while (!s.stopped.load(.acquire)) {
        var n_frames: u32 = 0;
        if (w.failed(s.capture.?.vtbl.GetNextPacketSize(s.capture.?, &n_frames))) return error.ActivationFailed;
        if (n_frames > 0) {
            var data: ?[*]u8 = null;
            var got: u32 = 0;
            var flags: u32 = 0;
            if (w.failed(s.capture.?.vtbl.GetBuffer(s.capture.?, &data, &got, &flags, null, null))) return error.ActivationFailed;
            const frames = convert.packet(data.?, got, s.src_fmt, flags & w.AUDCLNT_BUFFERFLAGS_SILENT != 0, s.dst_channels, &s.scratch);
            _ = s.capture.?.vtbl.ReleaseBuffer(s.capture.?, got);
            return .{ .frames = frames, .discontinuity = flags & w.AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY != 0 };
        }
        if (waited >= timeout_ms) return null;
        w.Sleep(poll_ms);
        waited += poll_ms;
    }
    return null;
}

fn stop(ptr: *anyopaque) void {
    const s: *Stream = @ptrCast(@alignCast(ptr));
    s.stopped.store(true, .release);
}

fn deinit(ptr: *anyopaque) void {
    const s: *Stream = @ptrCast(@alignCast(ptr));
    if (s.client) |c| _ = c.vtbl.Stop(c);
    if (s.capture) |c| c.release();
    if (s.client) |c| c.release();
    s.* = .{};
    w.CoUninitialize();
}

fn mixRate(ptr: *anyopaque) u32 {
    const s: *Stream = @ptrCast(@alignCast(ptr));
    return s.mix_rate;
}

fn renderWait(ptr: *anyopaque, timeout_ms: u32) bool {
    const r: *Render = @ptrCast(@alignCast(ptr));
    return w.WaitForSingleObject(r.event.?, timeout_ms) == w.WAIT_OBJECT_0;
}

fn renderAvailable(ptr: *anyopaque) Backend.Error!u32 {
    const r: *Render = @ptrCast(@alignCast(ptr));
    var padding: u32 = 0;
    if (w.failed(r.client.?.vtbl.GetCurrentPadding(r.client.?, &padding))) return error.ActivationFailed;
    return r.buffer_frames - @min(padding, r.buffer_frames);
}

fn renderWrite(ptr: *anyopaque, frames: []const f32) Backend.Error!void {
    const r: *Render = @ptrCast(@alignCast(ptr));
    const n: u32 = @intCast(frames.len / r.channels);
    if (n == 0) return;
    // GetBuffer(n) hands back exactly n * channels floats of engine
    // buffer. frames.len may not be a multiple of channels; take only
    // the whole frames and drop the trailing partial one rather than
    // read/write past the engine's buffer.
    const take = n * r.channels;
    var data: ?[*]u8 = null;
    if (w.failed(r.render.?.vtbl.GetBuffer(r.render.?, n, &data))) return error.ActivationFailed;
    // Silence flag: the engine skips the mix for this packet. Cheap scan;
    // the paused loop writes zeros every period.
    const silent = std.mem.allEqual(f32, frames[0..take], 0);
    if (!silent) @memcpy(data.?[0 .. take * @sizeOf(f32)], std.mem.sliceAsBytes(frames[0..take]));
    const flags: u32 = if (silent) w.AUDCLNT_BUFFERFLAGS_SILENT else 0;
    if (w.failed(r.render.?.vtbl.ReleaseBuffer(r.render.?, n, flags))) return error.ActivationFailed;
}

fn renderStop(ptr: *anyopaque) void {
    const r: *Render = @ptrCast(@alignCast(ptr));
    if (r.client) |c| _ = c.vtbl.Stop(c);
}

fn renderDeinit(ptr: *anyopaque) void {
    const r: *Render = @ptrCast(@alignCast(ptr));
    if (r.client) |c| _ = c.vtbl.Stop(c);
    if (r.render) |rc| rc.release();
    if (r.client) |c| c.release();
    if (r.event) |e| _ = w.CloseHandle(e);
    r.* = .{};
    w.CoUninitialize();
}

fn renderMixRate(ptr: *anyopaque) u32 {
    const r: *Render = @ptrCast(@alignCast(ptr));
    return r.mix_rate;
}

test "candidates: first entry is the requested format, all five are well-formed" {
    const c = candidates(96_000, 1);
    try std.testing.expectEqual(@as(u32, 96_000), c[0].nSamplesPerSec);
    try std.testing.expectEqual(@as(u16, 1), c[0].nChannels);
    for (c) |f| {
        try std.testing.expectEqual(f.nChannels * f.wBitsPerSample / 8, f.nBlockAlign);
        try std.testing.expectEqual(f.nSamplesPerSec * f.nBlockAlign, f.nAvgBytesPerSec);
    }
}

test "renderFormat is float32 at the clip's rate and channels; AUTOCONVERTPCM does the rest" {
    const f = renderFormat(96_000, 2);
    try std.testing.expectEqual(w.WAVE_FORMAT_IEEE_FLOAT, f.wFormatTag);
    try std.testing.expectEqual(@as(u16, 32), f.wBitsPerSample);
    try std.testing.expectEqual(@as(u32, 96_000), f.nSamplesPerSec);
    try std.testing.expectEqual(@as(u16, 8), f.nBlockAlign);
    try std.testing.expectEqual(@as(u32, 96_000 * 8), f.nAvgBytesPerSec);
}

test "openRender guard clause rejects bad kind/channels/rate before any COM call" {
    // root.zig only imports this file on Windows, so this is already
    // Windows-only in practice; the explicit skip documents that as a
    // standing contract rather than an accident of the import graph.
    if (builtin.os.tag != .windows) return error.SkipZigTest;
    const base = Backend.Spec{ .kind = .render, .device_id = "", .rate = 48_000, .channels = 2 };
    var wrong_kind = base;
    wrong_kind.kind = .input;
    try std.testing.expectError(error.Unsupported, backend().openRender(wrong_kind));
    var zero_channels = base;
    zero_channels.channels = 0;
    try std.testing.expectError(error.Unsupported, backend().openRender(zero_channels));
    var too_many_channels = base;
    too_many_channels.channels = 3;
    try std.testing.expectError(error.Unsupported, backend().openRender(too_many_channels));
    var zero_rate = base;
    zero_rate.rate = 0;
    try std.testing.expectError(error.Unsupported, backend().openRender(zero_rate));
}
