"""
MixedCaptureSource — mux multiple capture inputs into a single ring
buffer at the same bitrate. Lets users with limited RAM run several
sources worth of audio through one slot instead of allocating a full
ring per source.

Architecture:
    sub-source A ──► 2 s staging ring A ─┐
    sub-source B ──► 2 s staging ring B ─┼─► mixer thread ─► target ring
    sub-source C ──► 2 s staging ring C ─┘   (sum, clip [-1,1], write)

Each sub-source writes into its own small AudioCircularBuffer. A
background mixer thread polls every ~10 ms, reads whatever's
available from every staging ring (bounded by the slowest sub so
no source is ever skipped), sums them sample-for-sample, hard-clips
to [-1, 1], and writes the result to the shared target buffer.

All sub-sources must share the target's sample_rate and channel count.
Mixing does no level compensation — callers expecting N sources
should plan for roughly 1/N pre-mix gain to avoid clipping.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np

from flashback_sampler.core.buffer import AudioCircularBuffer
from flashback_sampler.core.capture_source import CaptureSource


class MixedCaptureSource:
    """Multi-input CaptureSource. Satisfies the CaptureSource protocol
    so the rest of the pipeline can treat a muxed slot exactly like a
    single-source slot."""

    STAGING_SECONDS: float = 2.0
    MIX_POLL_SECONDS: float = 0.01  # 10 ms

    def __init__(
        self,
        target_buffer: AudioCircularBuffer,
        sub_factories: list[Callable[[AudioCircularBuffer], CaptureSource]],
        sample_rate: int,
        channels: int,
    ):
        if not sub_factories:
            raise ValueError("MixedCaptureSource needs at least one sub-factory")
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self._target = target_buffer

        # One staging ring + one sub-source per factory.
        self._stages: list[AudioCircularBuffer] = []
        self._subs: list[CaptureSource] = []
        # Read position (in target absolute-sample space) per staging
        # ring — tracks how much of each we've already consumed.
        self._read_positions: list[int] = []
        for factory in sub_factories:
            stage = AudioCircularBuffer(
                duration_seconds=self.STAGING_SECONDS,
                sample_rate=self.sample_rate,
                channels=self.channels,
            )
            sub = factory(stage)
            self._stages.append(stage)
            self._subs.append(sub)
            self._read_positions.append(0)

        self._running: bool = False
        self._stop_event = threading.Event()
        self._mix_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # CaptureSource protocol
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        for sub in self._subs:
            sub.start()
        self._mix_thread = threading.Thread(
            target=self._mix_loop, daemon=True, name="mixed-capture"
        )
        self._mix_thread.start()
        self._running = True

    def stop(self) -> None:
        if not self._running and self._mix_thread is None:
            return
        self._stop_event.set()
        for sub in self._subs:
            try:
                sub.stop()
            except Exception:  # pragma: no cover — best effort
                pass
        if self._mix_thread is not None:
            self._mix_thread.join(timeout=1.0)
            self._mix_thread = None
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def xrun_count(self) -> int:
        return sum(sub.xrun_count() for sub in self._subs)

    def last_error(self) -> str | None:
        for sub in self._subs:
            fn = getattr(sub, "last_error", None)
            if fn is None:
                continue
            try:
                err = fn()
            except Exception:  # pragma: no cover
                continue
            if err:
                return err
        return None

    # ------------------------------------------------------------------
    # Mixer loop
    # ------------------------------------------------------------------

    def _mix_loop(self) -> None:
        """Runs on a background thread. Every poll, reads the minimum
        available samples across all staging rings (so no sub ever gets
        skipped) and writes the sum into the target. Clipping to [-1, 1]
        happens after summing so sub-sources that push the sum over
        range still produce valid float32 audio.

        Staleness recovery: if any sub drops behind far enough that its
        staging ring has already overwritten our read position, the
        read position is fast-forwarded to the oldest still-valid
        sample. Without this, a single xrun on one sub would make the
        mixer wait forever for data that no longer exists and nothing
        would reach the target buffer — the external symptom being
        'only one sub seems to record'.
        """
        max_chunk = int(0.05 * self.sample_rate)
        diag_last_ts = time.monotonic()
        diag_counts = [0] * len(self._subs)
        while not self._stop_event.is_set():
            # Before picking n, pull every read position forward if the
            # staging ring has lapped it. Losing the pre-lap data is
            # better than blocking the whole mix indefinitely.
            for i, stage in enumerate(self._stages):
                oldest = max(
                    0, int(stage.total_written) - int(stage.buffer_size)
                )
                if self._read_positions[i] < oldest:
                    self._read_positions[i] = oldest

            avail_list = [
                int(stage.total_written) - int(read_pos)
                for stage, read_pos in zip(self._stages, self._read_positions)
            ]
            n = min(avail_list) if avail_list else 0
            if n <= 0:
                time.sleep(self.MIX_POLL_SECONDS)
                continue
            n = min(n, max_chunk)

            mixed: np.ndarray | None = None
            for i, (stage, read_pos) in enumerate(
                zip(self._stages, self._read_positions)
            ):
                segment = stage._copy_abs_range(read_pos, read_pos + n)  # noqa: SLF001
                if segment.shape[0] == 0:
                    # Rare: writer lapped us between the stale-fixup
                    # above and the copy. Reset on next iteration.
                    mixed = None
                    break
                if mixed is None:
                    mixed = segment.astype(np.float32, copy=True)
                else:
                    if segment.shape != mixed.shape:
                        mixed = None
                        break
                    mixed += segment
                self._read_positions[i] = read_pos + n
                diag_counts[i] += n

            if mixed is None:
                time.sleep(self.MIX_POLL_SECONDS)
                continue

            np.clip(mixed, -1.0, 1.0, out=mixed)
            self._target.write(mixed)

            # Diagnostic: every 2 s print per-sub sample counts so the
            # developer can tell at a glance whether all sources are
            # actually producing. Cheap (one print per couple seconds).
            now = time.monotonic()
            if now - diag_last_ts >= 2.0:
                written = [int(s.total_written) for s in self._stages]
                print(
                    f"[MixedCapture] subs_written={written} "
                    f"mixed_to_target={list(diag_counts)}"
                )
                diag_last_ts = now
                diag_counts = [0] * len(self._subs)
