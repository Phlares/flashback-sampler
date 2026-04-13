"""
Windows per-process WASAPI loopback capture.

Uses `ActivateAudioInterfaceAsync` (from Mmdevapi.dll, available on
Windows 10 build 19041 / May 2020 and later) to activate an
IAudioClient bound to a specific PID. This is the OS-native mechanism
Game Bar / NVIDIA ShadowPlay / Voicemeeter use to capture audio from
one process without touching any other process's audio.

The API is async: you pass an IActivateAudioInterfaceCompletionHandler
COM object, wait for it to signal, then pull the activated interface
out of the resulting operation object. Implementing a COM interface
in Python requires hand-rolling a vtable via ctypes — that's ~150
lines of glue below.

SCOPE NOTE — M12.1:
- All ctypes declarations, struct layouts, vtable definitions, and
  the process enumeration helper are implemented and should compile
  / import cleanly on any Windows machine.
- ProcessLoopbackCapture.start() performs the full activate-and-
  capture sequence but has NOT been validated on real hardware.
- Expect iteration: first Windows run will almost certainly hit
  HRESULT errors that need narrow fixes. The class logs every COM
  failure verbatim so the user can report what blew up.
- Non-Windows platforms see a clean ImportError path: the module
  loads, but constructing ProcessLoopbackCapture raises a
  RuntimeError pointing at platform unsupported.

References:
  https://learn.microsoft.com/en-us/windows/win32/coreaudio/activateaudiointerfaceasync
  https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import (
    POINTER,
    Structure,
    WINFUNCTYPE,
    byref,
    c_int,
    c_int32,
    c_long,
    c_longlong,
    c_short,
    c_ubyte,
    c_uint,
    c_uint16,
    c_uint32,
    c_ulong,
    c_ulonglong,
    c_void_p,
    c_wchar,
    c_wchar_p,
    cast,
    memmove,
    pointer,
    sizeof,
)
from typing import Callable, Optional

import numpy as np


# ─────────────────────────────────────────────────────────────────────────
# Platform + version gate
# ─────────────────────────────────────────────────────────────────────────

IS_WINDOWS = sys.platform == "win32"


def is_supported() -> bool:
    """
    Return True if this platform can use ProcessLoopbackCapture.
    Requires Windows 10 build 19041 (20H1, May 2020) or newer.
    """
    if not IS_WINDOWS:
        return False
    try:
        # sys.getwindowsversion.build is the real kernel build number.
        build = int(getattr(sys.getwindowsversion(), "build", 0))
        return build >= 19041
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────
# HRESULT helpers
# ─────────────────────────────────────────────────────────────────────────

S_OK = 0
S_FALSE = 1
E_NOINTERFACE = 0x80004002
E_POINTER = 0x80004003
E_NOTIMPL = 0x80004001

COINIT_APARTMENTTHREADED = 0x2
COINIT_MULTITHREADED = 0x0

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF

CLSCTX_ALL = 0x17

AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_SHAREMODE_SHARED = 0


def _hresult_failed(hr: int) -> bool:
    """Win32 FAILED() macro — HRESULT is negative = error."""
    return (hr & 0x80000000) != 0


def _hex_hr(hr: int) -> str:
    return f"0x{hr & 0xFFFFFFFF:08X}"


# ─────────────────────────────────────────────────────────────────────────
# GUID / IID definitions
# ─────────────────────────────────────────────────────────────────────────


class GUID(Structure):
    """Standard Windows GUID: DWORD + WORD + WORD + 8 bytes."""
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", c_uint16),
        ("Data3", c_uint16),
        ("Data4", c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, s: str) -> "GUID":
        """
        Parse a "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}" string into a
        GUID. Accepts both braced and unbraced forms.
        """
        s = s.strip().lstrip("{").rstrip("}")
        parts = s.split("-")
        if len(parts) != 5:
            raise ValueError(f"not a GUID string: {s!r}")
        d1 = int(parts[0], 16)
        d2 = int(parts[1], 16)
        d3 = int(parts[2], 16)
        d4_hi = int(parts[3], 16)
        d4_lo = int(parts[4], 16)
        data4 = (c_ubyte * 8)(
            (d4_hi >> 8) & 0xFF,
            d4_hi & 0xFF,
            (d4_lo >> 40) & 0xFF,
            (d4_lo >> 32) & 0xFF,
            (d4_lo >> 24) & 0xFF,
            (d4_lo >> 16) & 0xFF,
            (d4_lo >> 8) & 0xFF,
            d4_lo & 0xFF,
        )
        return cls(d1, d2, d3, data4)

    def equals(self, other: "GUID") -> bool:
        if not isinstance(other, GUID):
            return False
        if self.Data1 != other.Data1:
            return False
        if self.Data2 != other.Data2:
            return False
        if self.Data3 != other.Data3:
            return False
        for i in range(8):
            if self.Data4[i] != other.Data4[i]:
                return False
        return True


IID_IUnknown = GUID.from_string("{00000000-0000-0000-C000-000000000046}")
IID_IAudioClient = GUID.from_string("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID.from_string(
    "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"
)
IID_IActivateAudioInterfaceCompletionHandler = GUID.from_string(
    "{41D949AB-9862-444A-80F6-C261334DA5EB}"
)
IID_IActivateAudioInterfaceAsyncOperation = GUID.from_string(
    "{72A22D78-CDE4-431D-B8CC-843A71199B6D}"
)


# The magic device path for process-loopback activation
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"


# ─────────────────────────────────────────────────────────────────────────
# Activation params (passed via PROPVARIANT BLOB)
# ─────────────────────────────────────────────────────────────────────────

# enum AUDIOCLIENT_ACTIVATION_TYPE
AUDIOCLIENT_ACTIVATION_TYPE_DEFAULT = 0
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1

# enum PROCESS_LOOPBACK_MODE
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [
        ("TargetProcessId", c_uint32),
        ("ProcessLoopbackMode", c_uint32),
    ]


class _ACTIVATION_UNION(ctypes.Union):
    _fields_ = [
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("ActivationType", c_uint32),
        ("u", _ACTIVATION_UNION),
    ]


# Minimal PROPVARIANT — we only populate VT_BLOB
VT_BLOB = 0x0011


class BLOB(Structure):
    _fields_ = [
        ("cbSize", c_uint32),
        ("pBlobData", c_void_p),
    ]


class _PROPVARIANT_UNION(ctypes.Union):
    _fields_ = [
        ("blob", BLOB),
        ("pad", c_ubyte * 16),
    ]


class PROPVARIANT(Structure):
    _fields_ = [
        ("vt", c_uint16),
        ("wReserved1", c_uint16),
        ("wReserved2", c_uint16),
        ("wReserved3", c_uint16),
        ("u", _PROPVARIANT_UNION),
    ]


# ─────────────────────────────────────────────────────────────────────────
# WAVEFORMATEX (for IAudioClient::Initialize + GetMixFormat)
# ─────────────────────────────────────────────────────────────────────────


class WAVEFORMATEX(Structure):
    _fields_ = [
        ("wFormatTag", c_uint16),
        ("nChannels", c_uint16),
        ("nSamplesPerSec", c_uint32),
        ("nAvgBytesPerSec", c_uint32),
        ("nBlockAlign", c_uint16),
        ("wBitsPerSample", c_uint16),
        ("cbSize", c_uint16),
    ]


WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_PCM = 0x0001


# ─────────────────────────────────────────────────────────────────────────
# COM vtable — IActivateAudioInterfaceCompletionHandler
# ─────────────────────────────────────────────────────────────────────────
#
# Layout:
#   struct Handler {
#       Vtbl* lpVtbl;
#   };
#   struct Vtbl {
#       HRESULT (*QueryInterface)(void* this, GUID* riid, void** out);
#       ULONG   (*AddRef)(void* this);
#       ULONG   (*Release)(void* this);
#       HRESULT (*ActivateCompleted)(void* this, IAsyncOp* op);
#   };

QueryInterfaceFn = WINFUNCTYPE(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
AddRefFn = WINFUNCTYPE(c_ulong, c_void_p)
ReleaseFn = WINFUNCTYPE(c_ulong, c_void_p)
ActivateCompletedFn = WINFUNCTYPE(c_long, c_void_p, c_void_p)


class IActivateAudioInterfaceCompletionHandlerVtbl(Structure):
    _fields_ = [
        ("QueryInterface", QueryInterfaceFn),
        ("AddRef", AddRefFn),
        ("Release", ReleaseFn),
        ("ActivateCompleted", ActivateCompletedFn),
    ]


class IActivateAudioInterfaceCompletionHandler(Structure):
    _fields_ = [
        (
            "lpVtbl",
            POINTER(IActivateAudioInterfaceCompletionHandlerVtbl),
        )
    ]


# IActivateAudioInterfaceAsyncOperation has 4 methods:
#   IUnknown: QueryInterface / AddRef / Release
#   GetActivateResult(HRESULT* out_hr, IUnknown** out_interface)

GetActivateResultFn = WINFUNCTYPE(
    c_long, c_void_p, POINTER(c_long), POINTER(c_void_p)
)


class IActivateAudioInterfaceAsyncOperationVtbl(Structure):
    _fields_ = [
        ("QueryInterface", QueryInterfaceFn),
        ("AddRef", AddRefFn),
        ("Release", ReleaseFn),
        ("GetActivateResult", GetActivateResultFn),
    ]


# IAudioClient minimal — we only need Initialize, Start, Stop,
# SetEventHandle, GetService, GetMixFormat, GetBufferSize, plus
# IUnknown base.

AudioClientInitializeFn = WINFUNCTYPE(
    c_long,
    c_void_p,           # this
    c_uint32,           # ShareMode
    c_uint32,           # StreamFlags
    c_longlong,         # hnsBufferDuration (REFERENCE_TIME = 100ns units)
    c_longlong,         # hnsPeriodicity
    POINTER(WAVEFORMATEX),
    c_void_p,           # AudioSessionGuid (nullable)
)
AudioClientGetBufferSizeFn = WINFUNCTYPE(c_long, c_void_p, POINTER(c_uint32))
AudioClientGetStreamLatencyFn = WINFUNCTYPE(c_long, c_void_p, POINTER(c_longlong))
AudioClientGetCurrentPaddingFn = WINFUNCTYPE(c_long, c_void_p, POINTER(c_uint32))
AudioClientIsFormatSupportedFn = WINFUNCTYPE(
    c_long, c_void_p, c_uint32, POINTER(WAVEFORMATEX), POINTER(POINTER(WAVEFORMATEX))
)
AudioClientGetMixFormatFn = WINFUNCTYPE(
    c_long, c_void_p, POINTER(POINTER(WAVEFORMATEX))
)
AudioClientGetDevicePeriodFn = WINFUNCTYPE(
    c_long, c_void_p, POINTER(c_longlong), POINTER(c_longlong)
)
AudioClientStartFn = WINFUNCTYPE(c_long, c_void_p)
AudioClientStopFn = WINFUNCTYPE(c_long, c_void_p)
AudioClientResetFn = WINFUNCTYPE(c_long, c_void_p)
AudioClientSetEventHandleFn = WINFUNCTYPE(c_long, c_void_p, c_void_p)
AudioClientGetServiceFn = WINFUNCTYPE(
    c_long, c_void_p, POINTER(GUID), POINTER(c_void_p)
)


class IAudioClientVtbl(Structure):
    _fields_ = [
        ("QueryInterface", QueryInterfaceFn),
        ("AddRef", AddRefFn),
        ("Release", ReleaseFn),
        ("Initialize", AudioClientInitializeFn),
        ("GetBufferSize", AudioClientGetBufferSizeFn),
        ("GetStreamLatency", AudioClientGetStreamLatencyFn),
        ("GetCurrentPadding", AudioClientGetCurrentPaddingFn),
        ("IsFormatSupported", AudioClientIsFormatSupportedFn),
        ("GetMixFormat", AudioClientGetMixFormatFn),
        ("GetDevicePeriod", AudioClientGetDevicePeriodFn),
        ("Start", AudioClientStartFn),
        ("Stop", AudioClientStopFn),
        ("Reset", AudioClientResetFn),
        ("SetEventHandle", AudioClientSetEventHandleFn),
        ("GetService", AudioClientGetServiceFn),
    ]


# IAudioCaptureClient — 3 methods after IUnknown
CaptureGetBufferFn = WINFUNCTYPE(
    c_long,
    c_void_p,                  # this
    POINTER(c_void_p),         # pData (out)
    POINTER(c_uint32),         # NumFramesToRead
    POINTER(c_uint32),         # dwFlags
    POINTER(c_ulonglong),      # DevicePosition
    POINTER(c_ulonglong),      # QPCPosition
)
CaptureReleaseBufferFn = WINFUNCTYPE(c_long, c_void_p, c_uint32)
CaptureGetNextPacketSizeFn = WINFUNCTYPE(c_long, c_void_p, POINTER(c_uint32))


class IAudioCaptureClientVtbl(Structure):
    _fields_ = [
        ("QueryInterface", QueryInterfaceFn),
        ("AddRef", AddRefFn),
        ("Release", ReleaseFn),
        ("GetBuffer", CaptureGetBufferFn),
        ("ReleaseBuffer", CaptureReleaseBufferFn),
        ("GetNextPacketSize", CaptureGetNextPacketSizeFn),
    ]


# ─────────────────────────────────────────────────────────────────────────
# DLL loaders (lazy — only on Windows)
# ─────────────────────────────────────────────────────────────────────────

_mmdevapi = None
_ole32 = None
_kernel32 = None
_psapi = None


def _load_win_dlls() -> None:
    global _mmdevapi, _ole32, _kernel32, _psapi
    if not IS_WINDOWS:
        return
    if _mmdevapi is None:
        _mmdevapi = ctypes.WinDLL("Mmdevapi.dll")
    if _ole32 is None:
        _ole32 = ctypes.WinDLL("ole32.dll")
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL("kernel32.dll")
    if _psapi is None:
        try:
            _psapi = ctypes.WinDLL("Psapi.dll")
        except OSError:  # Windows 7+: functions live in kernel32
            _psapi = _kernel32


# ─────────────────────────────────────────────────────────────────────────
# Process enumeration helpers (psapi / kernel32)
# ─────────────────────────────────────────────────────────────────────────


TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = -1


class PROCESSENTRY32W(Structure):
    _fields_ = [
        ("dwSize", c_uint32),
        ("cntUsage", c_uint32),
        ("th32ProcessID", c_uint32),
        ("th32DefaultHeapID", c_void_p),
        ("th32ModuleID", c_uint32),
        ("cntThreads", c_uint32),
        ("th32ParentProcessID", c_uint32),
        ("pcPriClassBase", c_int32),
        ("dwFlags", c_uint32),
        ("szExeFile", c_wchar * 260),
    ]


def enumerate_audio_processes(limit: int = 4096) -> list[tuple[int, str]]:
    """
    Enumerate every running process via CreateToolhelp32Snapshot.
    Returns a list of (pid, exe_name) sorted by exe_name then pid.
    The process picker uses this — ActivateAudioInterfaceAsync only
    errors out at activation time, not at PID selection, so we don't
    pre-filter by "has audio session."

    On non-Windows platforms returns an empty list.
    """
    if not IS_WINDOWS:
        return []
    _load_win_dlls()
    assert _kernel32 is not None

    CreateToolhelp32Snapshot = _kernel32.CreateToolhelp32Snapshot
    CreateToolhelp32Snapshot.argtypes = [c_uint32, c_uint32]
    CreateToolhelp32Snapshot.restype = c_void_p

    Process32FirstW = _kernel32.Process32FirstW
    Process32FirstW.argtypes = [c_void_p, POINTER(PROCESSENTRY32W)]
    Process32FirstW.restype = c_int

    Process32NextW = _kernel32.Process32NextW
    Process32NextW.argtypes = [c_void_p, POINTER(PROCESSENTRY32W)]
    Process32NextW.restype = c_int

    CloseHandle = _kernel32.CloseHandle
    CloseHandle.argtypes = [c_void_p]
    CloseHandle.restype = c_int

    snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    # CreateToolhelp32Snapshot returns INVALID_HANDLE_VALUE (-1 / 0xFFFFFFFFFFFFFFFF)
    # on failure. Treat both None and -1 as failure.
    if not snap or (isinstance(snap, int) and snap == INVALID_HANDLE_VALUE):
        return []

    entry = PROCESSENTRY32W()
    entry.dwSize = sizeof(PROCESSENTRY32W)

    results: list[tuple[int, str]] = []
    try:
        if not Process32FirstW(snap, byref(entry)):
            return []
        count = 0
        while True:
            pid = int(entry.th32ProcessID)
            name = str(entry.szExeFile) or ""
            if pid > 0 and name:
                results.append((pid, name))
            count += 1
            if count >= limit:
                break
            if not Process32NextW(snap, byref(entry)):
                break
    finally:
        CloseHandle(snap)

    results.sort(key=lambda t: (t[1].lower(), t[0]))
    return results


# ─────────────────────────────────────────────────────────────────────────
# CompletionHandler — Python COM object
# ─────────────────────────────────────────────────────────────────────────


class _CompletionHandler:
    """
    Python-side COM object implementing
    IActivateAudioInterfaceCompletionHandler. The handler's only real
    job is to signal a Windows event when ActivateCompleted fires so
    the calling thread can unblock and pull the activated interface
    out of the operation.
    """

    def __init__(self) -> None:
        self._ref_count = 1
        self._done_event = None
        self._op_ptr: int = 0

        # Build the vtable. We keep Python references to the wrapped
        # function objects so they don't GC while the C side holds
        # pointers to them.
        self._qi_fn = QueryInterfaceFn(self._query_interface)
        self._ar_fn = AddRefFn(self._add_ref)
        self._rl_fn = ReleaseFn(self._release)
        self._ac_fn = ActivateCompletedFn(self._activate_completed)
        self._vtbl = IActivateAudioInterfaceCompletionHandlerVtbl(
            self._qi_fn,
            self._ar_fn,
            self._rl_fn,
            self._ac_fn,
        )
        self._object = IActivateAudioInterfaceCompletionHandler(
            lpVtbl=pointer(self._vtbl)
        )

        _load_win_dlls()
        assert _kernel32 is not None
        CreateEventW = _kernel32.CreateEventW
        CreateEventW.argtypes = [c_void_p, c_int, c_int, c_wchar_p]
        CreateEventW.restype = c_void_p
        self._done_event = CreateEventW(None, 1, 0, None)  # manual-reset

    # COM methods

    def _query_interface(self, this, riid_ptr, ppv_ptr):  # noqa: ARG002
        if not riid_ptr or not ppv_ptr:
            return E_POINTER
        riid = riid_ptr.contents
        if riid.equals(IID_IUnknown) or riid.equals(
            IID_IActivateAudioInterfaceCompletionHandler
        ):
            ppv_ptr[0] = ctypes.addressof(self._object)
            self._ref_count += 1
            return S_OK
        ppv_ptr[0] = None
        return E_NOINTERFACE

    def _add_ref(self, this):  # noqa: ARG002
        self._ref_count += 1
        return self._ref_count

    def _release(self, this):  # noqa: ARG002
        self._ref_count -= 1
        return max(0, self._ref_count)

    def _activate_completed(self, this, op_ptr):  # noqa: ARG002
        self._op_ptr = int(op_ptr) if op_ptr else 0
        assert _kernel32 is not None
        SetEvent = _kernel32.SetEvent
        SetEvent.argtypes = [c_void_p]
        SetEvent.restype = c_int
        SetEvent(self._done_event)
        return S_OK

    # Controller API

    def this_pointer(self) -> int:
        return ctypes.addressof(self._object)

    def wait_for_completion(self, timeout_ms: int = 5000) -> bool:
        assert _kernel32 is not None
        WaitForSingleObject = _kernel32.WaitForSingleObject
        WaitForSingleObject.argtypes = [c_void_p, c_uint32]
        WaitForSingleObject.restype = c_uint32
        rc = WaitForSingleObject(self._done_event, timeout_ms)
        return rc == WAIT_OBJECT_0

    def operation_pointer(self) -> int:
        return self._op_ptr


# ─────────────────────────────────────────────────────────────────────────
# ProcessLoopbackCapture
# ─────────────────────────────────────────────────────────────────────────


class ProcessLoopbackCapture:
    """
    CaptureSource that captures audio output from a single Windows
    process (and optionally its child process tree) via WASAPI
    process loopback. Conforms to the CaptureSource Protocol.
    """

    def __init__(
        self,
        buffer,  # AudioCircularBuffer — typed as Any to avoid core import cycle
        pid: int,
        sample_rate: int = 48_000,
        channels: int = 2,
        include_tree: bool = True,
        on_level: Optional[Callable] = None,
    ) -> None:
        if not is_supported():
            raise RuntimeError(
                "ProcessLoopbackCapture requires Windows 10 build 19041 "
                "(May 2020) or newer."
            )
        self.buffer = buffer
        self.pid = int(pid)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.include_tree = bool(include_tree)
        self.on_level = on_level

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._dropped_callbacks = 0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # CaptureSource protocol
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._running

    def xrun_count(self) -> int:
        return int(self._dropped_callbacks)

    def start(self) -> None:
        if self._running:
            return
        _load_win_dlls()
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(
            f"[ProcessLoopbackCapture] Started — pid={self.pid}, "
            f"{self.sample_rate}Hz, {self.channels}ch"
        )

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        self._running = False
        print("[ProcessLoopbackCapture] Stopped.")
        if self._last_error:
            print(f"[ProcessLoopbackCapture] Last error: {self._last_error}")

    # ------------------------------------------------------------------
    # The capture thread — all COM work happens here so we don't need
    # to marshal COM objects across thread boundaries.
    # ------------------------------------------------------------------

    def _run(self) -> None:
        if not IS_WINDOWS:
            self._last_error = "not running on Windows"
            self._running = False
            return

        assert _ole32 is not None
        assert _mmdevapi is not None
        assert _kernel32 is not None

        # Initialize COM for this thread (MTA — ActivateAudioInterfaceAsync
        # requires multithreaded apartment).
        CoInitializeEx = _ole32.CoInitializeEx
        CoInitializeEx.argtypes = [c_void_p, c_uint32]
        CoInitializeEx.restype = c_long
        CoUninitialize = _ole32.CoUninitialize
        CoUninitialize.argtypes = []
        CoUninitialize.restype = None

        hr = CoInitializeEx(None, COINIT_MULTITHREADED)
        if _hresult_failed(hr) and hr != 0x80010106:  # RPC_E_CHANGED_MODE is ok
            self._last_error = f"CoInitializeEx failed: {_hex_hr(hr)}"
            self._running = False
            return

        try:
            self._run_captured_com()
        finally:
            CoUninitialize()

    def _run_captured_com(self) -> None:
        assert _mmdevapi is not None
        assert _kernel32 is not None

        # ── Prepare AUDIOCLIENT_ACTIVATION_PARAMS ───────────────────
        params = AUDIOCLIENT_ACTIVATION_PARAMS()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId = self.pid
        params.ProcessLoopbackParams.ProcessLoopbackMode = (
            PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
            if self.include_tree
            else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
        )

        # Wrap in a PROPVARIANT BLOB
        pv = PROPVARIANT()
        pv.vt = VT_BLOB
        pv.u.blob.cbSize = sizeof(AUDIOCLIENT_ACTIVATION_PARAMS)
        pv.u.blob.pBlobData = ctypes.cast(pointer(params), c_void_p)

        # ── Completion handler ───────────────────────────────────────
        handler = _CompletionHandler()

        # ── ActivateAudioInterfaceAsync ─────────────────────────────
        ActivateAudioInterfaceAsync = _mmdevapi.ActivateAudioInterfaceAsync
        ActivateAudioInterfaceAsync.argtypes = [
            c_wchar_p,
            POINTER(GUID),
            POINTER(PROPVARIANT),
            c_void_p,  # IActivateAudioInterfaceCompletionHandler*
            POINTER(c_void_p),  # IActivateAudioInterfaceAsyncOperation** out
        ]
        ActivateAudioInterfaceAsync.restype = c_long

        operation_out = c_void_p(0)
        hr = ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            byref(IID_IAudioClient),
            byref(pv),
            handler.this_pointer(),
            byref(operation_out),
        )
        if _hresult_failed(hr):
            self._last_error = (
                f"ActivateAudioInterfaceAsync failed: {_hex_hr(hr)}"
            )
            self._running = False
            return

        # Wait for ActivateCompleted
        if not handler.wait_for_completion(timeout_ms=5000):
            self._last_error = "ActivateCompleted timed out"
            self._running = False
            return

        op_ptr = handler.operation_pointer()
        if not op_ptr:
            self._last_error = "ActivateCompleted returned null operation"
            self._running = False
            return

        # ── Pull the IAudioClient out of the operation ──────────────
        op_vtbl_ptr = ctypes.cast(
            ctypes.c_void_p(op_ptr), POINTER(c_void_p)
        )[0]
        op_vtbl = ctypes.cast(
            op_vtbl_ptr, POINTER(IActivateAudioInterfaceAsyncOperationVtbl)
        )[0]
        activate_hr = c_long(0)
        audio_client_out = c_void_p(0)
        hr = op_vtbl.GetActivateResult(
            op_ptr, byref(activate_hr), byref(audio_client_out)
        )
        if _hresult_failed(hr):
            self._last_error = f"GetActivateResult failed: {_hex_hr(hr)}"
            self._running = False
            return
        if _hresult_failed(activate_hr.value):
            self._last_error = (
                f"Activation HRESULT failed: {_hex_hr(activate_hr.value)} "
                f"(common causes: PID has no audio, Windows build too old)"
            )
            self._running = False
            return
        if not audio_client_out.value:
            self._last_error = "GetActivateResult returned null interface"
            self._running = False
            return

        ac_ptr = audio_client_out.value
        ac_vtbl_ptr = ctypes.cast(
            c_void_p(ac_ptr), POINTER(c_void_p)
        )[0]
        ac_vtbl = ctypes.cast(
            ac_vtbl_ptr, POINTER(IAudioClientVtbl)
        )[0]

        # ── Initialize the audio client for process-loopback capture ─
        # For loopback, we want LOOPBACK + EVENTCALLBACK flags.
        # Buffer duration: request ~200 ms in 100ns units (REFERENCE_TIME)
        REFTIME_MS = 10_000  # 1 ms = 10_000 units of 100 ns
        buffer_duration = 200 * REFTIME_MS

        # Build WAVEFORMATEX for the requested format
        fmt = WAVEFORMATEX()
        fmt.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
        fmt.nChannels = self.channels
        fmt.nSamplesPerSec = self.sample_rate
        fmt.wBitsPerSample = 32
        fmt.nBlockAlign = (fmt.nChannels * fmt.wBitsPerSample) // 8
        fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign
        fmt.cbSize = 0

        hr = ac_vtbl.Initialize(
            ac_ptr,
            AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
            buffer_duration,
            0,
            pointer(fmt),
            None,
        )
        if _hresult_failed(hr):
            self._last_error = (
                f"IAudioClient::Initialize failed: {_hex_hr(hr)} "
                f"(may need to accept the native mix format — M12.2 work)"
            )
            ac_vtbl.Release(ac_ptr)
            self._running = False
            return

        # ── Event handle for buffer-ready callback ──────────────────
        CreateEventW = _kernel32.CreateEventW
        CreateEventW.argtypes = [c_void_p, c_int, c_int, c_wchar_p]
        CreateEventW.restype = c_void_p
        buffer_event = CreateEventW(None, 0, 0, None)  # auto-reset

        hr = ac_vtbl.SetEventHandle(ac_ptr, buffer_event)
        if _hresult_failed(hr):
            self._last_error = f"SetEventHandle failed: {_hex_hr(hr)}"
            ac_vtbl.Release(ac_ptr)
            self._running = False
            return

        # ── Get IAudioCaptureClient ─────────────────────────────────
        capture_out = c_void_p(0)
        hr = ac_vtbl.GetService(
            ac_ptr, byref(IID_IAudioCaptureClient), byref(capture_out)
        )
        if _hresult_failed(hr):
            self._last_error = f"GetService(IAudioCaptureClient) failed: {_hex_hr(hr)}"
            ac_vtbl.Release(ac_ptr)
            self._running = False
            return

        cap_ptr = capture_out.value
        cap_vtbl_ptr = ctypes.cast(c_void_p(cap_ptr), POINTER(c_void_p))[0]
        cap_vtbl = ctypes.cast(
            cap_vtbl_ptr, POINTER(IAudioCaptureClientVtbl)
        )[0]

        # ── Start the stream ────────────────────────────────────────
        hr = ac_vtbl.Start(ac_ptr)
        if _hresult_failed(hr):
            self._last_error = f"IAudioClient::Start failed: {_hex_hr(hr)}"
            cap_vtbl.Release(cap_ptr)
            ac_vtbl.Release(ac_ptr)
            self._running = False
            return

        WaitForSingleObject = _kernel32.WaitForSingleObject
        WaitForSingleObject.argtypes = [c_void_p, c_uint32]
        WaitForSingleObject.restype = c_uint32

        # ── Capture loop ────────────────────────────────────────────
        try:
            while not self._stop_event.is_set():
                rc = WaitForSingleObject(buffer_event, 500)
                if rc == WAIT_TIMEOUT:
                    self._dropped_callbacks += 1
                    continue
                if rc != WAIT_OBJECT_0:
                    break

                # Drain all currently-available packets
                while True:
                    next_size = c_uint32(0)
                    hr = cap_vtbl.GetNextPacketSize(cap_ptr, byref(next_size))
                    if _hresult_failed(hr):
                        self._dropped_callbacks += 1
                        break
                    if next_size.value == 0:
                        break
                    data_ptr = c_void_p(0)
                    n_frames = c_uint32(0)
                    flags = c_uint32(0)
                    hr = cap_vtbl.GetBuffer(
                        cap_ptr,
                        byref(data_ptr),
                        byref(n_frames),
                        byref(flags),
                        None,
                        None,
                    )
                    if _hresult_failed(hr):
                        self._dropped_callbacks += 1
                        break
                    if n_frames.value > 0:
                        n = n_frames.value
                        bytes_total = n * self.channels * 4  # float32
                        arr = (ctypes.c_ubyte * bytes_total).from_address(
                            int(data_ptr.value)
                        )
                        np_view = np.frombuffer(arr, dtype=np.float32).reshape(
                            n, self.channels
                        )
                        self.buffer.write(np_view.copy())
                        if self.on_level:
                            rms = np.sqrt(np.mean(np_view ** 2, axis=0))
                            self.on_level(rms)
                    cap_vtbl.ReleaseBuffer(cap_ptr, n_frames.value)
        finally:
            try:
                ac_vtbl.Stop(ac_ptr)
            except Exception:
                pass
            try:
                cap_vtbl.Release(cap_ptr)
            except Exception:
                pass
            try:
                ac_vtbl.Release(ac_ptr)
            except Exception:
                pass
