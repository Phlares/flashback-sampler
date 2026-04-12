"""
ScrubPlayer — a single long-lived output stream with a manually managed
read cursor for auditioning checked-out clips.

Design notes
------------
- One `sd.OutputStream` instance, created lazily in `open()`, reused for
  the lifetime of the app. Swapping the previewed clip is an in-place
  `bind()` — the stream never restarts.
- The audio callback (`_audio_callback`) reads from
  `self._source[self._cursor:self._cursor + frames]` and advances the
  cursor. Seeking is an atomic cursor assignment. Pausing causes the
  callback to zero-fill without advancing. End-of-source auto-stops
  (no loop); pressing play again rewinds to 0.
- The callback is a plain Python method, deliberately separated from the
  sounddevice wiring in `open()`/`close()`. Tests invoke the callback
  directly with a numpy output buffer — no real audio device required
  for 95% of the unit tests.
- State (`_source`, `_cursor`, `_playing`) is guarded by a single
  `threading.Lock()`. The callback holds the lock only long enough to
  snapshot the state and perform the slice copy, which is microseconds
  for typical block sizes.
"""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np


class ScrubPlayer:
    """
    Preview player with seek + pause. Supports a single bound source at a
    time (one active preview — see the plan's "multiple checkouts, one
    active preview" decision).
    """

    def __init__(
        self,
        sample_rate: int = 48_000,
        channels: int = 2,
        blocksize: int = 1024,
        device: Optional[int] = None,
    ):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.blocksize = int(blocksize)
        self.device = device

        self._lock = threading.Lock()
        self._source: Optional[np.ndarray] = None
        self._cursor: int = 0
        self._playing: bool = False
        self._stream = None  # sd.OutputStream, lazy

    # ------------------------------------------------------------------
    # Public API (UI thread)
    # ------------------------------------------------------------------

    def bind(self, audio: np.ndarray) -> None:
        """
        Atomically replace the source, reset cursor to 0, and pause. The
        input must be shape (N, channels) and will be viewed as float32.
        """
        if audio.ndim != 2:
            raise ValueError(
                f"audio must be [N, channels], got ndim={audio.ndim}"
            )
        if audio.shape[1] != self.channels:
            raise ValueError(
                f"audio has {audio.shape[1]} channels, player has "
                f"{self.channels}"
            )
        audio_f32 = audio.astype(np.float32, copy=False)
        with self._lock:
            self._source = audio_f32
            self._cursor = 0
            self._playing = False

    def play(self) -> None:
        """Begin (or resume) playback. Rewinds if the cursor is at the end."""
        with self._lock:
            if self._source is None:
                return
            if self._cursor >= len(self._source):
                self._cursor = 0
            self._playing = True

    def pause(self) -> None:
        with self._lock:
            self._playing = False

    def stop(self) -> None:
        """Pause, reset cursor, and release the source reference."""
        with self._lock:
            self._playing = False
            self._cursor = 0
            self._source = None

    def seek_samples(self, pos: int) -> None:
        """Atomically set the cursor. Clamped to [0, len(source)]."""
        with self._lock:
            if self._source is None:
                return
            self._cursor = max(0, min(int(pos), len(self._source)))

    def seek(self, seconds: float) -> None:
        self.seek_samples(int(round(seconds * self.sample_rate)))

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------

    @property
    def cursor_samples(self) -> int:
        with self._lock:
            return self._cursor

    @property
    def cursor_seconds(self) -> float:
        return self.cursor_samples / float(self.sample_rate)

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    @property
    def source_length_samples(self) -> int:
        with self._lock:
            return 0 if self._source is None else len(self._source)

    # ------------------------------------------------------------------
    # Audio callback (PortAudio thread)
    # ------------------------------------------------------------------

    def _audio_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info,  # noqa: ARG002 — PortAudio callback signature
        status,  # noqa: ARG002
    ) -> None:
        """
        Fill `outdata` (shape [frames, channels]) from the bound source.

        Idle / paused / end-of-source → zero-fill. Partial final fill
        also zero-pads the remainder and auto-stops playback.
        """
        with self._lock:
            source = self._source
            cursor = self._cursor
            if not self._playing or source is None:
                outdata.fill(0.0)
                return
            n_avail = len(source) - cursor
            if n_avail <= 0:
                outdata.fill(0.0)
                self._playing = False
                return
            n_fill = min(frames, n_avail)
            outdata[:n_fill] = source[cursor : cursor + n_fill]
            if n_fill < frames:
                outdata[n_fill:].fill(0.0)
                self._playing = False  # auto-stop after the final partial
            self._cursor = cursor + n_fill

    # ------------------------------------------------------------------
    # Device lifecycle (thin wrapper — only exercised by audio_hw tests)
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Create and start the underlying sounddevice OutputStream."""
        if self._stream is not None:
            return
        import sounddevice as sd  # lazy — lets unit tests skip PortAudio

        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            device=self.device,
            dtype="float32",
            blocksize=self.blocksize,
            callback=self._audio_callback,
            latency="low",
        )
        self._stream.start()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                self._stream.close()
                self._stream = None

    def __enter__(self) -> "ScrubPlayer":
        self.open()
        return self

    def __exit__(self, *_) -> None:
        self.close()
