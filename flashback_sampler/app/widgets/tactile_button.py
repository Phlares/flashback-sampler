"""
TactileButton — QPushButton subclass with the Erebus tactile-pad look.

Spec (from the frontend-design skill pass):
- Primary variant: 6 px radius, `ridge` fill, label cream, 2 px `ember`
  tell bar flush at the bottom edge, 1 px top-inside cream hairline
  (emulates milled edge).
- Secondary variant: same shape but no ember bar, `plate` fill.
- Hover: background steps up one surface tier, tell bar goes to
  ember_hot on primary.
- Pressed: content shifts down 1 px, 1 px `void` line paints at the
  top edge (inset / depressed state), tell bar goes to ember_deep.
- Disabled: text → ash, tell bar → dim ember.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QPushButton

from flashback_sampler.app.theme import EREBUS, font_family


class TactileButton(QPushButton):
    """
    QPushButton with custom paintEvent. Usage:
        b = TactileButton("CHECK OUT", variant="primary")
        b2 = TactileButton("FLUSH", variant="secondary")
    """

    def __init__(
        self,
        text: str = "",
        variant: str = "secondary",
        parent=None,
    ):
        super().__init__(text, parent)
        self._variant = variant
        self.setFlat(True)  # suppress Qt's default painting
        self.setCursor(Qt.PointingHandCursor)
        # Buttons don't take keyboard focus — Space is reserved for
        # the Preview shortcut, and keyboard activation of arbitrary
        # buttons would be too easy a footgun.
        self.setFocusPolicy(Qt.NoFocus)
        # Size hints — kept compact; main window overrides where needed
        self.setMinimumHeight(44 if variant == "secondary" else 52)
        self.setMinimumWidth(120)

    # Keep parent setText/etc. methods; override only paint & focus

    def variant(self) -> str:
        return self._variant

    def setVariant(self, variant: str) -> None:  # noqa: N802
        if variant != self._variant:
            self._variant = variant
            self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w = self.width()
        h = self.height()
        if w < 4 or h < 4:
            p.end()
            return

        radius = 6
        is_primary = self._variant == "primary"
        is_enabled = self.isEnabled()
        is_pressed = self.isDown()
        is_hover = self.underMouse() and is_enabled

        # ── Background fill ──────────────────────────────────────────
        if not is_enabled:
            fill = QColor(EREBUS["plate"])
        elif is_pressed:
            fill = QColor(EREBUS["void"])
        elif is_hover:
            fill = QColor(EREBUS["ridge"] if is_primary else EREBUS["ridge"])
        else:
            fill = QColor(EREBUS["ridge"] if is_primary else EREBUS["plate"])
        p.setBrush(fill)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, w, h, radius, radius)

        # ── 1 px cream hairline along the top edge (milled lip) ─────
        if is_enabled and not is_pressed:
            p.setPen(QPen(QColor(242, 237, 223, int(0.18 * 255)), 1))
            p.drawLine(radius, 1, w - radius, 1)

        # ── Pressed-state inset shadow along the top edge ────────────
        if is_pressed and is_enabled:
            p.setPen(QPen(QColor(0, 0, 0, 200), 1))
            p.drawLine(radius, 1, w - radius, 1)

        # ── Primary tell bar (2 px ember strip at bottom edge) ───────
        if is_primary:
            if not is_enabled:
                bar_color = QColor("#3a1a0e")  # dim ember for disabled
            elif is_pressed:
                bar_color = QColor(EREBUS["ember_deep"])
            elif is_hover:
                bar_color = QColor(EREBUS["ember_hot"])
            else:
                bar_color = QColor(EREBUS["ember"])
            p.setBrush(bar_color)
            p.setPen(Qt.NoPen)
            # Round only the bottom corners of the bar to match the
            # button's rounded rectangle
            bar_h = 2
            p.drawRoundedRect(
                0, h - bar_h - 1, w, bar_h + 1, radius, radius
            )
            # Paint over the top of the rounded bar so we only see the
            # bottom rounded corners (a flat top edge meeting the fill)
            p.setBrush(fill)
            p.setPen(Qt.NoPen)
            p.drawRect(0, h - bar_h - 1 - radius, w, radius)

        # ── Label ────────────────────────────────────────────────────
        label_text = self.text()
        if label_text:
            font: QFont = self.font()
            # Parse the first family out of our QSS-style stack
            fam = font_family("label").split(",")[0].strip().strip('"')
            if fam:
                font.setFamily(fam)
            font.setPointSize(9)
            font.setBold(False)
            font.setCapitalization(QFont.AllUppercase)
            font.setLetterSpacing(QFont.AbsoluteSpacing, 1.4)
            p.setFont(font)

            if not is_enabled:
                text_color = QColor(EREBUS["ash"])
            elif is_primary:
                text_color = QColor(EREBUS["ember"])
            else:
                text_color = QColor(EREBUS["cream"])
            p.setPen(text_color)

            # Content rect — shift down 1 px when pressed to sell the
            # depressed feel
            inner = QRect(0, 0, w, h - 3)  # reserve 3 px for the tell bar
            if is_pressed:
                inner.translate(0, 1)
            p.drawText(inner, Qt.AlignCenter, label_text)

        p.end()
