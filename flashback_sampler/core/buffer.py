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

    # Pre-decimated summary ring: slots of SUMMARY_SLOT_SAMPLES raw
    # samples, each storing (min, max, sum_of_squares, count) per channel.
    # The writer populates each slot incrementally as raw samples arrive;
    # once a slot's SUMMARY_SLOT_SAMPLES budget fills, it freezes and the
    # writer moves on. Readers aggregate frozen slots into display bins —
    # visualisations see stable values because the summary is computed
    # ONCE from all of a slot's samples, not stride-sampled per read.
    # 4096 samples ≈ 85 ms at 48 kHz; fine enough for smooth rolling,
    # coarse enough that the summary stays tiny (≈330 KB for 15 min).
    _SUMMARY_SLOT_SAMPLES = 4096

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

        # Summary ring. Each slot's _sum_slot_abs[i] is the absolute
        # sample index of its FIRST sample for the current generation;
        # a slot is "fresh" iff that value matches the writer's current
        # cycle. -1 = empty / never written.
        n_sum = max(1, self.buffer_size // self._SUMMARY_SLOT_SAMPLES)
        self._n_sum = n_sum
        self._sum_min = np.zeros((n_sum, channels), dtype=np.float32)
        self._sum_max = np.zeros((n_sum, channels), dtype=np.float32)
        self._sum_ss = np.zeros((n_sum, channels), dtype=np.float64)
        self._sum_count = np.zeros(n_sum, dtype=np.int64)
        self._sum_slot_abs = np.full(n_sum, -1, dtype=np.int64)

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
            self._update_summary_locked(frames, self.total_written)
            self.total_written += n

    def _update_summary_locked(self, frames: np.ndarray, start_abs: int) -> None:
        """Update the pre-decimated summary ring. Called under self._lock
        during write(). A typical audio block (~480 samples) hits one or
        two summary slots; the loop runs 1–2 iterations and completes in
        microseconds so the writer's critical section stays tiny."""
        n = len(frames)
        if n == 0:
            return
        slot_size = self._SUMMARY_SLOT_SAMPLES
        n_sum = self._n_sum
        slot_first = start_abs // slot_size
        slot_last = (start_abs + n - 1) // slot_size
        for s_global in range(slot_first, slot_last + 1):
            slot_idx = s_global % n_sum
            slot_abs = s_global * slot_size
            f_from = max(0, slot_abs - start_abs)
            f_to = min(n, slot_abs + slot_size - start_abs)
            piece = frames[f_from:f_to]
            piece_sq = piece.astype(np.float64) ** 2
            if self._sum_slot_abs[slot_idx] != slot_abs:
                # New generation — overwrite rather than accumulate
                self._sum_min[slot_idx] = piece.min(axis=0)
                self._sum_max[slot_idx] = piece.max(axis=0)
                self._sum_ss[slot_idx] = piece_sq.sum(axis=0)
                self._sum_count[slot_idx] = len(piece)
                self._sum_slot_abs[slot_idx] = slot_abs
            else:
                self._sum_min[slot_idx] = np.minimum(self._sum_min[slot_idx], piece.min(axis=0))
                self._sum_max[slot_idx] = np.maximum(self._sum_max[slot_idx], piece.max(axis=0))
                self._sum_ss[slot_idx] += piece_sq.sum(axis=0)
                self._sum_count[slot_idx] += len(piece)

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
            self._sum_min.fill(0.0)
            self._sum_max.fill(0.0)
            self._sum_ss.fill(0.0)
            self._sum_count.fill(0)
            self._sum_slot_abs.fill(-1)

    def get_rms_levels(self, window_seconds: float = 0.1) -> np.ndarray:
        """RMS level per channel for the last window_seconds (for metering)."""
        audio = self.get_latest(window_seconds)
        if len(audio) == 0:
            return np.zeros(self.channels)
        return np.sqrt(np.mean(audio ** 2, axis=0))

    # Cap on samples-per-bin actually inspected when computing peaks. Above
    # this, we stride-sample the bin's range so the per-tick work stays
    # bounded regardless of buffer size. 256 samples × 360 bins × stereo
    # is ~46k inspected values — sub-millisecond — and keeps enough
    # detail that the rendered waveform looks the same to the eye.
    _PEAK_BINS_MAX_SAMPLES_PER_BIN = 256

    # Slack left below buffer_size when the ring is saturated. If a reader
    # snapshots abs_start = total_written - buffer_size (no slack), any
    # writer advance between snapshot and verify makes the tear check fire
    # — the oldest sample gets overwritten on the writer's very next wrap.
    # 4096 samples ≈ 85 ms at 48 kHz, imperceptible at the edge of a
    # 15-minute ring, and larger than a typical WASAPI period block so the
    # writer can tick several times during our read without invalidating.
    _PEAK_BINS_READ_HEADROOM = 4096

    def get_peak_bins(self, seconds: float, n_bins: int) -> np.ndarray:
        """
        Downsample the most recent `seconds` of audio into `n_bins` min/max
        pairs for waveform rendering.

        Returns float32 array of shape (n_bins, 2, channels) where
        result[i, 0, c] = min and result[i, 1, c] = max of channel `c` in
        bin `i`. An empty buffer returns a zero array of the requested shape.

        Reads ring storage directly via numpy views — does NOT copy the
        whole window into a new array. This matters at 30 Hz polling on
        a 15-min × 48 kHz × stereo ring (≈345 MB): a copy-based read
        would saturate memory bandwidth. Strided sub-sampling caps the
        worst-case bin work so the call stays sub-millisecond even when
        the ring is full.
        """
        if n_bins <= 0:
            raise ValueError("n_bins must be positive")
        out_shape = (n_bins, 2, self.channels)
        empty = np.zeros(out_shape, dtype=np.float32)

        bsize = self.buffer_size
        ring = self.buffer

        # Retry up to 3 times on tear, matching _copy_abs_range. Each
        # retry re-snapshots abs_start so the window rides the writer.
        for attempt in range(3):
            out = np.zeros(out_shape, dtype=np.float32)

            # Snapshot the source range under the lock. Iterate without it.
            with self._lock:
                n_avail = min(self.total_written, self.buffer_size)
                # On a production-size ring, always leave headroom below
                # buffer_size so the writer can advance during iteration
                # without tripping the tear path. Consistent n across
                # consecutive calls is what keeps the stride-aligned
                # sample positions stable — mixing "first-attempt n=bsize"
                # with "retry n=bsize-headroom" frame-to-frame shifts each
                # bin's abs range by thousands of samples and re-rotates
                # which stride-aligned positions fall inside it.
                # Only the small-ring test case (bsize ≤ 2 × headroom)
                # skips the reduction so single-threaded unit tests can
                # still read the entire buffer.
                if (
                    n_avail >= bsize
                    and bsize > 2 * self._PEAK_BINS_READ_HEADROOM
                ):
                    n_avail = bsize - self._PEAK_BINS_READ_HEADROOM
                n = min(int(seconds * self.sample_rate), n_avail)
                if n <= 0:
                    return empty
                abs_start = self.total_written - n
                ring_start = abs_start % bsize

            edges = np.linspace(0, n, n_bins + 1, dtype=np.int64)
            span_ref = int(edges[1]) - int(edges[0])
            stride = max(1, span_ref // self._PEAK_BINS_MAX_SAMPLES_PER_BIN)

            # The stride grid is anchored to ABSOLUTE sample indices, not
            # to the bin's position in the rolling window — whether audio
            # sample at abs index Y is picked depends on Y alone. Rolling
            # the window by a few samples per tick therefore does not
            # rotate the sampled subset, so bin peaks stay stable across
            # ticks instead of flickering as stride-offset groups cycle.

            if stride == 1:
                # Small window / small ring — no sampling needed. Use
                # per-bin slicing with wrap handling.
                for i in range(n_bins):
                    a = int(edges[i])
                    b = int(edges[i + 1])
                    if b <= a:
                        if i > 0:
                            out[i] = out[i - 1]
                        continue
                    ra = (ring_start + a) % bsize
                    rb = ra + (b - a)
                    if rb <= bsize:
                        chunk = ring[ra:rb]
                    else:
                        chunk = np.concatenate([ring[ra:], ring[:rb - bsize]])
                    out[i, 0] = chunk.min(axis=0)
                    out[i, 1] = chunk.max(axis=0)
            else:
                # Vectorized stride sampling: build one (n_bins, k) matrix
                # of ring positions, fancy-index once, then reduce along
                # the sample axis. Bin peaks stay stable because first_abs
                # only shifts in stride-size steps as the window rolls.
                # k sized to fully cover span_ref regardless of alignment.
                # Tail bins may pull 1–2 positions from the next bin via
                # modular wrap — a sub-percent overlap with no visible
                # effect on a rolling waveform.
                k = span_ref // stride + 1
                abs_bin_starts = abs_start + edges[:n_bins]
                # Smallest multiple of stride >= abs_bin_start.
                first_abs = -(-abs_bin_starts // stride) * stride
                offsets = np.arange(k, dtype=np.int64) * stride
                positions = (first_abs[:, None] + offsets[None, :]) % bsize
                chunks = ring[positions]  # (n_bins, k, channels)
                out[:, 0] = chunks.min(axis=1)
                out[:, 1] = chunks.max(axis=1)

            # Seqlock-style verify: if the writer has lapped our slice while
            # we were iterating, the data we read may be torn. Retry with a
            # fresh snapshot rather than returning torn data.
            with self._lock:
                if self.total_written - abs_start <= bsize:
                    return out
            # Lapped — retry with fresh snapshot.

        # Three attempts failed — return empty rather than torn data.
        return empty

    def get_summary_bins(
        self,
        n_bins: int,
        seconds: float | None = None,
        bin_span_samples: int | None = None,
    ) -> np.ndarray:
        """
        Return (n_bins, channels) RMS amplitude values derived from the
        pre-decimated summary ring.

        bin_span_samples: width of one display bin in raw samples. When
        None (default), bin_span is computed from the current window —
        convenient but bin positions shift every tick during unsaturated
        fill because both `n_samples` and `n_bins` grow asymmetrically,
        which visibly re-aggregates historical bars (user sees pulsing).
        Pass an explicit value (e.g. ``buffer_size // n_bins``) to pin
        bin boundaries to a capacity-based grid so each slot's bin
        assignment is fixed for its lifetime.

        Why this exists alongside get_peak_bins: for visualisations that
        render one bar per bin (e.g. radial waveform around a turntable),
        stride-sampling the raw ring each frame produces extreme-value
        estimates that wobble frame-to-frame. The summary ring instead
        computes each slot's stats ONCE from ALL its samples as the
        writer appends, so readers see stable numbers — only the bin
        holding the actively-growing newest slot changes between ticks.

        Aggregates frozen summary slots into n_bins display bins via
        scatter-add. Cost: O(n_sum) per call, a single numpy pass.
        """
        if n_bins <= 0:
            raise ValueError("n_bins must be positive")
        out = np.zeros((n_bins, self.channels), dtype=np.float32)

        with self._lock:
            tw = self.total_written
            n_avail = min(tw, self.buffer_size)
            if seconds is None:
                n_samples = n_avail
            else:
                n_samples = min(int(seconds * self.sample_rate), n_avail)
            if n_samples <= 0:
                return out
            abs_start = tw - n_samples
            sum_ss = self._sum_ss.copy()
            sum_count = self._sum_count.copy()
            sum_slot_abs = self._sum_slot_abs.copy()

        # Aggregate frozen slots into display bins outside the lock.
        # Only slots whose abs-start falls inside our window contribute.
        valid = (sum_slot_abs >= abs_start) & (sum_slot_abs < abs_start + n_samples)
        if not valid.any():
            return out
        bin_span = (
            float(bin_span_samples)
            if bin_span_samples is not None and bin_span_samples > 0
            else n_samples / n_bins
        )
        bin_idx = ((sum_slot_abs - abs_start) / bin_span).astype(np.int32)
        np.clip(bin_idx, 0, n_bins - 1, out=bin_idx)

        bin_ss = np.zeros((n_bins, self.channels), dtype=np.float64)
        bin_cnt = np.zeros(n_bins, dtype=np.int64)
        np.add.at(bin_ss, bin_idx[valid], sum_ss[valid])
        np.add.at(bin_cnt, bin_idx[valid], sum_count[valid])

        nonzero = bin_cnt > 0
        if nonzero.any():
            out[nonzero] = np.sqrt(
                bin_ss[nonzero] / bin_cnt[nonzero, None]
            ).astype(np.float32)
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
