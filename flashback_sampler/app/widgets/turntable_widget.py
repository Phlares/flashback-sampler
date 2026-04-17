"""TurntableWidget — custom QPainter widget rendering the record turntable.

Wireframe phase: empty concentric rings, colored track headers with status
lights, center spindle with colored selector chips, and a needle arm line.
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QMouseEvent
from PySide6.QtWidgets import QWidget

from flashback_sampler.app.theme import EREBUS

BUFFER_TRACK_COLORS = ["#EDF9B8", "#FABCBC", "#E3B9FF"]
CLIP_TRACK_COLORS = ["#8123BF", "#48C2FF", "#FD35CB"]
STATUS_COLORS = {"armed": "#FF0000", "paused": "#FF9500", "inactive": "#B3ACAC"}


class TurntableWidget(QWidget):
    track_selected = Signal(int)

    def __init__(self, side: str = "buffer", parent=None):
        super().__init__(parent)
        self._side = side
        self._track_count = 3
        self._selected_track = 0
        self._track_statuses: list[str] = ["armed", "armed", "paused"]
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

    def _track_colors(self) -> list[str]:
        base = BUFFER_TRACK_COLORS if self._side == "buffer" else CLIP_TRACK_COLORS
        colors = []
        for i in range(self._track_count):
            colors.append(base[i % len(base)])
        return colors

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w, h = self.width(), self.height()
        size = min(w, h)
        cx, cy = w / 2, h / 2
        colors = self._track_colors()

        # Outer disc background
        disc_r = size * 0.46
        p.setBrush(QColor(EREBUS["plate"]))
        p.setPen(QPen(QColor(EREBUS["hairline_strong"]), 1))
        p.drawEllipse(QPointF(cx, cy), disc_r, disc_r)

        # Concentric track rings (innermost = track 0, outermost = last)
        spindle_r = size * 0.12
        ring_gap = 3
        track_area = disc_r - spindle_r - ring_gap * (self._track_count + 1)
        ring_width = track_area / max(self._track_count, 1)

        for i in range(self._track_count):
            r = spindle_r + ring_gap * (i + 1) + ring_width * (i + 0.5)
            pen_width = max(ring_width * 0.6, 2)
            color = QColor(colors[i])
            if i == self._selected_track:
                color.setAlpha(200)
                pen_w = pen_width * 1.3
            else:
                color.setAlpha(80)
                pen_w = pen_width
            p.setPen(QPen(color, pen_w))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), r, r)

        # Track headers at play position (3 o'clock for buffer, 9 o'clock for clip)
        header_angle_rad = math.radians(self.header_angle_deg())
        header_w = max(ring_width * 0.8, 6)
        header_h = max(12, size * 0.04)
        for i in range(self._track_count):
            r = spindle_r + ring_gap * (i + 1) + ring_width * (i + 0.5)
            hx = cx + r * math.cos(header_angle_rad)
            hy = cy - r * math.sin(header_angle_rad)
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
        p.drawEllipse(QPointF(cx, cy), spindle_r, spindle_r)

        # Selector chips in center spindle (radial arrangement)
        chip_r = max(4, spindle_r * 0.15)
        chip_orbit = spindle_r * 0.6
        for i in range(self._track_count):
            angle = 2 * math.pi * i / max(self._track_count, 1) - math.pi / 2
            chip_x = cx + chip_orbit * math.cos(angle)
            chip_y = cy + chip_orbit * math.sin(angle)
            color = QColor(colors[i])
            if i == self._selected_track:
                color.setAlpha(255)
            else:
                color.setAlpha(120)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(chip_x, chip_y), chip_r, chip_r)

        # Needle arm — line from outside disc to selected track ring
        selected_r = spindle_r + ring_gap * (self._selected_track + 1) + ring_width * (self._selected_track + 0.5)
        needle_start_r = disc_r + 8
        # Needle enters from the side facing the center bridge:
        # buffer = right side (0°), clip = left side (180°)
        needle_angle = math.radians(self.header_angle_deg())
        # Offset the needle slightly from the header so they don't overlap
        needle_offset = math.radians(15 if self._side == "buffer" else -15)
        nx_start = cx + needle_start_r * math.cos(needle_angle + needle_offset)
        ny_start = cy - needle_start_r * math.sin(needle_angle + needle_offset)
        nx_end = cx + selected_r * math.cos(needle_angle + needle_offset)
        ny_end = cy - selected_r * math.sin(needle_angle + needle_offset)
        p.setPen(QPen(QColor(EREBUS["cream"]), 2))
        p.drawLine(QPointF(nx_start, ny_start), QPointF(nx_end, ny_end))
        # Needle head (small circle at the track)
        p.setBrush(QColor(EREBUS["ember"]))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(nx_end, ny_end), 4, 4)

        # Track selection tabs (stacked bars on the bridge-facing side)
        tab_side = 1 if self._side == "buffer" else -1  # right or left
        tab_x_base = cx + (disc_r + 14) * tab_side
        tab_width = max(8, size * 0.03)
        tab_height = max(ring_width * 0.7, 6)
        total_tabs_h = self._track_count * (tab_height + 2)
        tab_y_start = cy - total_tabs_h / 2
        for i in range(self._track_count):
            tx = tab_x_base - (tab_width if tab_side < 0 else 0)
            ty = tab_y_start + i * (tab_height + 2)
            color = QColor(colors[i])
            if i == self._selected_track:
                color.setAlpha(255)
            else:
                color.setAlpha(100)
            p.setBrush(color)
            p.setPen(Qt.NoPen)
            p.drawRect(QRectF(tx, ty, tab_width, tab_height))

        p.end()

    def mousePressEvent(self, ev: QMouseEvent) -> None:
        w, h = self.width(), self.height()
        size = min(w, h)
        cx, cy = w / 2, h / 2
        mx, my = ev.position().x(), ev.position().y()

        spindle_r = size * 0.12
        disc_r = size * 0.46
        ring_gap = 3
        track_area = disc_r - spindle_r - ring_gap * (self._track_count + 1)
        ring_width = track_area / max(self._track_count, 1)

        dist = math.hypot(mx - cx, my - cy)

        # Click on spindle chips
        if dist < spindle_r:
            chip_orbit = spindle_r * 0.6
            best = -1
            best_d = float("inf")
            for i in range(self._track_count):
                angle = 2 * math.pi * i / max(self._track_count, 1) - math.pi / 2
                chip_x = cx + chip_orbit * math.cos(angle)
                chip_y = cy + chip_orbit * math.sin(angle)
                d = math.hypot(mx - chip_x, my - chip_y)
                if d < best_d:
                    best_d = d
                    best = i
            if best >= 0:
                self.select_track(best)
            return

        # Click on a track ring
        for i in range(self._track_count):
            r = spindle_r + ring_gap * (i + 1) + ring_width * (i + 0.5)
            if abs(dist - r) < ring_width / 2:
                self.select_track(i)
                return

        # Click on track tabs
        tab_side = 1 if self._side == "buffer" else -1
        tab_x_base = cx + (disc_r + 14) * tab_side
        tab_width = max(8, size * 0.03)
        tab_height = max(ring_width * 0.7, 6)
        total_tabs_h = self._track_count * (tab_height + 2)
        tab_y_start = cy - total_tabs_h / 2
        for i in range(self._track_count):
            tx = tab_x_base - (tab_width if tab_side < 0 else 0)
            ty = tab_y_start + i * (tab_height + 2)
            if tx <= mx <= tx + tab_width and ty <= my <= ty + tab_height:
                self.select_track(i)
                return
