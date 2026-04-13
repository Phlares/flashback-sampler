"""
BufferTrack — Track 1 composition.

Stacks a WaveformView (recessed screen) with a LevelMeter to its right
and a small bottom readout row showing buffered time / capacity / fill
percentage. Fed by the main window's 30 Hz tick — no internal timer.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from flashback_sampler.app.widgets.level_meter import LevelMeter
from flashback_sampler.app.widgets.selectable_waveform import SelectableWaveform


def compute_anchor_section(
    anchor_offset_s: float,
    duration_s: float,
    buffered_s: float,
) -> tuple[float, float] | None:
    """
    Compute (start_frac, end_frac) for the prospective checkout band on
    the live buffer waveform. Both fractions are in [0, 1] where 0 is
    the oldest buffered sample and 1 is the current head ("now").

    Returns None when there is nothing meaningful to show — buffer empty,
    degenerate duration, or start/end collapse.
    """
    if buffered_s <= 0.0 or duration_s <= 0.0:
        return None
    end_frac = 1.0 - (anchor_offset_s / buffered_s)
    start_frac = 1.0 - ((anchor_offset_s + duration_s) / buffered_s)
    end_frac = max(0.0, min(1.0, end_frac))
    start_frac = max(0.0, min(1.0, start_frac))
    if end_frac <= start_frac:
        return None
    return (start_frac, end_frac)


class BufferTrack(QWidget):
    """
    Track 1 — always-visible live ring buffer display.

    Use:
        track = BufferTrack(channels=2)
        track.update_waveform(bins)          # (n_bins, 2, channels)
        track.update_levels([rms_l, rms_r])  # np.ndarray or list
        track.update_readouts(
            buffered_s=12.3, capacity_s=900, sample_rate=48_000,
            channels=2, device_name="Loopback (Default Speaker)",
        )

    Manual selection:
        The embedded waveform view supports drag-to-select. BufferTrack
        translates the drag fractions into absolute sample positions
        (pinned via the AudioCircularBuffer.total_written snapshot at
        the moment of selection commit) so the highlighted band stays
        anchored to real audio as the ring scrolls. The host controller
        connects to manualSelectionCommitted / manualSelectionCleared /
        contextMenuRequested and handles the context menu population.
    """

    manualSelectionCommitted = Signal(int, int)  # abs_start, abs_end
    manualSelectionCleared = Signal()
    contextMenuRequested = Signal(QPointF)

    def __init__(self, channels: int = 2, parent=None):
        super().__init__(parent)
        self._channels = channels
        # Absolute-sample pinning state for the manual selection. When
        # the user drags-and-releases, we snapshot total_written and
        # compute abs positions. On subsequent ticks we translate back
        # to fractions given the current buffered span so the band
        # tracks the audio it pointed to originally.
        self._sel_abs_start: int | None = None
        self._sel_abs_end: int | None = None
        self._sel_sample_rate: int = 48_000
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # ── waveform + meter row ─────────────────────────────────────
        center_row = QHBoxLayout()
        center_row.setSpacing(6)
        self._waveform = SelectableWaveform(self)
        self._waveform.setMinimumHeight(140)
        self._waveform.set_labels("LIVE BUFFER", "READY")
        self._waveform.manualSelectionChanged.connect(
            self._on_manual_selection_changed
        )
        self._waveform.manualSelectionCleared.connect(
            self._on_manual_selection_cleared
        )
        self._waveform.contextMenuRequested.connect(
            self.contextMenuRequested.emit
        )
        center_row.addWidget(self._waveform, 1)

        self._meter = LevelMeter(channels=self._channels, parent=self)
        self._meter.setFixedWidth(28 if self._channels == 2 else 18)
        self._meter.setMinimumHeight(140)
        center_row.addWidget(self._meter, 0)

        root.addLayout(center_row, 1)

        # ── bottom readout row ───────────────────────────────────────
        bottom = QHBoxLayout()
        bottom.setSpacing(16)

        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setProperty("role", "label")
        bottom.addWidget(self._time_label)

        self._fill_label = QLabel("FILL   0.0%")
        self._fill_label.setProperty("role", "label")
        bottom.addWidget(self._fill_label)

        bottom.addStretch(1)

        self._dev_label = QLabel("")
        self._dev_label.setProperty("role", "label")
        bottom.addWidget(self._dev_label)

        root.addLayout(bottom, 0)

    # ------------------------------------------------------------------
    # Data in
    # ------------------------------------------------------------------

    def update_waveform(self, bins: np.ndarray | None) -> None:
        self._waveform.set_data(bins)

    def set_anchor_playhead(self, frac: float | None) -> None:
        """
        DEPRECATED — use set_anchor_section instead. Kept as a thin
        wrapper so older callers don't break.
        """
        self._waveform.set_playhead(frac)

    def set_anchor_section(
        self,
        start_frac: float | None,
        end_frac: float | None,
    ) -> None:
        """
        Highlight the [start_frac, end_frac] band on the live waveform —
        the prospective checkout range. Pass None / None to hide.
        The anchor band is suppressed whenever a manual drag-selection
        is active so the two don't fight for visual attention.
        """
        if self.has_manual_selection():
            self._waveform.set_selection(None, None)
        else:
            self._waveform.set_selection(start_frac, end_frac)

    # ------------------------------------------------------------------
    # Manual selection — absolute-sample pinning
    # ------------------------------------------------------------------

    def has_manual_selection(self) -> bool:
        return self._sel_abs_start is not None and self._sel_abs_end is not None

    def manual_selection_abs_range(self) -> tuple[int, int] | None:
        if not self.has_manual_selection():
            return None
        return (int(self._sel_abs_start), int(self._sel_abs_end))

    def clear_manual_selection(self) -> None:
        self._sel_abs_start = None
        self._sel_abs_end = None
        self._waveform.clear_manual_selection()

    def sync_manual_selection_to_buffer(
        self,
        buffered_s: float,
        total_written: int,
        sample_rate: int,
    ) -> None:
        """
        Called by the host controller on every tick. Translates the
        pinned absolute-sample selection back into the fraction space
        of the current visible waveform, and hides the overlay if the
        selection has scrolled entirely out of the ring.
        """
        self._sel_sample_rate = sample_rate
        if not self.has_manual_selection():
            return

        # Newest visible sample is total_written; oldest is
        # total_written - buffered_s * sample_rate.
        oldest = total_written - int(buffered_s * sample_rate)
        span = max(1, total_written - oldest)

        # If the whole selection has rolled off the back of the ring,
        # drop the pin and notify the widget.
        if self._sel_abs_end <= oldest:
            self.clear_manual_selection()
            self.manualSelectionCleared.emit()
            return

        # Clamp the start to the oldest visible sample so the band
        # hugs the left edge as the selection exits.
        s_abs = max(self._sel_abs_start, oldest)
        e_abs = min(self._sel_abs_end, total_written)
        if e_abs <= s_abs:
            self.clear_manual_selection()
            self.manualSelectionCleared.emit()
            return

        start_frac = (s_abs - oldest) / span
        end_frac = (e_abs - oldest) / span
        self._waveform.set_manual_selection(
            max(0.0, min(1.0, start_frac)),
            max(0.0, min(1.0, end_frac)),
        )

    # ------------------------------------------------------------------
    # Signal plumbing from the embedded SelectableWaveform
    # ------------------------------------------------------------------

    def _on_manual_selection_changed(
        self, start_frac: float, end_frac: float
    ) -> None:
        """
        User released a drag. Translate fractions → absolute samples
        using the most recent tick state, then emit the controller-
        facing signal so the host can pop up a context menu / offer
        a check-out action.
        """
        # The waveform needs the host to supply the current buffered
        # seconds / total_written; we cache them via set_anchor_section
        # and friends. Easiest: read from the parent's latest tick
        # state via callback. For now, use the waveform's painted data
        # to estimate via sample_rate; the final commit comes from the
        # host which snapshots total_written authoritatively.
        #
        # The actual abs-sample calculation lives in the host
        # (_pin_manual_selection below) — here we just forward the
        # fractional info via the parent signal contract.
        # HOWEVER we also need to emit manualSelectionCommitted with
        # abs_start/end. The host must call sync_manual_selection_to_buffer
        # first or provide total_written. We resolve this by storing
        # the frac pair and emitting on the NEXT buffer update the host
        # provides via pin_manual_selection.
        self._pending_start_frac = float(start_frac)
        self._pending_end_frac = float(end_frac)

    def _on_manual_selection_cleared(self) -> None:
        self._sel_abs_start = None
        self._sel_abs_end = None
        self._pending_start_frac = None
        self._pending_end_frac = None
        self.manualSelectionCleared.emit()

    def pin_manual_selection(
        self,
        buffered_s: float,
        total_written: int,
        sample_rate: int,
    ) -> None:
        """
        Called by the host when it sees a pending manual selection
        (set via _on_manual_selection_changed but not yet pinned to
        abs samples). Converts the stored fractions into absolute
        sample positions using the host's authoritative buffer state
        and emits manualSelectionCommitted.
        """
        if not hasattr(self, "_pending_start_frac"):
            return
        start_frac = getattr(self, "_pending_start_frac", None)
        end_frac = getattr(self, "_pending_end_frac", None)
        if start_frac is None or end_frac is None:
            return
        if sample_rate <= 0 or buffered_s <= 0:
            return

        oldest = total_written - int(buffered_s * sample_rate)
        span = max(1, total_written - oldest)
        abs_start = int(oldest + start_frac * span)
        abs_end = int(oldest + end_frac * span)
        if abs_end <= abs_start:
            return

        self._sel_abs_start = abs_start
        self._sel_abs_end = abs_end
        self._pending_start_frac = None
        self._pending_end_frac = None
        self.manualSelectionCommitted.emit(abs_start, abs_end)

    def update_levels(self, rms_per_channel) -> None:
        self._meter.set_levels(rms_per_channel)

    def update_readouts(
        self,
        *,
        buffered_s: float,
        capacity_s: float,
        sample_rate: int,
        channels: int,
        device_name: str,
    ) -> None:
        self._time_label.setText(
            f"{_mmss(buffered_s)} / {_mmss(capacity_s)}"
        )
        pct = 100.0 * buffered_s / capacity_s if capacity_s else 0.0
        self._fill_label.setText(f"FILL  {pct:5.1f}%")
        self._dev_label.setText(
            f"{sample_rate // 1000}K / {channels}CH    {device_name.upper()}"
        )
        self._waveform.set_labels(
            "LIVE BUFFER",
            f"{sample_rate // 1000}K   {channels}CH",
        )
        # Timeline: "NOW" at the right edge, negative offsets into the
        # buffered history to the left. Span = what's actually in the
        # ring, not the capacity, so labels land on real audio.
        self._waveform.set_timeline(total_seconds=buffered_s, anchor="right")


def _mmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = int(seconds - m * 60)
    return f"{m:02d}:{s:02d}"
