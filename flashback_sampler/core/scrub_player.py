"""NativeScrubPlayer — the clip preview player on the Zig core.

Python holds a handle. The Zig render thread opens the WASAPI output,
fills it from an owned copy of the clip, and publishes cursor/playing
through atomics (core/src/Playback.zig). Nothing here touches frames.

`bind` hands the checkout's audio and rate across; the stream opens at
that rate and the OS resamples to the mix rate. Playback auto-stops at
the end of the clip (no loop); `play` at the end rewinds.
"""
from __future__ import annotations

import ctypes as C

import numpy as np

from flashback_sampler.core import native


class NativeScrubPlayer:
    def __init__(self, sample_rate: int = 48_000, channels: int = 2, device: str = ""):
        # No native call here. AppState builds a player at startup, and a
        # workstation with no Zig build must still import and run (see
        # tests/conftest.py); the handle appears on the first bind/play,
        # and only that call can raise.
        self._lib = None
        self._h = None
        # Separates "not created yet" from "destroyed": both leave _h
        # None, but only the first may create.
        self._closed = False
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.device = device

    def _handle(self):
        """The native handle, created on first need. None once closed."""
        if self._h is None and not self._closed:
            lib = native.load()
            if lib is None:
                raise RuntimeError("flashback_core library not available")
            self._lib = lib
            self._h = lib.fb_playback_create(self.device.encode("utf-8"), self.sample_rate, self.channels)
            if not self._h:
                raise RuntimeError("fb_playback_create failed (bad args, or no render backend on this OS)")
        return self._h

    # -- transport ------------------------------------------------------
    def bind(self, audio: np.ndarray, sample_rate: int) -> None:
        if not self._handle():
            return
        audio = native._frames2d(audio)
        n_frames, channels = audio.shape
        status = self._lib.fb_playback_bind(self._h, native._as_f32p(audio), n_frames, int(sample_rate), channels)
        if status == native._INVALID_ARG:
            raise ValueError(f"fb_playback_bind rejected {n_frames} frames x {channels} ch at {sample_rate} Hz")
        if status == native._OUT_OF_MEMORY:
            raise MemoryError(f"fb_playback_bind: could not allocate {audio.nbytes} bytes")
        if status != native._OK:
            raise RuntimeError(f"fb_playback_bind failed with status {status}")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)

    def bind_checkout(self, scratch, h: int, start: int, n: int, sample_rate: int, channels: int) -> None:
        """Bind `[start, start + n)` of a checkout handle: Zig copies from
        the checkout's RAM copy or reads its file — no numpy round trip."""
        if not self._handle():
            return
        status = self._lib.fb_playback_bind_checkout(self._h, scratch.handle, h, int(start), int(n))
        if status == native._INVALID_ARG:
            raise ValueError(f"fb_playback_bind_checkout rejected span {start}+{n}")
        if status == native._OUT_OF_MEMORY:
            raise MemoryError("fb_playback_bind_checkout: could not allocate the clip")
        if status != native._OK:
            raise RuntimeError(f"fb_playback_bind_checkout failed with status {status}")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)

    def play(self) -> None:
        if not self._handle():
            return
        status = self._lib.fb_playback_play(self._h)
        if status != native._OK:
            raise RuntimeError(f"fb_playback_play failed with status {status}: {self.last_error() or ''}")

    def pause(self) -> None:
        if self._h:
            self._lib.fb_playback_pause(self._h)

    def stop(self) -> None:
        self.pause()
        self.seek_samples(0)

    def seek_samples(self, pos: int) -> None:
        if self._h:
            # The ABI takes u64; a negative int must not wrap on the wire.
            self._lib.fb_playback_seek(self._h, max(0, int(pos)))

    def seek(self, seconds: float) -> None:
        self.seek_samples(int(round(seconds * self.sample_rate)))

    def set_device(self, device: str) -> None:
        self.device = device
        if self._h:
            self._lib.fb_playback_set_device(self._h, device.encode("utf-8"))

    # -- state ----------------------------------------------------------
    @property
    def cursor_samples(self) -> int:
        return int(self._state().cursor)

    @property
    def cursor_seconds(self) -> float:
        return self.cursor_samples / float(self.sample_rate)

    @property
    def is_playing(self) -> bool:
        return bool(self._state().playing)

    @property
    def source_length_samples(self) -> int:
        return int(self._state().clip_frames)

    def last_error(self) -> str | None:
        if not self._h:
            return None
        raw = self._lib.fb_playback_last_error(self._h)
        return raw.decode("utf-8", "replace") if raw else None

    def close(self) -> None:
        self._closed = True
        if self._h:
            self._lib.fb_playback_destroy(self._h)
            self._h = None

    def _state(self) -> native.FbPlaybackState:
        st = native.FbPlaybackState()
        if self._h:
            self._lib.fb_playback_state(self._h, C.byref(st))
        return st

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
