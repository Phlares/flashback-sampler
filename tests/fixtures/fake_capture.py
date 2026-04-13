"""
Fake CaptureSource implementations for unit tests.

These satisfy `flashback_sampler.core.capture_source.CaptureSource`
structurally — they have the right methods and attributes — so tests
can inject them without pulling in soundcard / sounddevice.
"""

from __future__ import annotations

import threading
import time

import numpy as np

from flashback_sampler.core.buffer import AudioCircularBuffer


class SilenceCaptureSource:
    """
    Background thread that writes zero-filled frames into an
    AudioCircularBuffer at a simulated real-time rate. Useful for
    running the rest of the pipeline headless without any signal.
    """

    def __init__(
        self,
        buffer: AudioCircularBuffer,
        sample_rate: int = 48_000,
        channels: int = 2,
        blocksize: int = 512,
    ):
        self.buffer = buffer
        self.sample_rate = sample_rate
        self.channels = channels
        self.blocksize = blocksize
        self._running = False
        self._dropped_callbacks = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def xrun_count(self) -> int:
        return int(self._dropped_callbacks)

    def last_error(self) -> str | None:
        return None

    def _run(self) -> None:
        block = np.zeros((self.blocksize, self.channels), dtype=np.float32)
        interval = self.blocksize / self.sample_rate
        next_tick = time.monotonic()
        while not self._stop_event.is_set():
            self.buffer.write(block)
            next_tick += interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_tick = time.monotonic()


class FakeCaptureSourceNoThread:
    """
    Immediate-write fake: call fill(seconds) to push audio into the
    buffer synchronously, no threading. Useful when a test wants to
    seed audio without real-time pacing.
    """

    def __init__(
        self,
        buffer: AudioCircularBuffer,
        sample_rate: int = 48_000,
        channels: int = 2,
    ):
        self.buffer = buffer
        self.sample_rate = sample_rate
        self.channels = channels
        self._running = False
        self._xruns = 0

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def xrun_count(self) -> int:
        return int(self._xruns)

    def last_error(self) -> str | None:
        return None

    def fill(self, seconds: float, amplitude: float = 0.25) -> None:
        n = int(seconds * self.sample_rate)
        if n <= 0:
            return
        t = np.arange(n) / self.sample_rate
        sine = (np.sin(2 * np.pi * 440.0 * t) * amplitude).astype(np.float32)
        frames = np.tile(sine[:, None], (1, self.channels))
        self.buffer.write(frames)

    def bump_xrun(self) -> None:
        self._xruns += 1
