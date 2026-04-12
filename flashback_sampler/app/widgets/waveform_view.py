"""
WaveformView — the recessed "glass screen" that shows peak-bin data.

Paints directly via QPainter so we get pixel-perfect hairlines and an
inset recess effect that QSS box-shadow can't emulate in Qt. Consumes
the (n_bins, 2, channels) ndarray produced by
AudioCircularBuffer.get_peak_bins().
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QLineF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from flashback_sampler.app.theme import EREBUS


class WaveformView(QWidget):
    """
    Displays peak-bin waveform data.

    Call `set_data(bins)` from the main thread to update the rendered
    frame. Bins must be shape (n_bins, 2, channels) — the first axis of
    the second dim is min, the second is max.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(80)
        self._bins: np.ndarray | None = None
        self._label_top: str = ""
        self._label_right: str = ""
        self._playhead_frac: float | None = None  # [0..1], None = hidden

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_data(self, bins: np.ndarray | None) -> None:
        self._bins = bins
        self.update()

    def set_labels(self, top: str, right: str) -> None:
        if top != self._label_top or right != self._label_right:
            self._label_top = top
            self._label_right = right
            self.update()

    def set_playhead(self, frac: float | None) -> None:
        """0..1 horizontal position of the scrub marker, or None to hide."""
        self._playhead_frac = frac
        self.update()

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)

        w = self.width()
        h = self.height()
        if w < 4 or h < 4:
            p.end()
            return

        # ── 1. Background ─────────────────────────────────────────────
        p.fillRect(self.rect(), QColor(EREBUS["void"]))

        # ── 2. Recessed border (light top-left, shadow bottom-right) ─
        # Top + left = cream hairline at 22% (light catching)
        p.setPen(QPen(QColor(242, 237, 223, int(0.22 * 255)), 1))
        p.drawLine(0, 0, w - 1, 0)
        p.drawLine(0, 0, 0, h - 1)
        # Bottom + right = pure #000 (shadow depth)
        p.setPen(QPen(QColor(0, 0, 0, 255), 1))
        p.drawLine(0, h - 1, w - 1, h - 1)
        p.drawLine(w - 1, 0, w - 1, h - 1)

        # ── 3. Inner content region (with label strip) ───────────────
        label_strip = 18
        inner_top = 1 + label_strip
        inner_x = 6
        inner_w = w - inner_x - 6
        inner_y = inner_top
        inner_h = h - inner_top - 6
        if inner_w <= 2 or inner_h <= 2:
            p.end()
            return

        # ── 4. Top label strip ────────────────────────────────────────
        if self._label_top or self._label_right:
            label_font = self.font()
            label_font.setPointSize(7)
            label_font.setBold(False)
            p.setFont(label_font)
            p.setPen(QColor(EREBUS["bone"]))
            p.drawText(
                6, 1, w - 12, label_strip,
                Qt.AlignLeft | Qt.AlignVCenter,
                self._label_top.upper(),
            )
            p.drawText(
                6, 1, w - 12, label_strip,
                Qt.AlignRight | Qt.AlignVCenter,
                self._label_right.upper(),
            )

        # ── 5. Waveform peak bins ────────────────────────────────────
        bins = self._bins
        if bins is not None and bins.size > 0:
            n_bins, _, channels = bins.shape
            bin_width = inner_w / n_bins if n_bins else 1

            signal = QColor(EREBUS["signal"])

            # Stereo = two stacked rows; mono = one row spanning full inner_h
            if channels >= 2:
                row_h = inner_h / 2
                for ch in (0, 1):
                    mid = inner_y + row_h * (ch + 0.5)
                    half = (row_h - 4) / 2
                    lines = _make_peak_lines(
                        bins, ch, inner_x, bin_width, mid, half
                    )
                    p.setPen(QPen(signal, 1))
                    p.drawLines(lines)
            else:
                mid = inner_y + inner_h / 2
                half = (inner_h - 4) / 2
                lines = _make_peak_lines(
                    bins, 0, inner_x, bin_width, mid, half
                )
                p.setPen(QPen(signal, 1))
                p.drawLines(lines)

        # ── 6. Playhead (scrub cursor) ───────────────────────────────
        if self._playhead_frac is not None:
            frac = max(0.0, min(1.0, float(self._playhead_frac)))
            x = int(inner_x + frac * inner_w)
            p.setPen(QPen(QColor(EREBUS["ember"]), 1))
            p.drawLine(x, inner_y, x, inner_y + inner_h)

        p.end()


def _make_peak_lines(
    bins: np.ndarray,
    channel: int,
    x0: float,
    bin_width: float,
    mid_y: float,
    half_h: float,
) -> list[QLineF]:
    """
    Build a list of QLineF (one per bin) for a single channel. Each line
    runs from the bin's max to its min around `mid_y`, scaled to
    ±half_h. Audio samples are assumed to be in [-1, 1]; values outside
    are clamped by the scale.
    """
    n_bins = bins.shape[0]
    lines: list[QLineF] = []
    mins = bins[:, 0, channel]
    maxs = bins[:, 1, channel]
    for i in range(n_bins):
        x = x0 + i * bin_width + bin_width / 2.0
        y_top = mid_y - float(maxs[i]) * half_h
        y_bot = mid_y - float(mins[i]) * half_h
        # Guarantee at least 1 px tall so silent bins still render a dot
        if abs(y_bot - y_top) < 1.0:
            y_top -= 0.5
            y_bot += 0.5
        lines.append(QLineF(x, y_top, x, y_bot))
    return lines
