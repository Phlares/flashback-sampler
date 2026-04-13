"""
Topographical background painter — paints a subtle concentric-ring /
dimensioned-schematic pattern behind the main chassis content.

Used by MainWindow.paintEvent to fill the window background. Draws at
low opacity (6%) so the pattern reads as texture rather than decoration.
Inspired by the Erebus mood board's CAD-schematic references (the
dimensioned concentric-circle image and the "transfer details"
instrumentation screenshot).
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen

from flashback_sampler.app.theme import EREBUS


def paint_topo_background(painter: QPainter, w: int, h: int) -> None:
    """
    Fill a rect of size (w, h) with the Erebus topographical pattern.
    Caller is responsible for any prior fillRect / chassis background.
    """
    if w < 10 or h < 10:
        return

    # Base chassis tone (caller usually paints this already, but the
    # function is idempotent).
    painter.fillRect(0, 0, w, h, QColor(EREBUS["chassis"]))

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)

    ink = QColor(EREBUS["cream"])
    ink.setAlpha(int(0.06 * 255))
    pen = QPen(ink, 1)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    # ── Layer 1: concentric rings centered off the top-right corner ─
    # The off-center origin + partial intersection with the window
    # gives a "CAD drawing" feel without being symmetrical or busy.
    cx = w * 0.92
    cy = h * 0.08
    max_r = math.hypot(w, h) * 0.85
    step = 48
    for r in range(step, int(max_r), step):
        painter.drawEllipse(QPointF(cx, cy), r, r)

    # ── Layer 2: fainter horizontal grid (emphasizes "instrument") ──
    grid = QColor(EREBUS["cream"])
    grid.setAlpha(int(0.035 * 255))
    painter.setPen(QPen(grid, 1, Qt.DotLine))
    grid_step = 64
    for y in range(grid_step, h, grid_step):
        painter.drawLine(0, y, w, y)

    painter.restore()
