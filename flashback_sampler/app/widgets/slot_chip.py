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
CHIP_HEIGHT = 68

# Prime-toggle hit rectangle in the chip's top-right corner
PRIME_BTN_X = CHIP_WIDTH - 28
PRIME_BTN_Y = 4
PRIME_BTN_W = 24
PRIME_BTN_H = 24


def short_source_name(full: str, max_chars: int = 18) -> str:
    """
    Shorten a capture device name for the chip's source label line.
    Strips well-known trailing tags ("[loopback]", "[default]") and
    any final "(...)" qualifier, then truncates to `max_chars` with
    an ellipsis. Empty / missing → "—".
    """
    if not full:
        return "—"
    s = str(full).strip()
    for suffix in (" [loopback]", " [default]"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip()
    # Strip a trailing parenthesized qualifier (e.g. "(Realtek(R) Audio)")
    if s.endswith(")"):
        open_idx = s.rfind(" (")
        if open_idx > 0:
            s = s[:open_idx].rstrip()
    if len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s or "—"


class SlotChip(QWidget):
    """
    Visual representation of one CaptureSlot.

    Two independent click actions:
      - Click anywhere EXCEPT the top-right REC button → emit
        `clicked()` so the host switches the active-focus slot.
      - Click the REC button in the top-right → emit `primeToggled()`
        so the host starts or stops the slot's capture
        independently of which slot is currently active-focused.
      - Right-click anywhere → emit contextMenuRequested(QPointF).

    Active-focus and primed-state are orthogonal — a slot can be
    primed but not active (capturing in the background while the
    user is watching a different slot), or active but not primed
    (UI is driving it, but it's frozen / held for review).
    """

    clicked = Signal()
    primeToggled = Signal()
    contextMenuRequested = Signal(QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._name: str = ""
        self._source_short: str = ""
        self._fill_percent: float = 0.0
        self._is_active: bool = False
        self._is_capturing: bool = False
        self._is_armed: bool = True
        self._is_rolling: bool = False
        self._has_error: bool = False
        self._xrun_count: int = 0
        self._ram_mb: float = 0.0
        self.setFixedSize(CHIP_WIDTH, CHIP_HEIGHT)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        # Buttons shouldn't steal keyboard focus — spacebar is
        # reserved for Preview. Chips are click-to-switch only.
        self.setFocusPolicy(Qt.NoFocus)

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
        source_short: str = "",
        is_armed: bool = True,
        is_rolling: bool = False,
        has_error: bool = False,
    ) -> None:
        changed = (
            name != self._name
            or fill_percent != self._fill_percent
            or is_active != self._is_active
            or is_capturing != self._is_capturing
            or is_armed != self._is_armed
            or is_rolling != self._is_rolling
            or has_error != self._has_error
            or xrun_count != self._xrun_count
            or abs(ram_mb - self._ram_mb) > 0.05
            or source_short != self._source_short
        )
        self._name = name
        self._source_short = source_short
        self._fill_percent = max(0.0, min(100.0, float(fill_percent)))
        self._is_active = bool(is_active)
        self._is_capturing = bool(is_capturing)
        self._is_armed = bool(is_armed)
        self._is_rolling = bool(is_rolling)
        self._has_error = bool(has_error)
        self._xrun_count = int(xrun_count)
        self._ram_mb = float(ram_mb)
        if changed:
            self.update()

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def _in_prime_button(self, x: float, y: float) -> bool:
        return (
            PRIME_BTN_X <= x <= PRIME_BTN_X + PRIME_BTN_W
            and PRIME_BTN_Y <= y <= PRIME_BTN_Y + PRIME_BTN_H
        )

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        px = ev.position().x()
        py = ev.position().y()
        if ev.button() == Qt.LeftButton:
            # Prime-toggle hit area takes priority over the main
            # active-focus click, so you can prime / unprime without
            # first switching which slot the UI is showing.
            if self._in_prime_button(px, py):
                self.primeToggled.emit()
                ev.accept()
                return
            self.clicked.emit()
            ev.accept()
            return
        if ev.button() == Qt.RightButton:
            self.contextMenuRequested.emit(ev.globalPosition())
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        # Hover cursor: pointing hand over the body, forbidden cursor
        # nowhere — just keep the pointer consistent. (The prime button
        # could flip to a different cursor, but PointingHand for the
        # whole chip keeps the signal simple.)
        super().mouseMoveEvent(ev)

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
        name_rect = QRectF(10, 6, w - 40, 16)
        p.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, self._name or "—")

        # ── Source line (device short name, bone — red on error) ────
        if self._source_short:
            src_font = self.font()
            if fam:
                src_font.setFamily(fam)
            src_font.setPointSize(7)
            src_font.setBold(False)
            src_font.setCapitalization(QFont.AllUppercase)
            src_font.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
            p.setFont(src_font)
            p.setPen(
                QColor(EREBUS["rec"]) if self._has_error
                else QColor(EREBUS["bone"])
            )
            src_rect = QRectF(10, 24, w - 20, 12)
            p.drawText(
                src_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                self._source_short,
            )

        # ── Arm button (top-right corner) ───────────────────────────
        # Click toggles `slot.armed`. Visual states:
        #   capturing (armed + rolling): solid rec disc + halo
        #   armed + not rolling:         dim ember outline (queued)
        #   not armed:                   hollow bone outline
        btn_cx = PRIME_BTN_X + PRIME_BTN_W / 2.0
        btn_cy = PRIME_BTN_Y + PRIME_BTN_H / 2.0
        center = QPointF(btn_cx, btn_cy)
        if self._is_capturing:
            # Halo first, then solid on top
            halo = QColor(EREBUS["rec"])
            halo.setAlpha(int(0.25 * 255))
            p.setBrush(halo)
            p.setPen(Qt.NoPen)
            p.drawEllipse(center, 8.5, 8.5)
            p.setBrush(QColor(EREBUS["rec"]))
            p.drawEllipse(center, 5.5, 5.5)
        elif self._is_armed:
            # Queued: ember ring with faint fill
            ember = QColor(EREBUS["ember"])
            fill = QColor(EREBUS["ember"])
            fill.setAlphaF(0.20)
            p.setBrush(fill)
            p.setPen(QPen(ember, 1.4))
            p.drawEllipse(center, 5.5, 5.5)
        else:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(EREBUS["bone"]), 1.2))
            p.drawEllipse(center, 5.5, 5.5)

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
