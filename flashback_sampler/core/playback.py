"""
AudioPlayback & Exporter — Read from buffer, play back, or save to disk.
"""

import numpy as np
import soundfile as sf
import threading
import time
from pathlib import Path
from typing import Optional
from .buffer import AudioCircularBuffer

sd = None
def _get_sd():
    global sd
    if sd is None:
        import sounddevice as _sd
        sd = _sd
    return sd


class AudioPlayback:
    """
    Plays audio segments from an AudioCircularBuffer or a raw numpy array.
    Non-blocking: playback runs in a background thread.
    """

    def __init__(self, sample_rate: int = 48_000, channels: int = 2,
                 device: Optional[int] = None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.device = device
        self._stream: Optional[sd.OutputStream] = None
        self._thread: Optional[threading.Thread] = None
        self.is_playing = False

    def play(self, audio: np.ndarray, blocking: bool = False) -> None:
        """Play a numpy array [N, channels] float32."""
        if self.is_playing:
            self.stop()
        self._thread = threading.Thread(target=self._play_worker,
                                        args=(audio,), daemon=True)
        self._thread.start()
        if blocking:
            self._thread.join()

    def play_from_buffer(self, buf: AudioCircularBuffer,
                         seconds: float = 30.0, blocking: bool = False) -> None:
        """Pull the last `seconds` from buffer and play."""
        audio = buf.get_latest(seconds)
        print(f"[Playback] Playing {len(audio)/self.sample_rate:.1f}s "
              f"({len(audio)} samples)")
        self.play(audio, blocking=blocking)

    def play_segment(self, buf: AudioCircularBuffer,
                     start_ago: float, end_ago: float,
                     blocking: bool = False) -> None:
        """Play buf.get_segment(start_ago, end_ago)."""
        audio = buf.get_segment(start_ago, end_ago)
        dur = len(audio) / self.sample_rate
        print(f"[Playback] Playing segment {start_ago:.0f}s→{end_ago:.0f}s ago "
              f"({dur:.1f}s)")
        self.play(audio, blocking=blocking)

    def stop(self) -> None:
        self.is_playing = False
        if self._thread:
            self._thread.join(timeout=1.0)

    def _play_worker(self, audio: np.ndarray) -> None:
        self.is_playing = True
        try:
            sd = _get_sd()
            sd.play(audio, samplerate=self.sample_rate, device=self.device)
            sd.wait()
        finally:
            self.is_playing = False


class AudioExporter:
    """
    Save segments from the ring buffer to disk.
    Supports WAV (lossless) and FLAC. Use WAV for maximum compatibility.
    """

    @staticmethod
    def save_latest(buf: AudioCircularBuffer, seconds: float,
                    path: str | Path, fmt: str = "WAV") -> Path:
        """Save the most recent `seconds` to a file."""
        audio = buf.get_latest(seconds)
        return AudioExporter._write(audio, buf.sample_rate, path, fmt)

    @staticmethod
    def save_segment(buf: AudioCircularBuffer,
                     start_ago: float, end_ago: float,
                     path: str | Path, fmt: str = "WAV") -> Path:
        """Save buf.get_segment(start_ago, end_ago) to a file."""
        audio = buf.get_segment(start_ago, end_ago)
        return AudioExporter._write(audio, buf.sample_rate, path, fmt)

    @staticmethod
    def _write(audio: np.ndarray, sr: int,
               path: str | Path, fmt: str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(path), audio, sr, format=fmt)
        size_kb = path.stat().st_size / 1024
        dur = len(audio) / sr
        print(f"[Export] Saved {dur:.1f}s → {path}  ({size_kb:.0f} KB)")
        return path

    @staticmethod
    def generate_filename(prefix: str = "capture", ext: str = "wav") -> str:
        ts = time.strftime("%Y%m%d_%H%M%S")
        return f"{prefix}_{ts}.{ext}"
