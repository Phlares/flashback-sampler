"""NativeCaptureSource — the CaptureSource that runs on the Zig core.

Python holds a handle. The Zig thread opens the WASAPI stream and writes
straight into the ring; nothing here touches audio frames. One class for
every kind ("loopback", "input", "process") — the kind is a field of the
spec the Zig side receives, not a Python class.
"""
from __future__ import annotations

import ctypes as C

from flashback_sampler.core import native


class NativeCaptureSource:
    def __init__(self, buffer, kind: str, device_id: str = "", pid: int = 0,
                 sample_rate: int = 48_000, channels: int = 2):
        h = getattr(buffer, "_h", None)
        if not h:
            raise TypeError("NativeCaptureSource needs a NativeAudioCircularBuffer (no native ring handle)")
        if kind not in native.KIND_INTS:
            raise ValueError(f"unknown capture kind {kind!r}; expected one of {sorted(native.KIND_INTS)}")
        lib = native.load()
        if lib is None:
            raise RuntimeError("flashback_core library not available")
        self._lib = lib
        self.kind = kind
        self.device_id = device_id
        self.pid = int(pid)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        # Keep the encoded id alive: the spec holds a raw pointer into it
        # for the duration of fb_capture_create (Zig copies it out).
        self._id_bytes = device_id.encode("utf-8")
        spec = native.FbCaptureSpec(native.KIND_INTS[kind], self.pid, self.sample_rate, self.channels, self._id_bytes)
        self._h = lib.fb_capture_create(h, C.byref(spec))
        if not self._h:
            raise RuntimeError("fb_capture_create failed (bad spec, or no capture backend on this OS)")
        self._started = False

    # -- CaptureSource protocol ----------------------------------------
    def start(self) -> None:
        if self._started:
            return
        status = self._lib.fb_capture_start(self._h)
        if status != native._OK:
            raise RuntimeError(f"fb_capture_start failed with status {status}")
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._lib.fb_capture_stop(self._h)
        self._started = False

    def is_running(self) -> bool:
        return bool(self._stats().running)

    def xrun_count(self) -> int:
        return int(self._stats().xruns)

    def last_error(self) -> str | None:
        raw = self._lib.fb_capture_last_error(self._h)
        return raw.decode("utf-8", "replace") if raw else None

    # -- extras -------------------------------------------------------
    def frames_written(self) -> int:
        return int(self._stats().frames_written)

    def mix_rate(self) -> int:
        return int(self._stats().mix_rate)

    def close(self) -> None:
        if self._h:
            self._lib.fb_capture_destroy(self._h)
            self._h = None

    def _stats(self) -> native.FbCaptureStats:
        st = native.FbCaptureStats()
        self._lib.fb_capture_stats(self._h, C.byref(st))
        return st

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
