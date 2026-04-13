"""
BufferTrack — Track 1 composition.

Stacks a WaveformView (recessed screen) with a LevelMeter to its right
and a small bottom readout row showing buffered time / capacity / fill
percentage. Fed by the main window's 30 Hz tick — no internal timer.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from flashback_sampler.app.widgets.level_meter import LevelMeter
from flashback_sampler.app.widgets.waveform_view import WaveformView


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
    """

    def __init__(self, channels: int = 2, parent=None):
        super().__init__(parent)
        self._channels = channels
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # ── waveform + meter row ─────────────────────────────────────
        center_row = QHBoxLayout()
        center_row.setSpacing(6)
        self._waveform = WaveformView(self)
        self._waveform.setMinimumHeight(140)
        self._waveform.set_labels("LIVE BUFFER", "READY")
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
        """
        self._waveform.set_selection(start_frac, end_frac)

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


def _mmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = int(seconds - m * 60)
    return f"{m:02d}:{s:02d}"
