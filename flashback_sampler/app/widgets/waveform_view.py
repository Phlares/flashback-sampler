"""
WaveformView — the recessed "glass screen" that shows peak-bin data.

Paints directly via QPainter so we get pixel-perfect hairlines and an
inset recess effect that QSS box-shadow can't emulate in Qt. Consumes
the (n_bins, 2, channels) ndarray produced by
AudioCircularBuffer.get_peak_bins().
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, QLineF, QPointF, QRectF
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
        self._sel_start: float | None = None  # [0..1]
        self._sel_end: float | None = None  # [0..1]
        # Timeline state: total duration in seconds, and which side
        # is zero. "left" = ascending LTR 00:00..MM:SS (clip view),
        # "right" = zero at right edge, descending into negatives
        # (live buffer view, "-MM:SS ago").
        self._timeline_total_s: float = 0.0
        self._timeline_anchor: str = "left"  # "left" or "right"

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

    def set_timeline(self, total_seconds: float, anchor: str = "left") -> None:
        """
        Configure the timeline strip painted along the bottom of the
        waveform. Pass `total_seconds <= 0` to hide it.

        anchor:
          "left"  — 00:00 at the left edge, ascending rightward
                    (clip view: start of clip at left, end at right).
          "right" — 00:00 at the right edge, descending leftward into
                    negatives (live buffer: NOW at right, audio-ago
                    at left).
        """
        total = max(0.0, float(total_seconds))
        if anchor not in ("left", "right"):
            anchor = "left"
        if total == self._timeline_total_s and anchor == self._timeline_anchor:
            return
        self._timeline_total_s = total
        self._timeline_anchor = anchor
        self.update()

    def set_selection(
        self,
        start_frac: float | None,
        end_frac: float | None,
    ) -> None:
        """
        Highlight a horizontal selection band. Pass None / None to hide.
        The band is drawn as a translucent ember fill with a dashed
        boundary on the start edge and a solid boundary on the end edge —
        matches the "anchor section view" intent: start is informational,
        end is where the commit lands.
        """
        self._sel_start = start_frac
        self._sel_end = end_frac
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

        # ── 3. Inner content region (with label strip + timeline) ───
        label_strip = 18
        timeline_strip = 16 if self._timeline_total_s > 0 else 0
        inner_top = 1 + label_strip
        inner_x = 6
        inner_w = w - inner_x - 6
        inner_y = inner_top
        inner_h = h - inner_top - 6 - timeline_strip
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

        # ── 6. Section band (optional, drawn BEHIND the playhead) ────
        # Anti-alias these overlays so sub-pixel edges stay smooth even
        # when the rotary or scrub cursor sits between whole pixels.
        # The peak-bin waveform above was drawn with AA off for crisp
        # 1 px lines; we toggle AA on locally for the ember overlays.
        if self._sel_start is not None and self._sel_end is not None:
            s = max(0.0, min(1.0, float(self._sel_start)))
            e = max(0.0, min(1.0, float(self._sel_end)))
            if e > s:
                x1 = inner_x + s * inner_w
                x2 = inner_x + e * inner_w
                p.setRenderHint(QPainter.Antialiasing, True)
                # Translucent ember fill — float-precise rect
                fill = QColor(EREBUS["ember"])
                fill.setAlpha(int(0.14 * 255))
                p.fillRect(
                    QRectF(x1, float(inner_y), max(0.5, x2 - x1), float(inner_h)),
                    fill,
                )
                # Dashed start edge (informational)
                dash_pen = QPen(QColor(EREBUS["ember"]), 1, Qt.DashLine)
                p.setPen(dash_pen)
                p.drawLine(
                    QLineF(x1, float(inner_y), x1, float(inner_y + inner_h))
                )
                # Solid end edge (where the commit lands)
                p.setPen(QPen(QColor(EREBUS["ember"]), 2))
                p.drawLine(
                    QLineF(x2, float(inner_y), x2, float(inner_y + inner_h))
                )
                p.setRenderHint(QPainter.Antialiasing, False)

        # ── 7. Playhead (scrub cursor for Track 2 clip playback) ─────
        if self._playhead_frac is not None:
            frac = max(0.0, min(1.0, float(self._playhead_frac)))
            x = inner_x + frac * inner_w
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(EREBUS["ember"]), 1.2))
            p.drawLine(
                QLineF(x, float(inner_y), x, float(inner_y + inner_h))
            )
            p.setRenderHint(QPainter.Antialiasing, False)

        # ── 8. Timeline strip (bottom edge) ──────────────────────────
        if self._timeline_total_s > 0 and timeline_strip > 0:
            _paint_timeline(
                p,
                x0=inner_x,
                y0=inner_y + inner_h + 2,
                width=inner_w,
                height=timeline_strip - 2,
                total_seconds=self._timeline_total_s,
                anchor=self._timeline_anchor,
            )

        p.end()


def _pick_timeline_step(total_seconds: float, pixel_width: int) -> tuple[float, float]:
    """
    Return (major_step_seconds, minor_step_seconds) for a timeline
    that covers `total_seconds` over `pixel_width` pixels.

    Picks the largest step from a fixed ladder such that major-tick
    labels don't collide. Approximately 60 px between major labels.
    """
    if total_seconds <= 0 or pixel_width <= 40:
        return (total_seconds, total_seconds)

    # Candidate major step sizes in seconds, smallest to largest
    CANDIDATES: tuple[float, ...] = (
        0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0,
        60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0,
    )
    target_label_spacing_px = 70
    seconds_per_pixel = total_seconds / pixel_width
    desired_step = target_label_spacing_px * seconds_per_pixel

    major = CANDIDATES[-1]
    for c in CANDIDATES:
        if c >= desired_step:
            major = c
            break

    # Minor step = 1/5 of major for most ranges, 1/4 for coarse ones
    if major >= 60.0:
        minor = major / 6.0
    elif major >= 10.0:
        minor = major / 5.0
    else:
        minor = major / 5.0
    return (major, minor)


def _format_timeline_label(seconds: float) -> str:
    """Return a compact MM:SS or M:SS.s label for a timeline position."""
    sign = "-" if seconds < 0 else ""
    s = abs(float(seconds))
    if s < 60:
        whole = int(s)
        frac = s - whole
        if frac > 0.05:
            return f"{sign}{whole}.{int(round(frac * 10))}"
        return f"{sign}{whole}"
    m = int(s // 60)
    sec = int(round(s - m * 60))
    if sec == 60:
        m += 1
        sec = 0
    return f"{sign}{m}:{sec:02d}"


def _paint_timeline(
    p: QPainter,
    x0: int,
    y0: int,
    width: int,
    height: int,
    total_seconds: float,
    anchor: str,
) -> None:
    """
    Paint a tick-strip timeline over the rectangle (x0, y0, width, height).

    anchor="left":  0:00 at left, total_seconds at right
    anchor="right": 0:00 at right, labels show negative offsets going left
    """
    if width <= 2 or height <= 2 or total_seconds <= 0:
        return

    major_step, minor_step = _pick_timeline_step(total_seconds, width)
    px_per_sec = width / total_seconds

    # Minor ticks
    minor_pen = QPen(QColor(168, 163, 152, int(0.25 * 255)), 1)
    major_pen = QPen(QColor(168, 163, 152, int(0.55 * 255)), 1)

    p.save()
    p.setRenderHint(QPainter.Antialiasing, False)

    # Baseline — thin hairline across the top of the timeline strip
    base_pen = QPen(QColor(242, 237, 223, int(0.10 * 255)), 1)
    p.setPen(base_pen)
    p.drawLine(x0, y0, x0 + width, y0)

    # Label font
    font = p.font()
    font.setPointSize(7)
    font.setBold(False)
    p.setFont(font)

    minor_top = y0 + 1
    minor_bot = y0 + 4
    major_top = y0 + 1
    major_bot = y0 + 7
    text_top = y0 + 8

    def _tick_x_for_sec(s: float) -> float:
        if anchor == "right":
            # NOW at x0 + width; negative offsets extend leftward
            # s is a positive "seconds ago" value
            return x0 + width - s * px_per_sec
        # anchor == "left"
        return x0 + s * px_per_sec

    # Draw minor ticks
    n_minor = int(total_seconds / minor_step) + 1
    p.setPen(minor_pen)
    for i in range(n_minor + 1):
        s = i * minor_step
        if s > total_seconds + minor_step * 0.5:
            break
        x = _tick_x_for_sec(s)
        if x < x0 - 1 or x > x0 + width + 1:
            continue
        p.drawLine(int(x), minor_top, int(x), minor_bot)

    # Draw major ticks + labels
    n_major = int(total_seconds / major_step) + 1
    p.setPen(major_pen)
    # We want the last label flush to the anchor edge, so iterate and
    # draw everything including a final label for the far edge.
    last_label_x: float = -1e9
    min_label_spacing_px = 40

    for i in range(n_major + 1):
        s = i * major_step
        if s > total_seconds + major_step * 0.5:
            break
        x = _tick_x_for_sec(s)
        if x < x0 - 1 or x > x0 + width + 1:
            continue
        p.setPen(major_pen)
        p.drawLine(int(x), major_top, int(x), major_bot)

        # Don't paint labels that would collide with the previous one
        if x - last_label_x < min_label_spacing_px:
            continue

        if anchor == "right":
            label = _format_timeline_label(-s) if s > 0 else "NOW"
        else:
            label = _format_timeline_label(s)

        label_rect = QRectF(x - 40, text_top, 80, height - 8)
        p.setPen(QColor(EREBUS["bone"]))
        p.drawText(label_rect, Qt.AlignHCenter | Qt.AlignTop, label)
        last_label_x = x

    p.restore()


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
