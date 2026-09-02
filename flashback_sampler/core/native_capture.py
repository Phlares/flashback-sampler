"""The CaptureSources that run on the Zig core.

Python holds a handle. The Zig thread opens the WASAPI stream and writes
straight into the ring; nothing here touches audio frames.

`_NativeSource` is the shared handle lifecycle (start/stop/stats/
last_error/destroy), which the fb_capture_* and fb_mixer_* ABIs have in
common. `NativeCaptureSource` is one device: one class for every kind
("loopback", "input", "process") — the kind is a field of the spec the
Zig side receives, not a Python class. `NativeMixedSource` is a handle on
the Zig Mixer: N devices summed onto one ring by a Zig mixer thread.
"""
from __future__ import annotations

import ctypes as C
import sys

from flashback_sampler.core import native


def is_process_loopback_supported() -> bool:
    """Per-process WASAPI loopback needs Windows 10 build 19041 (20H1,
    May 2020) or newer — the same floor the ctypes port enforced."""
    if sys.platform != "win32":
        return False
    try:
        return sys.getwindowsversion().build >= 19041
    except Exception:
        return False


class _NativeSource:
    """Shared handle lifecycle for the fb_capture_* and fb_mixer_*
    families. The two ABIs have the same start/stop/stats/last_error/
    destroy shape; a subclass names its prefix in `_api` and creates
    `_h`. Every call goes through `_call`, so a closed instance (`_h` is
    None) can be made inert in ONE place: the Zig exports take a
    non-optional pointer, and NULL through them is undefined behaviour in
    the DLL, not a Python exception."""

    _api: str
    sample_rate: int
    channels: int

    def _call(self, name: str):
        return getattr(self._lib, f"{self._api}_{name}")

    # -- CaptureSource protocol ----------------------------------------
    def start(self) -> None:
        if self._h is None:
            raise RuntimeError(f"{type(self).__name__} is closed")
        if self._started:
            return
        status = self._call("start")(self._h)
        if status != native._OK:
            raise RuntimeError(f"{self._api}_start failed with status {status}")
        self._started = True

    def stop(self) -> None:
        if self._h is None or not self._started:
            return
        self._call("stop")(self._h)
        self._started = False

    def is_running(self) -> bool:
        return bool(self._stats().running)

    def xrun_count(self) -> int:
        return int(self._stats().xruns)

    def last_error(self) -> str | None:
        if self._h is None:
            return None
        raw = self._call("last_error")(self._h)
        return raw.decode("utf-8", "replace") if raw else None

    # -- extras -------------------------------------------------------
    def frames_written(self) -> int:
        return int(self._stats().frames_written)

    def mix_rate(self) -> int:
        return int(self._stats().mix_rate)

    def running_sources(self) -> int:
        """Bitmask: bit i set while source i streams. A single capture
        reports bit 0; a mixer reports one bit per source, so a dead
        source can be named while the mix carries on (#47)."""
        return int(self._stats().sources)

    def close(self) -> None:
        if self._h:
            self._call("destroy")(self._h)
            self._h = None
        self._started = False

    def _stats(self) -> native.FbCaptureStats:
        st = native.FbCaptureStats()
        if self._h is not None:
            self._call("stats")(self._h, C.byref(st))
        return st

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class NativeCaptureSource(_NativeSource):
    """One source. The kind ("loopback", "input", "process") is a field of
    the spec the Zig side receives, not a Python class."""

    _api = "fb_capture"

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


class NativeMixedSource(_NativeSource):
    """N sources summed into one ring by the Zig mixer thread. `specs` is
    a list of NativeCaptureSource keyword dicts ({"kind", "device_id",
    "pid"}); the staging rings live inside the Zig Mixer and never reach
    Python. Zig validates the count (1..native.MAX_MIXER_SOURCES) and each
    spec; a rejection surfaces as fb_mixer_create returning NULL.

    No level compensation: the staging rings are unreachable from Python,
    so the target applies its `Ring.gain`; 1/N pre-mix gain stays the
    caller's job."""

    _api = "fb_mixer"

    def __init__(self, buffer, specs: list[dict], sample_rate: int = 48_000, channels: int = 2):
        h = getattr(buffer, "_h", None)
        if not h:
            raise TypeError("NativeMixedSource needs a NativeAudioCircularBuffer (no native ring handle)")
        lib = native.load()
        if lib is None:
            raise RuntimeError("flashback_core library not available")
        self._lib = lib
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.specs = [dict(s) for s in specs]
        # Encoded ids stay referenced for the duration of fb_mixer_create
        # (Zig copies them out) — one bytes object per spec.
        self._id_bytes = [str(s.get("device_id", "")).encode("utf-8") for s in self.specs]
        arr = (native.FbCaptureSpec * max(len(self.specs), 1))()
        for i, (s, raw) in enumerate(zip(self.specs, self._id_bytes)):
            kind = s["kind"]
            if kind not in native.KIND_INTS:
                raise ValueError(f"unknown capture kind {kind!r}; expected one of {sorted(native.KIND_INTS)}")
            arr[i] = native.FbCaptureSpec(native.KIND_INTS[kind], int(s.get("pid", 0)), self.sample_rate, self.channels, raw)
        self._h = lib.fb_mixer_create(h, arr, len(self.specs))
        if not self._h:
            raise RuntimeError(
                f"fb_mixer_create failed ({len(self.specs)} specs; needs 1..{native.MAX_MIXER_SOURCES} "
                "valid specs and a capture backend on this OS)"
            )
        self._started = False
