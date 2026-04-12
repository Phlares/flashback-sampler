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
        # Snapshot state under lock — just indices, no copy.
        with self._lock:
            n_avail = min(self.total_written, self.buffer_size)
            n = min(n_want, n_avail)
            if n == 0:
                return np.zeros((0, self.channels), dtype=np.float32)
            total_snapshot = self.total_written
            abs_end = total_snapshot
            abs_start = abs_end - n
        return self._copy_abs_range(abs_start, abs_end)

    def get_segment(self, start_ago: float, end_ago: float) -> np.ndarray:
        """
        Extract a segment defined by how many seconds ago each boundary is.
        Example: get_segment(300, 60)  → audio from 5 min ago to 1 min ago.

        start_ago > end_ago  (start is further back in time)
        """
        if start_ago <= end_ago:
            raise ValueError("start_ago must be greater than end_ago")

        # Snapshot state under lock — just indices, no copy.
        with self._lock:
            n_avail = min(self.total_written, self.buffer_size)
            avail_secs = n_avail / self.sample_rate
            start_ago = min(start_ago, avail_secs)
            end_ago = max(end_ago, 0.0)
            n_start = int(start_ago * self.sample_rate)
            n_end = int(end_ago * self.sample_rate)
            span = n_start - n_end
            if span <= 0:
                return np.zeros((0, self.channels), dtype=np.float32)
            total_snapshot = self.total_written
            abs_end = total_snapshot - n_end
            abs_start = total_snapshot - n_start
        return self._copy_abs_range(abs_start, abs_end)

    # ------------------------------------------------------------------
    # Seqlock-style non-blocking read
    # ------------------------------------------------------------------

    def _copy_abs_range(self, abs_start: int, abs_end: int) -> np.ndarray:
        """
        Copy samples from the ring by absolute sample index (total_written
        space), WITHOUT holding the writer lock during the memcpy.

        Algorithm:
          1. Under lock, snapshot total_written, compute ring indices,
             release lock.
          2. Copy from the ring outside the lock. Writer may advance.
          3. Re-acquire the lock briefly and verify the writer has not
             lapped the slice start (i.e. not written more than
             buffer_size - span samples since step 1).
          4. If lapped, retry up to 3 times. Otherwise return the copy.

        The writer's critical section stays tiny — just the index-update
        and a small-block memcpy in write() — so even multi-megabyte reads
        do not stall audio capture.
        """
        n = abs_end - abs_start
        if n <= 0:
            return np.zeros((0, self.channels), dtype=np.float32)

        for _ in range(3):
            # Step 1: snapshot indices under lock
            with self._lock:
                current = self.total_written
                if abs_end > current:
                    # Range extends past what's written — empty.
                    return np.zeros((0, self.channels), dtype=np.float32)
                if current - abs_start > self.buffer_size:
                    # Slice already fully overwritten — empty.
                    return np.zeros((0, self.channels), dtype=np.float32)
                ring_start = abs_start % self.buffer_size
                ring_end = abs_end % self.buffer_size

            # Step 2: copy outside the lock (this is the expensive part)
            if n == self.buffer_size:
                # Full ring — ring_start and ring_end coincide; split at start.
                chunk = np.concatenate([
                    self.buffer[ring_start:].copy(),
                    self.buffer[:ring_start].copy(),
                ])
            elif ring_end > ring_start:
                chunk = self.buffer[ring_start:ring_end].copy()
            else:
                chunk = np.concatenate([
                    self.buffer[ring_start:].copy(),
                    self.buffer[:ring_end].copy(),
                ])

            # Step 3: verify the writer did not lap our slice
            with self._lock:
                if self.total_written - abs_start <= self.buffer_size:
                    return chunk
            # Step 4: lapped — retry

        # Gave up after 3 attempts — return empty rather than stale/torn data.
        return np.zeros((0, self.channels), dtype=np.float32)

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

    def flush(self) -> None:
        """
        Discard all currently buffered audio. Resets write_pos and
        total_written to zero and zeros out the ring.

        Does NOT affect any Checkout snapshots already taken — those live
        in their own ndarrays and are immutable after creation. Only the
        ring's own contents are cleared. Safe to call during capture:
        the writer's next write() will start filling from index 0 again.
        """
        with self._lock:
            self.buffer.fill(0.0)
            self.write_pos = 0
            self.total_written = 0

    def get_rms_levels(self, window_seconds: float = 0.1) -> np.ndarray:
        """RMS level per channel for the last window_seconds (for metering)."""
        audio = self.get_latest(window_seconds)
        if len(audio) == 0:
            return np.zeros(self.channels)
        return np.sqrt(np.mean(audio ** 2, axis=0))

    def get_peak_bins(self, seconds: float, n_bins: int) -> np.ndarray:
        """
        Downsample the most recent `seconds` of audio into `n_bins` min/max
        pairs for waveform rendering.

        Returns float32 array of shape (n_bins, 2, channels) where
        result[i, 0, c] = min and result[i, 1, c] = max of channel `c` in
        bin `i`. An empty buffer returns a zero array of the requested shape.
        """
        if n_bins <= 0:
            raise ValueError("n_bins must be positive")
        out = np.zeros((n_bins, 2, self.channels), dtype=np.float32)
        audio = self.get_latest(seconds)
        n = len(audio)
        if n == 0:
            return out
        # Distribute samples across bins as evenly as possible. Use integer
        # slicing boundaries computed via linspace so the last bin always
        # covers to the end.
        edges = np.linspace(0, n, n_bins + 1, dtype=np.int64)
        for i in range(n_bins):
            a, b = int(edges[i]), int(edges[i + 1])
            if b <= a:
                # Fewer samples than bins — repeat the last value pair
                if i > 0:
                    out[i] = out[i - 1]
                continue
            chunk = audio[a:b]
            out[i, 0] = chunk.min(axis=0)
            out[i, 1] = chunk.max(axis=0)
        return out

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
