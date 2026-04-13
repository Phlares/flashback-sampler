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

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from flashback_sampler.app.theme import EREBUS, font_family


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

# Mouse-wheel / keyboard step: each notch (or arrow press) moves this
# fraction of the full range. A full sweep therefore takes 60 ticks.
# Modifiers scale: Shift × 5 (coarse), Ctrl × 0.2 (fine).
WHEEL_STEP_FRAC = 1.0 / 60.0
WHEEL_STEP_COARSE_MULTIPLIER = 5.0
WHEEL_STEP_FINE_MULTIPLIER = 0.2


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
        # Focus policy: click and tab both bring focus so keyboard and
        # wheel input are routed to us. Wheel events require focus so
        # scrolling OVER the knob doesn't steal it from the surrounding
        # scroll area (even though we don't currently have one — good
        # practice for future embedding).
        self.setFocusPolicy(Qt.StrongFocus)

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

    def _modifier_step_value(self, modifiers) -> float:
        """How much one wheel notch / arrow press should move the value."""
        frac = WHEEL_STEP_FRAC
        if modifiers & Qt.ShiftModifier:
            frac *= WHEEL_STEP_COARSE_MULTIPLIER
        elif modifiers & Qt.ControlModifier:
            frac *= WHEEL_STEP_FINE_MULTIPLIER
        return (self._max - self._min) * frac

    def wheelEvent(self, ev) -> None:  # noqa: N802
        notches = ev.angleDelta().y() / 120.0
        if notches == 0:
            ev.ignore()
            return
        # Grab focus on first interaction so the focus ring appears
        if not self.hasFocus():
            self.setFocus()
        step = self._modifier_step_value(ev.modifiers())
        new_val = clamp(self._value + notches * step, self._min, self._max)
        if new_val != self._value:
            self._value = new_val
            self.valueChanged.emit(new_val)
            self.update()
        ev.accept()

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        key = ev.key()
        step = self._modifier_step_value(ev.modifiers())
        if key in (Qt.Key_Up, Qt.Key_Right):
            self.setValue(self._value + step)
            ev.accept()
            return
        if key in (Qt.Key_Down, Qt.Key_Left):
            self.setValue(self._value - step)
            ev.accept()
            return
        if key == Qt.Key_Home:
            self.setValue(self._min)
            ev.accept()
            return
        if key == Qt.Key_End:
            self.setValue(self._max)
            ev.accept()
            return
        if key in (Qt.Key_0, Qt.Key_Return, Qt.Key_Enter):
            # Snap-to-default (same as double-click)
            self.setValue(self._default_value)
            ev.accept()
            return
        super().keyPressEvent(ev)

    def focusInEvent(self, ev) -> None:  # noqa: N802
        self.update()
        super().focusInEvent(ev)

    def focusOutEvent(self, ev) -> None:  # noqa: N802
        self.update()
        super().focusOutEvent(ev)

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

        # ── Focus ring — thin ember circle inside the bezel ─────────
        # Drawn only when the knob holds keyboard focus, so users can
        # tell at a glance that wheel / arrow input is active.
        if self.hasFocus():
            focus_r = (r_dial + r_outer) / 2.0
            p.setBrush(Qt.NoBrush)
            focus_pen = QPen(QColor(EREBUS["ember"]), 1.2, Qt.DotLine)
            p.setPen(focus_pen)
            p.drawEllipse(QPointF(cx, cy), focus_r, focus_r)

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

        # ── Hub — void well with hairline ring ───────────────────────
        p.setBrush(QColor(EREBUS["void"]))
        p.setPen(QPen(QColor(242, 237, 223, int(0.22 * 255)), 1))
        p.drawEllipse(QPointF(cx, cy), r_hub, r_hub)

        # ── Hub readout — sits INSIDE the dial face, not the hub ─────
        # The hub circle is decorative; the text is allowed to extend
        # beyond it as long as it stays clear of the indicator line.
        # Available radius for text: up to ~80% of r_dial (leaves room
        # for the indicator at 85-95%). We iteratively shrink the font
        # until the text fits, so "-15:00" sits comfortably even on a
        # knob where a smaller default pt size would have been fine for
        # the 3-char "NOW" idle state.
        if self._hub_text:
            text_w_limit = r_dial * 1.55  # diameter of the text area
            text_h_limit = r_hub * 1.7

            # Start with a generous size (~11% of the diameter) and
            # shrink in integer steps until the widest possible value
            # fits. Using a fixed "widest expected" probe instead of
            # self._hub_text keeps the font size stable across
            # rest/dragging transitions (so numbers don't jitter in
            # size as the user turns the knob).
            probe = "-99:99"
            fam = font_family("display").split(",")[0].strip().strip('"')
            font: QFont = self.font()
            if fam:
                font.setFamily(fam)
            font.setBold(True)

            size_pt = max(9, int(side * 0.115))
            for _ in range(12):
                font.setPointSize(size_pt)
                metrics = QFontMetrics(font)
                if (
                    metrics.horizontalAdvance(probe) <= text_w_limit
                    and metrics.height() <= text_h_limit
                ):
                    break
                size_pt -= 1
                if size_pt <= 8:
                    size_pt = 8
                    font.setPointSize(size_pt)
                    break

            p.setFont(font)
            p.setPen(QColor(EREBUS["cream"]))
            rect = QRectF(
                cx - text_w_limit / 2.0,
                cy - text_h_limit / 2.0,
                text_w_limit,
                text_h_limit,
            )
            p.drawText(rect, Qt.AlignCenter, self._hub_text)

        p.end()
