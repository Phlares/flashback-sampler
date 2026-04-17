"""TurntableWidget — custom QPainter widget rendering the record turntable.

Wireframe phase: empty concentric rings, colored track headers with status
lights, center spindle with colored selector chips, needle head below the
selected rim header, and an arm line to the bottom-inner corner of the widget.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import Qt, Signal, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QMouseEvent
from PySide6.QtWidgets import QWidget

from flashback_sampler.app.theme import EREBUS

BUFFER_TRACK_COLORS = ["#EDF9B8", "#FABCBC", "#E3B9FF"]
CLIP_TRACK_COLORS = ["#8123BF", "#48C2FF", "#FD35CB"]
STATUS_COLORS = {"armed": "#FF0000", "paused": "#FF9500", "inactive": "#B3ACAC"}


@dataclass(frozen=True)
class TurntableGeometry:
    cx: float
    cy: float
    disc_r: float
    spindle_r: float
    ring_gap: float
    ring_width: float
    size: float         # min(widget_w, widget_h)

    def ring_radius(self, i: int) -> float:
        return self.spindle_r + self.ring_gap * (i + 1) + self.ring_width * (i + 0.5)

    @property
    def spindle_chip_r(self) -> float:
        return max(4.0, self.spindle_r * 0.15)

    def spindle_chip_center(self, i: int, n: int) -> tuple[float, float]:
        angle = 2 * math.pi * i / max(n, 1) - math.pi / 2
        orbit = self.spindle_r * 0.6
        return self.cx + orbit * math.cos(angle), self.cy + orbit * math.sin(angle)

    def rim_header_center(self, side: str, i: int) -> tuple[float, float]:
        """Return (x, y) of rim track header for track i at the play angle.
        Buffer: 3 o'clock; Clip: 9 o'clock."""
        r = self.ring_radius(i)
        if side == "buffer":
            return (self.cx + r, self.cy)
        else:  # clip
            return (self.cx - r, self.cy)

    def needle_head_rect(self, side: str, i: int) -> QRectF:
        """Return a small rect just below the rim header for track i.
        Same width as the rim header, 4px tall, positioned 2px below the header."""
        header_cx, header_cy = self.rim_header_center(side, i)
        header_w = max(self.ring_width * 0.8, 6)
        header_h = max(12, self.size * 0.04)
        x = header_cx - header_w / 2
        y = header_cy + header_h / 2 + 2
        return QRectF(x, y, header_w, 4)

    def arm_target(self, side: str, widget_w: float, widget_h: float) -> tuple[float, float]:
        """Return the arm destination within the widget (bottom-inner corner)."""
        if side == "buffer":
            return (widget_w - 2, widget_h - 2)
        else:  # clip
            return (2, widget_h - 2)


class TurntableWidget(QWidget):
    track_selected = Signal(int)

    def __init__(self, side: str = "buffer", parent=None):
        super().__init__(parent)
        self._side = side
        self._track_count = 3
        self._selected_track = 0
        self._track_statuses: list[str] = ["armed", "armed", "paused"]
        self._track_waveforms: dict[int, "np.ndarray"] = {}
        self._track_selections: dict[int, tuple[float, float, str]] = {}
        self.setMinimumSize(200, 200)

    def side(self) -> str:
        return self._side

    def track_count(self) -> int:
        return self._track_count

    def set_track_count(self, n: int) -> None:
        self._track_count = max(1, n)
        if self._selected_track >= self._track_count:
            self._selected_track = self._track_count - 1
        while len(self._track_statuses) < self._track_count:
            self._track_statuses.append("inactive")
        self.update()

    def selected_track(self) -> int:
        return self._selected_track

    def select_track(self, index: int) -> None:
        index = max(0, min(index, self._track_count - 1))
        if index != self._selected_track:
            self._selected_track = index
            self.track_selected.emit(index)
            self.update()

    def header_angle_deg(self) -> int:
        return 0 if self._side == "buffer" else 180

    def set_track_waveform(self, track_idx: int, samples: np.ndarray) -> None:
        """Store a 1D float32 ndarray of normalized amplitudes [-1.0, 1.0]
        to be plotted radially around the given track's ring. Triggers repaint."""
        self._track_waveforms[track_idx] = np.asarray(samples, dtype=np.float32)
        self.update()

    def set_track_selection(self, track_idx: int, start_frac: float | None, end_frac: float | None, color: str) -> None:
        """Store or clear a selection range on a track's ring.
        Passing None for either bound clears the selection for that track."""
        if start_frac is None or end_frac is None or end_frac <= start_frac:
            self._track_selections.pop(track_idx, None)
        else:
            self._track_selections[track_idx] = (float(start_frac), float(end_frac), color)
        self.update()

    def _track_colors(self) -> list[str]:
        base = BUFFER_TRACK_COLORS if self._side == "buffer" else CLIP_TRACK_COLORS
        colors = []
        for i in range(self._track_count):
            colors.append(base[i % len(base)])
        return colors

    def _compute_geometry(self) -> TurntableGeometry:
        w, h = self.width(), self.height()
        size = min(w, h)
        cx, cy = w / 2, h / 2
        disc_r = size * 0.46
        spindle_r = size * 0.12
        ring_gap = 3
        track_area = disc_r - spindle_r - ring_gap * (self._track_count + 1)
        ring_width = track_area / max(self._track_count, 1)

        return TurntableGeometry(
            cx=cx, cy=cy, disc_r=disc_r, spindle_r=spindle_r,
            ring_gap=ring_gap, ring_width=ring_width,
            size=size,
        )

    def geometry(self) -> TurntableGeometry:
        """Public accessor for layout geometry — used by tests."""
        return self._compute_geometry()

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        g = self._compute_geometry()
        colors = self._track_colors()

        # Outer disc background
        p.setBrush(QColor(EREBUS["plate"]))
        p.setPen(QPen(QColor(EREBUS["hairline_strong"]), 1))
        p.drawEllipse(QPointF(g.cx, g.cy), g.disc_r, g.disc_r)

        # Concentric track rings (innermost = track 0, outermost = last)
        for i in range(self._track_count):
            r = g.ring_radius(i)
            color = QColor(colors[i])
            if i == self._selected_track:
                color.setAlpha(255)
                pen_w = max(g.ring_width * 0.5, 2) * 1.3
            else:
                color.setAlpha(80)
                pen_w = max(g.ring_width * 0.5, 2)
            p.setPen(QPen(color, pen_w))
            p.setBrush(Qt.NoBrush)

            if i in self._track_waveforms:
                samples = self._track_waveforms[i]
                n = len(samples)
                if n > 0:
                    path = QPainterPath()
                    for j in range(n):
                        theta = 2 * math.pi * j / n
                        r_j = r + float(samples[j]) * (g.ring_width * 0.4)
                        x = g.cx + r_j * math.cos(theta)
                        y = g.cy - r_j * math.sin(theta)
                        if j == 0:
                            path.moveTo(x, y)
                        else:
                            path.lineTo(x, y)
                    path.closeSubpath()
                    p.drawPath(path)
                else:
                    p.drawEllipse(QPointF(g.cx, g.cy), r, r)
            else:
                p.drawEllipse(QPointF(g.cx, g.cy), r, r)

        # ── Selection arc on each track with a stored selection ─────────
        # Body is 25% opacity; inner/outer edges are 1px fully-opaque strokes.
        for track_idx, (start_f, end_f, color_hex) in self._track_selections.items():
            if track_idx >= self._track_count:
                continue
            r = g.ring_radius(track_idx)
            play_angle_deg = self.header_angle_deg()
            start_angle = play_angle_deg - start_f * 360.0
            span = -(end_f - start_f) * 360.0
            band_w = max(g.ring_width * 0.8, 3)
            p.setBrush(Qt.NoBrush)

            body = QColor(color_hex)
            body.setAlphaF(0.25)
            body_pen = QPen(body, band_w)
            body_pen.setCapStyle(Qt.FlatCap)
            p.setPen(body_pen)
            rect_body = QRectF(g.cx - r, g.cy - r, 2 * r, 2 * r)
            p.drawArc(rect_body, int(start_angle * 16), int(span * 16))

            edge_pen = QPen(QColor(color_hex), 1)
            edge_pen.setCapStyle(Qt.FlatCap)
            p.setPen(edge_pen)
            r_outer = r + band_w / 2
            r_inner = r - band_w / 2
            rect_outer = QRectF(g.cx - r_outer, g.cy - r_outer, 2 * r_outer, 2 * r_outer)
            rect_inner = QRectF(g.cx - r_inner, g.cy - r_inner, 2 * r_inner, 2 * r_inner)
            p.drawArc(rect_outer, int(start_angle * 16), int(span * 16))
            p.drawArc(rect_inner, int(start_angle * 16), int(span * 16))

        # Track headers at play position (3 o'clock for buffer, 9 o'clock for clip)
        header_angle_rad = math.radians(self.header_angle_deg())
        header_w = max(g.ring_width * 0.8, 6)
        header_h = max(12, g.size * 0.04)
        for i in range(self._track_count):
            r = g.ring_radius(i)
            hx = g.cx + r * math.cos(header_angle_rad)
            hy = g.cy - r * math.sin(header_angle_rad)
            rect = QRectF(hx - header_w / 2, hy - header_h / 2, header_w, header_h)
            p.setBrush(QColor(colors[i]))
            p.setPen(Qt.NoPen)
            p.drawRect(rect)
            # Status indicator light (small circle on the header)
            status = self._track_statuses[i] if i < len(self._track_statuses) else "inactive"
            indicator_r = max(3, header_h * 0.2)
            p.setBrush(QColor(STATUS_COLORS.get(status, "#B3ACAC")))
            p.drawEllipse(QPointF(hx, hy), indicator_r, indicator_r)

        # Center spindle
        p.setBrush(QColor(EREBUS["void"]))
        p.setPen(QPen(QColor(EREBUS["hairline_strong"]), 1))
        p.drawEllipse(QPointF(g.cx, g.cy), g.spindle_r, g.spindle_r)

        # Selector chips in center spindle (radial arrangement)
        for i in range(self._track_count):
            chip_x, chip_y = g.spindle_chip_center(i, self._track_count)
            color = QColor(colors[i])
            if i == self._selected_track:
                color.setAlpha(255)
            else:
                color.setAlpha(120)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(chip_x, chip_y), g.spindle_chip_r, g.spindle_chip_r)

        # ── Needle head (directly below selected rim header) ────────────
        needle_rect = g.needle_head_rect(self._side, self._selected_track)
        p.setBrush(QColor(EREBUS["cream"]))
        p.setPen(Qt.NoPen)
        p.drawRect(needle_rect)

        # ── Arm line from needle head to bottom-inner corner ────────────
        arm_start_x = needle_rect.center().x()
        arm_start_y = needle_rect.bottom()
        arm_end_x, arm_end_y = g.arm_target(self._side, float(self.width()), float(self.height()))
        p.setPen(QPen(QColor(EREBUS["cream"]), 2))
        p.drawLine(QPointF(arm_start_x, arm_start_y), QPointF(arm_end_x, arm_end_y))

        p.end()

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        g = self._compute_geometry()
        mx, my = ev.position().x(), ev.position().y()

        dist = math.hypot(mx - g.cx, my - g.cy)

        # Click on spindle chips
        if dist < g.spindle_r:
            best = -1
            best_d = float("inf")
            for i in range(self._track_count):
                chip_x, chip_y = g.spindle_chip_center(i, self._track_count)
                d = math.hypot(mx - chip_x, my - chip_y)
                if d < best_d:
                    best_d = d
                    best = i
            if best >= 0:
                self.select_track(best)
            return

        # Click on a track ring
        for i in range(self._track_count):
            if abs(dist - g.ring_radius(i)) < g.ring_width / 2:
                self.select_track(i)
                return
