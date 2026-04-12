"""
AudioCircularBuffer - Core ring buffer for continuous audio retention.

Keeps the last N minutes of audio in memory, always.
Write pointer advances continuously; old data is silently overwritten.
"""

import numpy as np
import threading
import time
from typing import Optional, Tuple


class AudioCircularBuffer:
    """
    Ring buffer that holds audio data for a configurable duration.

    Memory layout:
        buffer[buffer_size, channels]  (float32)
        write_pos  -- current write head (0..buffer_size-1)
        total_written -- monotonically increasing sample count

    Thread-safe: all public methods acquire self._lock.
    """

    def __init__(
        self,
        duration_seconds: float = 900.0,   # 15 min default
        sample_rate: int = 48_000,
        channels: int = 2,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.duration = duration_seconds
        self.buffer_size = int(duration_seconds * sample_rate)

        # Pre-allocate entire buffer up front — no GC surprises later
        self.buffer = np.zeros((self.buffer_size, channels), dtype=np.float32)
        self.write_pos = 0          # next slot to write into
        self.total_written = 0      # ever-increasing, used to track fill level
        self._lock = threading.Lock()
        self._created_at = time.time()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def write(self, frames: np.ndarray) -> None:
        """
        Append `frames` (shape [N, channels]) to the ring buffer.
        Called from the audio callback thread — must be fast.
        """
        if frames.ndim == 1:
            frames = frames[:, np.newaxis]  # mono -> [N,1]

        n = len(frames)
        with self._lock:
            end = self.write_pos + n
            if end <= self.buffer_size:
                self.buffer[self.write_pos:end] = frames
            else:
                # Wrap-around write
                first = self.buffer_size - self.write_pos
                self.buffer[self.write_pos:] = frames[:first]
                self.buffer[:end - self.buffer_size] = frames[first:]
            self.write_pos = end % self.buffer_size
            self.total_written += n

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def get_latest(self, seconds: float) -> np.ndarray:
        """Return the most recent `seconds` of audio as [N, channels] float32."""
        n_want = int(seconds * self.sample_rate)
        with self._lock:
            n_avail = min(self.total_written, self.buffer_size)
            n = min(n_want, n_avail)
            start = (self.write_pos - n) % self.buffer_size
            if start < self.write_pos:
                return self.buffer[start:self.write_pos].copy()
            else:
                return np.concatenate([
                    self.buffer[start:],
                    self.buffer[:self.write_pos]
                ])

    def get_segment(self, start_ago: float, end_ago: float) -> np.ndarray:
        """
        Extract a segment defined by how many seconds ago each boundary is.
        Example: get_segment(300, 60)  → audio from 5 min ago to 1 min ago.

        start_ago > end_ago  (start is further back in time)
        """
        if start_ago <= end_ago:
            raise ValueError("start_ago must be greater than end_ago")

        with self._lock:
            n_avail = min(self.total_written, self.buffer_size)
            avail_secs = n_avail / self.sample_rate

            # Clamp to what's actually available
            start_ago = min(start_ago, avail_secs)
            end_ago = max(end_ago, 0.0)

            n_start = int(start_ago * self.sample_rate)
            n_end = int(end_ago * self.sample_rate)

            abs_start = (self.write_pos - n_start) % self.buffer_size
            abs_end = (self.write_pos - n_end) % self.buffer_size

            if abs_start < abs_end:
                return self.buffer[abs_start:abs_end].copy()
            else:
                return np.concatenate([
                    self.buffer[abs_start:],
                    self.buffer[:abs_end]
                ])

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @property
    def buffered_seconds(self) -> float:
        """How many seconds of audio are currently in the buffer."""
        return min(self.total_written, self.buffer_size) / self.sample_rate

    @property
    def is_full(self) -> bool:
        return self.total_written >= self.buffer_size

    def get_rms_levels(self, window_seconds: float = 0.1) -> np.ndarray:
        """RMS level per channel for the last window_seconds (for metering)."""
        audio = self.get_latest(window_seconds)
        if len(audio) == 0:
            return np.zeros(self.channels)
        return np.sqrt(np.mean(audio ** 2, axis=0))

    def status(self) -> dict:
        return {
            "buffered_seconds": round(self.buffered_seconds, 1),
            "buffer_capacity_seconds": self.duration,
            "fill_percent": round(100 * self.buffered_seconds / self.duration, 1),
            "write_pos": self.write_pos,
            "total_written_samples": self.total_written,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "memory_mb": round(self.buffer.nbytes / 1_048_576, 1),
        }
