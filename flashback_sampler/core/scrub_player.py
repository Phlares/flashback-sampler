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
        lib = native.load()
        if lib is None:
            raise RuntimeError("flashback_core library not available")
        self._lib = lib
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.device = device
        self._h = lib.fb_playback_create(device.encode("utf-8"), self.sample_rate, self.channels)
        if not self._h:
            raise RuntimeError("fb_playback_create failed (bad args, or no render backend on this OS)")

    # -- transport ------------------------------------------------------
    def bind(self, audio: np.ndarray, sample_rate: int) -> None:
        if audio.ndim == 1:
            audio = audio[:, np.newaxis]
        audio = np.ascontiguousarray(audio, dtype=np.float32)
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

    def play(self) -> None:
        if not self._h:
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
