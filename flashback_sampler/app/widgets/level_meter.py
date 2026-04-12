"""
LevelMeter — vertical dBFS strip with discrete thermal segments.

This is the single place in the Erebus system where the thermal gradient
lives. RMS-to-segment math is a pure function so it can be unit-tested
without a running Qt event loop; the widget just consumes the result and
paints the segments.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from flashback_sampler.app.theme import EREBUS


N_SEGMENTS = 20
FLOOR_DB = -60.0
CEILING_DB = 0.0
PEAK_HOLD_SECONDS = 1.2


def level_to_lit_segments(
    rms: float,
    n_segments: int = N_SEGMENTS,
    floor_db: float = FLOOR_DB,
    ceiling_db: float = CEILING_DB,
) -> int:
    """
    Map an RMS float in [0, 1+] to the count of lit segments (0..n_segments).
    Silence (<-60 dB) returns 0; full scale returns n_segments.
    """
    if rms <= 1e-7:
        return 0
    db = 20.0 * math.log10(rms)
    if db <= floor_db:
        return 0
    if db >= ceiling_db:
        return n_segments
    frac = (db - floor_db) / (ceiling_db - floor_db)
    return max(0, min(n_segments, int(round(frac * n_segments))))


def segment_color_token(idx: int, n_segments: int = N_SEGMENTS) -> str:
    """
    Return the EREBUS palette key for a given segment index (0 = bottom,
    n_segments-1 = top). Mapping follows the thermal spec:

        0..0.60   -> meter_low
        0.60..0.85 -> meter_mid
        0.85..0.95 -> meter_hot
        0.95..1.00 -> meter_peak
    """
    if n_segments <= 0:
        return "meter_low"
    frac = (idx + 1) / n_segments
    if frac <= 0.60:
        return "meter_low"
    if frac <= 0.85:
        return "meter_mid"
    if frac <= 0.95:
        return "meter_hot"
    return "meter_peak"


class LevelMeter(QWidget):
    """
    Vertical meter showing N_SEGMENTS segments per channel. Call
    `set_levels(rms_per_channel)` on every tick; the widget handles its
    own peak hold and repaint.
    """

    _SEGMENT_GAP = 1  # px between segments

    def __init__(self, channels: int = 2, parent=None):
        super().__init__(parent)
        self._channels = max(1, int(channels))
        self._levels = [0.0] * self._channels
        self._peak_segments = [0] * self._channels
        self._peak_ts = [0.0] * self._channels
        self.setMinimumWidth(10 * self._channels + 4)
        self.setMinimumHeight(80)

    def set_levels(self, rms_per_channel) -> None:
        now = time.monotonic()
        for i in range(self._channels):
            rms = float(rms_per_channel[i]) if i < len(rms_per_channel) else 0.0
            self._levels[i] = rms
            lit = level_to_lit_segments(rms)
            if lit >= self._peak_segments[i]:
                self._peak_segments[i] = lit
                self._peak_ts[i] = now
            elif now - self._peak_ts[i] > PEAK_HOLD_SECONDS:
                self._peak_segments[i] = max(self._peak_segments[i] - 1, lit)
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)

        p.fillRect(self.rect(), QColor(EREBUS["void"]))

        w = self.width()
        h = self.height()
        if w < 4 or h < 4:
            p.end()
            return

        # Lay out vertical strips, one per channel
        strip_w = max(4, int((w - 4) / self._channels) - 2)
        total_w = strip_w * self._channels + 2 * (self._channels - 1)
        start_x = (w - total_w) // 2

        inner_top = 2
        inner_bot = h - 2
        inner_h = inner_bot - inner_top
        seg_h = max(2, (inner_h - (N_SEGMENTS - 1) * self._SEGMENT_GAP) // N_SEGMENTS)
        meter_h = seg_h * N_SEGMENTS + (N_SEGMENTS - 1) * self._SEGMENT_GAP
        y0 = inner_bot - meter_h  # draw upward from bottom

        for ch in range(self._channels):
            x = start_x + ch * (strip_w + 2)
            lit = level_to_lit_segments(self._levels[ch])
            peak = self._peak_segments[ch]
            for s in range(N_SEGMENTS):
                # segment s is at the bottom if s=0
                sy = y0 + meter_h - (s + 1) * seg_h - s * self._SEGMENT_GAP
                if s < lit:
                    color_key = segment_color_token(s)
                    color = QColor(EREBUS[color_key])
                elif s == peak:
                    color = QColor(EREBUS["meter_peak"])
                else:
                    # dim background segment
                    color = QColor(EREBUS["meter_floor"])
                p.fillRect(x, sy, strip_w, seg_h, color)

        p.end()
