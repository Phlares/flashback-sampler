"""
AudioCapture - Continuously feeds audio from any input device into the buffer.

On Windows:  uses WASAPI (exclusive or shared).
             For system/loopback audio, pass device name containing 'Loopback'
             or use wasapi_exclusive=False with a loopback-capable device.
On Linux/Pi: uses ALSA via PortAudio — same API, no changes needed.
"""

import numpy as np
import threading
import time
from typing import Optional, Callable
from .buffer import AudioCircularBuffer

# Lazy import — only fails at runtime if PortAudio isn't installed,
# not at module import time (lets buffer.py be used standalone in tests).
sd = None
def _get_sd():
    global sd
    if sd is None:
        import sounddevice as _sd
        sd = _sd
    return sd


class AudioCapture:
    """
    Wraps a sounddevice InputStream and pipes frames into an AudioCircularBuffer.

    Usage:
        buf = AudioCircularBuffer(duration_seconds=900)
        cap = AudioCapture(buf, device=None)  # None = system default input
        cap.start()
        ...
        cap.stop()
    """

    def __init__(
        self,
        buffer: AudioCircularBuffer,
        device: Optional[int | str] = None,   # None = default device
        sample_rate: int = 48_000,
        channels: int = 2,
        blocksize: int = 1024,                 # frames per callback ~21ms
        on_level: Optional[Callable] = None,   # optional metering callback
        extra_settings: Optional[object] = None,  # e.g. sd.WasapiSettings(loopback=True)
    ):
        self.buffer = buffer
        self.device = device
        self.sample_rate = sample_rate
        self.channels = channels
        self.blocksize = blocksize
        self.on_level = on_level
        self.extra_settings = extra_settings

        self._stream: Optional[sd.InputStream] = None
        self._running = False
        self._dropped_callbacks = 0

    # ------------------------------------------------------------------
    # Stream lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        sd = _get_sd()
        self._stream = sd.InputStream(
            device=self.device,
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            blocksize=self.blocksize,
            callback=self._callback,
            latency="low",
            extra_settings=self.extra_settings,
        )
        self._stream.start()
        self._running = True
        print(f"[AudioCapture] Started — device: {self._get_device_name()}, "
              f"{self.sample_rate}Hz, {self.channels}ch")

    def stop(self) -> None:
        if not self._running:
            return
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self._running = False
        print("[AudioCapture] Stopped.")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ------------------------------------------------------------------
    # Callback (runs on PortAudio thread — keep it lean)
    # ------------------------------------------------------------------

    def _callback(self, indata: np.ndarray, frames: int,
                   time_info, status) -> None:
        if status:
            self._dropped_callbacks += 1
            # Don't print here — that can cause xruns
        self.buffer.write(indata.copy())
        if self.on_level:
            rms = np.sqrt(np.mean(indata ** 2, axis=0))
            self.on_level(rms)

    # ------------------------------------------------------------------
    # Device helpers
    # ------------------------------------------------------------------

    def _get_device_name(self) -> str:
        sd = _get_sd()
        if self.device is None:
            info = sd.query_devices(kind="input")
        elif isinstance(self.device, int):
            info = sd.query_devices(self.device)
        else:
            return str(self.device)
        return info.get("name", "unknown") if isinstance(info, dict) else "unknown"

    @staticmethod
    def list_devices() -> None:
        """Print all available audio devices."""
        print(_get_sd().query_devices())

    @staticmethod
    def find_device(keyword: str) -> Optional[int]:
        """Find a device index by name substring (case-insensitive)."""
        devices = _get_sd().query_devices()
        for i, dev in enumerate(devices):
            if keyword.lower() in dev["name"].lower():
                return i
        return None
