"""
LoopbackCapture — WASAPI loopback via the `soundcard` library.

Windows-focused alternative to AudioCapture (which uses sounddevice/PortAudio).
PortAudio wheels don't ship WASAPI loopback, so for "capture what's playing"
we bypass it entirely and talk to WASAPI through soundcard.

Same public surface as AudioCapture: start() / stop() / context manager,
pumps float32 frames into an AudioCircularBuffer.
"""

import sys
import threading
import time
from typing import Callable, Optional

import numpy as np

from .buffer import AudioCircularBuffer

# Windows: soundcard talks to Media Foundation via COM, which must be
# initialized on every thread that calls into it. Our capture runs on a
# background thread, so we CoInitializeEx here ourselves.
_IS_WIN = sys.platform == "win32"
if _IS_WIN:
    import ctypes
    _ole32 = ctypes.windll.ole32
    _COINIT_MULTITHREADED = 0x0

# Lazy import so the rest of the package is usable without soundcard installed
_sc = None
def _get_sc():
    global _sc
    if _sc is None:
        import soundcard as sc
        _sc = sc
    return _sc


class LoopbackCapture:
    """
    Continuously record the default (or named) speaker's loopback stream
    into an AudioCircularBuffer.

    Usage:
        buf = AudioCircularBuffer(duration_seconds=60, channels=2)
        cap = LoopbackCapture(buf)            # default speaker
        cap.start()
        ...
        cap.stop()
    """

    def __init__(
        self,
        buffer: AudioCircularBuffer,
        speaker_name: Optional[str] = None,   # None = default speaker
        sample_rate: int = 48_000,
        channels: int = 2,
        blocksize: int = 4096,
        on_level: Optional[Callable] = None,
    ):
        self.buffer = buffer
        self.speaker_name = speaker_name
        self.sample_rate = sample_rate
        self.channels = channels
        self.blocksize = blocksize
        self.on_level = on_level

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._mic = None
        # xrun counter: soundcard.rec() can return shorter chunks than
        # requested when the kernel's buffer underflows. We don't have
        # a direct status flag from the library, so increment every
        # time we observe a chunk smaller than `blocksize`. Surfaced
        # via xrun_count() for the status bar.
        self._dropped_callbacks = 0

    def start(self) -> None:
        if self._running:
            return
        sc = _get_sc()
        if self.speaker_name is None:
            speaker = sc.default_speaker()
        else:
            speaker = sc.get_speaker(self.speaker_name)
        # Loopback mic that mirrors the chosen speaker
        self._mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._running = True
        self._thread.start()
        print(f"[LoopbackCapture] Started — speaker: {speaker.name}, "
              f"{self.sample_rate}Hz, {self.channels}ch")

    def stop(self) -> None:
        if not self._running:
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._mic = None
        self._running = False
        print("[LoopbackCapture] Stopped.")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ------------------------------------------------------------------
    # CaptureSource protocol
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._running

    def xrun_count(self) -> int:
        return int(self._dropped_callbacks)

    def _run(self) -> None:
        if _IS_WIN:
            # S_OK (0) or S_FALSE (1) both fine; RPC_E_CHANGED_MODE (0x80010106)
            # means COM was already initialized with a different model — also ok.
            _ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        try:
            with self._mic.recorder(
                samplerate=self.sample_rate,
                channels=self.channels,
            ) as rec:
                while not self._stop_event.is_set():
                    chunk = rec.record(numframes=self.blocksize)
                    if chunk is None or len(chunk) == 0:
                        self._dropped_callbacks += 1
                        continue
                    if len(chunk) < self.blocksize:
                        # Short read — likely a kernel buffer underflow
                        self._dropped_callbacks += 1
                    # soundcard returns float32 [N, channels] — buffer.write is happy
                    self.buffer.write(chunk.astype(np.float32, copy=False))
                    if self.on_level:
                        rms = np.sqrt(np.mean(chunk ** 2, axis=0))
                        self.on_level(rms)
        finally:
            if _IS_WIN:
                _ole32.CoUninitialize()
