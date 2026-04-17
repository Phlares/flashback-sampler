"""TurntableWidget — custom QPainter widget rendering the record turntable.

Wireframe phase: empty concentric rings, colored track headers with status
lights, center spindle with colored selector chips, needle rail + chips,
and a swing-arm needle anchored at the upper-inner corner of the disc.
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
    rail_x: float       # top-left x of rail rect
    rail_y: float       # top-left y of rail rect
    rail_w: float
    rail_h: float
    chip_w: float
    chip_h: float
    anchor_x: float     # upper-inner corner x
    anchor_y: float     # upper-inner corner y
    size: float         # min(widget_w, widget_h)

    def ring_radius(self, i: int) -> float:
        return self.spindle_r + self.ring_gap * (i + 1) + self.ring_width * (i + 0.5)

    def chip_center(self, side: str, i: int, n: int) -> tuple[float, float]:
        if side == "buffer":
            cx = self.rail_x + (i + 0.5) * self.rail_w / max(n, 1)
        else:  # clip — reverse so innermost track is closest to rim
            cx = self.rail_x + (n - 1 - i + 0.5) * self.rail_w / max(n, 1)
        cy = self.rail_y + self.rail_h / 2
        return cx, cy

    @property
    def spindle_chip_r(self) -> float:
        return max(4.0, self.spindle_r * 0.15)

    def spindle_chip_center(self, i: int, n: int) -> tuple[float, float]:
        angle = 2 * math.pi * i / max(n, 1) - math.pi / 2
        orbit = self.spindle_r * 0.6
        return self.cx + orbit * math.cos(angle), self.cy + orbit * math.sin(angle)


class TurntableWidget(QWidget):
    track_selected = Signal(int)

    def __init__(self, side: str = "buffer", parent=None):
        super().__init__(parent)
        self._side = side
        self._track_count = 3
        self._selected_track = 0
        self._track_statuses: list[str] = ["armed", "armed", "paused"]
        self._track_waveforms: dict[int, "np.ndarray"] = {}
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

        rail_w = disc_r * 0.45
        rail_h = max(12.0, disc_r * 0.10)
        if self._side == "buffer":
            rail_x = cx + disc_r + 4
            anchor_x = cx + disc_r
        else:  # clip
            rail_x = cx - disc_r - 4 - rail_w
            anchor_x = cx - disc_r
        rail_y = cy - rail_h / 2
        anchor_y = cy - disc_r

        chip_w = chip_h = rail_h * 0.7
        return TurntableGeometry(
            cx=cx, cy=cy, disc_r=disc_r, spindle_r=spindle_r,
            ring_gap=ring_gap, ring_width=ring_width,
            rail_x=rail_x, rail_y=rail_y, rail_w=rail_w, rail_h=rail_h,
            chip_w=chip_w, chip_h=chip_h,
            anchor_x=anchor_x, anchor_y=anchor_y,
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

        # ── Selector rail ─────────────────────────────────────────────────
        rail_rect = QRectF(g.rail_x, g.rail_y, g.rail_w, g.rail_h)
        p.setBrush(QColor(EREBUS["void"]))
        p.setPen(QPen(QColor(EREBUS["hairline_strong"]), 1))
        p.drawRect(rail_rect)

        # ── Chips on rail ────────────────────────────────────────────────
        for i in range(self._track_count):
            chip_cx, chip_cy = g.chip_center(self._side, i, self._track_count)
            chip_rect = QRectF(chip_cx - g.chip_w / 2, chip_cy - g.chip_h / 2, g.chip_w, g.chip_h)
            color = QColor(colors[i])
            if i == self._selected_track:
                color.setAlpha(255)
            else:
                color.setAlpha(100)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawRect(chip_rect)
            # Status indicator above chip
            status = self._track_statuses[i] if i < len(self._track_statuses) else "inactive"
            p.setBrush(QColor(STATUS_COLORS.get(status, "#B3ACAC")))
            p.drawEllipse(QPointF(chip_cx, chip_cy - g.chip_h), 2, 2)

        # ── Arm (needle) ─────────────────────────────────────────────────
        sel_chip_cx, sel_chip_cy = g.chip_center(self._side, self._selected_track, self._track_count)
        p.setPen(QPen(QColor(EREBUS["cream"]), 2))
        p.drawLine(QPointF(g.anchor_x, g.anchor_y), QPointF(sel_chip_cx, sel_chip_cy))
        p.setBrush(QColor(EREBUS["ember"]))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(sel_chip_cx, sel_chip_cy), 4, 4)

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

        # Click on rail chips
        for i in range(self._track_count):
            chip_cx, chip_cy = g.chip_center(self._side, i, self._track_count)
            chip_rect = QRectF(chip_cx - g.chip_w / 2, chip_cy - g.chip_h / 2, g.chip_w, g.chip_h)
            if chip_rect.contains(QPointF(mx, my)):
                self.select_track(i)
                return
