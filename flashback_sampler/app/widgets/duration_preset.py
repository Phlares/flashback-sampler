"""
DurationPreset — vertical column of 8 preset duration cells.

One cell is active at a time, rendered with an ember left-edge and
brighter text. Click a cell to select it; emits `durationChanged(float)`
with the new duration in seconds.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from flashback_sampler.app.theme import EREBUS


DEFAULT_PRESETS: tuple[float, ...] = (
    15.0, 30.0, 60.0, 120.0, 180.0, 300.0, 600.0, 900.0,
)


def format_preset(seconds: float) -> str:
    seconds = int(seconds)
    m = seconds // 60
    s = seconds - m * 60
    return f"{m:01d}:{s:02d}"


class DurationPreset(QWidget):
    durationChanged = Signal(float)

    def __init__(
        self,
        presets: tuple[float, ...] = DEFAULT_PRESETS,
        default_index: int = 4,
        parent=None,
    ):
        super().__init__(parent)
        self._presets = tuple(presets)
        self._active_idx = max(0, min(len(self._presets) - 1, default_index))
        self._hovered_idx: int | None = None

        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.setMinimumWidth(80)
        self.setMinimumHeight(len(self._presets) * 22)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def active_index(self) -> int:
        return self._active_idx

    def active_duration(self) -> float:
        return self._presets[self._active_idx]

    def set_active_index(self, idx: int) -> None:
        idx = max(0, min(len(self._presets) - 1, int(idx)))
        if idx == self._active_idx:
            return
        self._active_idx = idx
        self.durationChanged.emit(self._presets[idx])
        self.update()

    def step(self, delta: int) -> None:
        self.set_active_index(self._active_idx + int(delta))

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def _cell_at(self, y: float) -> int | None:
        h = self.height()
        if h <= 0 or not self._presets:
            return None
        row_h = h / len(self._presets)
        idx = int(y // row_h)
        if 0 <= idx < len(self._presets):
            return idx
        return None

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        idx = self._cell_at(ev.position().y())
        if idx is not None:
            self.set_active_index(idx)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        idx = self._cell_at(ev.position().y())
        if idx != self._hovered_idx:
            self._hovered_idx = idx
            self.update()

    def leaveEvent(self, ev) -> None:  # noqa: N802
        self._hovered_idx = None
        self.update()

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)

        w = self.width()
        h = self.height()
        if w < 4 or h < 4 or not self._presets:
            p.end()
            return

        row_h = h / len(self._presets)
        font = self.font()
        font.setPointSize(9)
        font.setBold(False)
        p.setFont(font)

        for i, dur in enumerate(self._presets):
            y = int(i * row_h)
            rh = int(row_h) - 1

            # Background
            if i == self._active_idx:
                bg = QColor(EREBUS["ridge"])
            elif i == self._hovered_idx:
                bg = QColor(EREBUS["plate"])
            else:
                bg = QColor(EREBUS["chassis"])
            p.fillRect(0, y, w, rh, bg)

            # Left ember tell for active cell
            if i == self._active_idx:
                p.fillRect(0, y, 3, rh, QColor(EREBUS["ember"]))
                text_color = QColor(EREBUS["cream"])
            else:
                text_color = QColor(EREBUS["bone"])

            # Row text, uppercase, letter-spaced
            p.setPen(QPen(text_color))
            label = format_preset(dur).upper()
            p.drawText(
                8, y, w - 12, rh,
                Qt.AlignLeft | Qt.AlignVCenter,
                label,
            )

            # 1px hairline between rows (except last)
            if i < len(self._presets) - 1:
                p.setPen(QPen(QColor(242, 237, 223, int(0.06 * 255)), 1))
                p.drawLine(6, y + rh, w - 6, y + rh)

        p.end()
