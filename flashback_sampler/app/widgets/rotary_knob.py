"""
RotaryKnob — the TP-7-style central encoder.

Drag-to-turn interaction with a recessed bezel, engraved ticks, ember
indicator line, and a hub-mounted text readout. Pure Qt Widgets — no
external libraries.

Value math (normalization, sweep → angle) is extracted into module-level
pure functions so tests can verify the angle mapping without a running
Qt event loop.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from flashback_sampler.app.theme import EREBUS


# Sweep runs from 7:30 (−225°) clockwise through 12 o'clock (−90°) to
# 4:30 (+45°) — 270° total, leaving the bottom arc empty so the value
# direction reads correctly as "back in time" when the indicator is on
# the left half.
SWEEP_START_DEG = -225.0
SWEEP_END_DEG = 45.0
SWEEP_DEG = SWEEP_END_DEG - SWEEP_START_DEG  # 270.0

# Drag sensitivity: pixels of vertical cursor motion that equals a full
# sweep from min to max. Smaller = more sensitive.
DRAG_PX_FULL_RANGE = 260.0


def value_to_angle_deg(value: float, lo: float, hi: float) -> float:
    """Map a value in [lo, hi] to a degrees position on the dial sweep."""
    if hi <= lo:
        return SWEEP_START_DEG
    frac = (value - lo) / (hi - lo)
    frac = max(0.0, min(1.0, frac))
    return SWEEP_START_DEG + frac * SWEEP_DEG


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class RotaryKnob(QWidget):
    """
    Drag-to-turn rotary control.

    Signals:
        valueChanged(float) — fires on every mouse drag delta when the
            internal value changes.

    Minimal API:
        setRange(lo, hi)
        setValue(v)              # clamps; emits valueChanged if changed
        value() -> float
        setHubText(str)          # the readout mounted inside the hub
    """

    valueChanged = Signal(float)

    def __init__(self, parent=None, diameter: int = 160):
        super().__init__(parent)
        self.setMinimumSize(diameter, diameter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.resize(diameter, diameter)

        self._min = 0.0
        self._max = 1.0
        self._value = 0.0
        self._default_value = 0.0
        self._hub_text: str = ""

        self._dragging = False
        self._drag_start_y = 0
        self._drag_start_value = 0.0

        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def setRange(self, lo: float, hi: float) -> None:  # noqa: N802
        self._min = float(lo)
        self._max = float(hi)
        self._value = clamp(self._value, self._min, self._max)
        self.update()

    def setValue(self, v: float) -> None:  # noqa: N802
        new_val = clamp(float(v), self._min, self._max)
        if new_val != self._value:
            self._value = new_val
            self.valueChanged.emit(new_val)
            self.update()

    def setDefaultValue(self, v: float) -> None:  # noqa: N802
        self._default_value = clamp(float(v), self._min, self._max)

    def value(self) -> float:
        return self._value

    def setHubText(self, text: str) -> None:  # noqa: N802
        if text != self._hub_text:
            self._hub_text = text
            self.update()

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() != Qt.LeftButton:
            super().mousePressEvent(ev)
            return
        self._dragging = True
        self._drag_start_y = ev.position().y()
        self._drag_start_value = self._value

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if not self._dragging:
            return
        dy = ev.position().y() - self._drag_start_y
        # Up (negative dy) = smaller value (closer to lo); down = larger
        delta = dy * (self._max - self._min) / DRAG_PX_FULL_RANGE
        new_val = clamp(self._drag_start_value + delta, self._min, self._max)
        if new_val != self._value:
            self._value = new_val
            self.valueChanged.emit(new_val)
            self.update()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self._dragging = False

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        # Double-click resets to the default (useful for snapping anchor
        # back to "NOW" — the 0 value).
        self.setValue(self._default_value)

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w = self.width()
        h = self.height()
        side = min(w, h)
        if side < 20:
            p.end()
            return
        cx = w / 2.0
        cy = h / 2.0
        r_outer = side / 2.0 - 2.0
        bezel_w = max(8.0, side * 0.085)
        r_dial = r_outer - bezel_w
        r_hub = r_dial * 0.34

        # ── Bezel — radial gradient (void → chassis) fakes the recess ─
        grad = QRadialGradient(cx, cy - r_outer * 0.35, r_outer * 1.2)
        grad.setColorAt(0.0, QColor(EREBUS["chassis"]))
        grad.setColorAt(0.85, QColor(EREBUS["void"]))
        grad.setColorAt(1.0, QColor(0, 0, 0))
        p.setBrush(grad)
        p.setPen(QPen(QColor(0, 0, 0, 180), 1))
        p.drawEllipse(QPointF(cx, cy), r_outer, r_outer)

        # ── Dial face ────────────────────────────────────────────────
        p.setBrush(QColor(EREBUS["ridge"]))
        p.setPen(QPen(QColor(242, 237, 223, int(0.08 * 255)), 1))
        p.drawEllipse(QPointF(cx, cy), r_dial, r_dial)

        # ── Engraved ticks — 12 at 30° intervals ─────────────────────
        p.setPen(QPen(QColor(168, 163, 152, int(0.40 * 255)), 1))
        tick_inner = r_dial - max(4.0, side * 0.05)
        tick_outer = r_dial - 2.0
        for i in range(12):
            angle_deg = i * 30.0 - 90.0
            a = math.radians(angle_deg)
            p.drawLine(
                QPointF(cx + math.cos(a) * tick_inner, cy + math.sin(a) * tick_inner),
                QPointF(cx + math.cos(a) * tick_outer, cy + math.sin(a) * tick_outer),
            )

        # ── Indicator line (ember) ───────────────────────────────────
        angle_deg = value_to_angle_deg(self._value, self._min, self._max)
        a = math.radians(angle_deg)
        ind_inner = r_hub + max(3.0, side * 0.03)
        ind_outer = r_dial - max(4.0, side * 0.05)
        p.setPen(QPen(QColor(EREBUS["ember"]), max(2.0, side * 0.020)))
        p.drawLine(
            QPointF(cx + math.cos(a) * ind_inner, cy + math.sin(a) * ind_inner),
            QPointF(cx + math.cos(a) * ind_outer, cy + math.sin(a) * ind_outer),
        )

        # ── Hub — void well with hairline ring + readout ─────────────
        p.setBrush(QColor(EREBUS["void"]))
        p.setPen(QPen(QColor(242, 237, 223, int(0.22 * 255)), 1))
        p.drawEllipse(QPointF(cx, cy), r_hub, r_hub)

        if self._hub_text:
            font: QFont = self.font()
            font.setPointSize(max(9, int(side * 0.085)))
            font.setBold(True)
            p.setFont(font)
            p.setPen(QColor(EREBUS["cream"]))
            p.drawText(
                int(cx - r_hub),
                int(cy - r_hub),
                int(r_hub * 2),
                int(r_hub * 2),
                Qt.AlignCenter,
                self._hub_text,
            )

        p.end()
