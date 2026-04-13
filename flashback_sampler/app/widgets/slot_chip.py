"""
SlotChip — one compact representation of a CaptureSlot in the source
strip. Not a full waveform view; just a tactile pad with the slot
name, a fill bar, and a record dot when the slot is capturing.

Clicking emits `clicked()` so the host can switch the active slot.
Right-click emits `contextMenuRequested(QPointF global_pos)` so the
host can show a QMenu with Remove / Rename / Device picker.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from flashback_sampler.app.theme import EREBUS, font_family


CHIP_WIDTH = 168
CHIP_HEIGHT = 52


class SlotChip(QWidget):
    clicked = Signal()
    contextMenuRequested = Signal(QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._name: str = ""
        self._fill_percent: float = 0.0
        self._is_active: bool = False
        self._is_capturing: bool = False
        self._xrun_count: int = 0
        self._ram_mb: float = 0.0
        self.setFixedSize(CHIP_WIDTH, CHIP_HEIGHT)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def set_state(
        self,
        *,
        name: str,
        fill_percent: float,
        is_active: bool,
        is_capturing: bool,
        xrun_count: int,
        ram_mb: float,
    ) -> None:
        changed = (
            name != self._name
            or fill_percent != self._fill_percent
            or is_active != self._is_active
            or is_capturing != self._is_capturing
            or xrun_count != self._xrun_count
            or abs(ram_mb - self._ram_mb) > 0.05
        )
        self._name = name
        self._fill_percent = max(0.0, min(100.0, float(fill_percent)))
        self._is_active = bool(is_active)
        self._is_capturing = bool(is_capturing)
        self._xrun_count = int(xrun_count)
        self._ram_mb = float(ram_mb)
        if changed:
            self.update()

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
            ev.accept()
            return
        if ev.button() == Qt.RightButton:
            self.contextMenuRequested.emit(ev.globalPosition())
            ev.accept()
            return
        super().mousePressEvent(ev)

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w = self.width()
        h = self.height()

        # Background fill: ridge when active, plate otherwise
        if self._is_active:
            bg = QColor(EREBUS["ridge"])
        else:
            bg = QColor(EREBUS["plate"])
        p.setBrush(bg)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, w, h, 6, 6)

        # Active indicator: 2 px ember top border
        if self._is_active:
            p.setBrush(QColor(EREBUS["ember"]))
            p.setPen(Qt.NoPen)
            p.drawRect(0, 0, w, 2)

        # Top hairline (milled lip) on inactive chips
        if not self._is_active:
            p.setPen(QPen(QColor(242, 237, 223, int(0.10 * 255)), 1))
            p.drawLine(8, 1, w - 8, 1)

        # ── Slot name (Monaspace Neon, uppercase) ───────────────────
        name_font: QFont = self.font()
        fam = font_family("label").split(",")[0].strip().strip('"')
        if fam:
            name_font.setFamily(fam)
        name_font.setPointSize(9)
        name_font.setBold(True)
        name_font.setCapitalization(QFont.AllUppercase)
        name_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.2)
        p.setFont(name_font)
        name_color = (
            QColor(EREBUS["ember"]) if self._is_active else QColor(EREBUS["cream"])
        )
        p.setPen(name_color)
        name_rect = QRectF(10, 6, w - 40, 18)
        p.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, self._name or "—")

        # ── REC dot (top-right) when capturing ─────────────────────
        if self._is_capturing:
            p.setBrush(QColor(EREBUS["rec"]))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(w - 14.0, 14.0), 4.0, 4.0)

        # ── Fill bar (bottom-most 4 px) ────────────────────────────
        bar_y = h - 6
        bar_h = 3
        bar_x = 10
        bar_w = w - 20
        # track
        track_color = QColor(242, 237, 223, int(0.10 * 255))
        p.setBrush(track_color)
        p.setPen(Qt.NoPen)
        p.drawRect(bar_x, bar_y, bar_w, bar_h)
        # fill
        if self._fill_percent > 0:
            fill_len = int(bar_w * self._fill_percent / 100.0)
            fill_color = QColor(
                EREBUS["ember"] if self._is_active else EREBUS["signal"]
            )
            p.setBrush(fill_color)
            p.drawRect(bar_x, bar_y, max(1, fill_len), bar_h)

        # ── Status line (RAM + xruns) ──────────────────────────────
        status_font = p.font()
        status_font.setPointSize(7)
        status_font.setBold(False)
        p.setFont(status_font)
        bone = QColor(EREBUS["bone"])
        p.setPen(bone)
        status_rect = QRectF(10, h - 22, w - 20, 12)
        status_text = f"{self._ram_mb:5.0f} MB   XR {self._xrun_count:02d}"
        p.drawText(
            status_rect,
            Qt.AlignLeft | Qt.AlignVCenter,
            status_text,
        )

        p.end()
