"""
CaptureAllButton — master transport for the whole session.

Lives at the left edge of the SourceStrip. Starts or stops capture on
every armed CaptureSlot at once. Doubles as a status display: the
button face shows one concentric-ring "channel indicator" per slot,
each filling with `rec` when that slot is armed, going solid when
it's actually capturing (rolling). A count badge to the right shows
`armed / total`.

States driven by (armed_count, total, rolling):
  stopped + some armed  -> label "START CAPTURE", dots are dim
                           ember outlines for armed slots
  stopped + 0 armed     -> label "START CAPTURE", no dots filled,
                           button still clickable (no-op; status bar
                           warns)
  rolling + any armed   -> label "STOP CAPTURE" in ember_hot, bottom
                           tell bar is `rec`, pulses at 1.6 s period;
                           dots of armed slots are filled `rec`
  rolling + 0 armed     -> label "STOP CAPTURE" (still clickable, just
                           drops the rolling flag)

Pulse reflects ROLLING, not "all armed." Max 8 visible dots; beyond
that, truncate as "… +N".
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QPushButton

from flashback_sampler.app.theme import EREBUS, font_family


CAPTURE_ALL_WIDTH = 188
CAPTURE_ALL_HEIGHT = 104
MAX_VISIBLE_DOTS = 8
PULSE_PERIOD_MS = 1600
PULSE_FRAME_MS = 33  # ~30 fps


class CaptureAllButton(QPushButton):
    """
    QPushButton with a fully custom paintEvent. Emits the standard
    `clicked` signal on click. The host (main_window) decides what
    that means — prime-all when `is_all_primed()` is False, stop-all
    otherwise.
    """

    def __init__(self, parent=None):
        super().__init__("START CAPTURE", parent)
        self.setFlat(True)
        self.setCursor(Qt.PointingHandCursor)
        # No keyboard focus — Space is reserved for Preview; the
        # global Ctrl+R shortcut drives the master transport instead.
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(CAPTURE_ALL_WIDTH, CAPTURE_ALL_HEIGHT)

        self._armed: int = 0
        self._total: int = 1
        self._rolling: bool = False
        self._pulse_phase: float = 0.0  # 0.0 .. 1.0

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(PULSE_FRAME_MS)
        self._pulse_timer.timeout.connect(self._advance_pulse)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def set_state(
        self,
        armed_count: int,
        total_count: int,
        is_rolling: bool,
    ) -> None:
        """
        Push current (armed, total, rolling) into the button. Armed
        count is how many slots will participate when capture starts;
        `is_rolling` is whether the master transport is currently
        running.
        """
        armed_count = max(0, int(armed_count))
        total_count = max(1, int(total_count))
        if armed_count > total_count:
            armed_count = total_count
        is_rolling = bool(is_rolling)

        changed = (
            armed_count != self._armed
            or total_count != self._total
            or is_rolling != self._rolling
        )
        self._armed = armed_count
        self._total = total_count
        self._rolling = is_rolling

        new_text = "STOP CAPTURE" if is_rolling else "START CAPTURE"
        if self.text() != new_text:
            self.setText(new_text)

        # Pulse ONLY while rolling — the "recording live" cue. Arming
        # alone is quiet intent and doesn't pulse.
        if is_rolling:
            if not self._pulse_timer.isActive():
                self._pulse_timer.start()
        else:
            if self._pulse_timer.isActive():
                self._pulse_timer.stop()
                self._pulse_phase = 0.0

        if changed:
            self.update()

    # Preserved name for compat with any tests / external callers that
    # still ask "how many dots are lit?"
    def armed_count(self) -> int:
        return self._armed

    def total_count(self) -> int:
        return self._total

    def is_rolling(self) -> bool:
        return self._rolling

    # ------------------------------------------------------------------
    # Pulse animation
    # ------------------------------------------------------------------

    def _advance_pulse(self) -> None:
        frames_per_period = PULSE_PERIOD_MS / PULSE_FRAME_MS
        self._pulse_phase = (self._pulse_phase + 1.0 / frames_per_period) % 1.0
        self.update()

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w = self.width()
        h = self.height()
        radius = 8

        is_enabled = self.isEnabled()
        is_pressed = self.isDown()
        is_hover = self.underMouse() and is_enabled
        is_rolling = self._rolling

        # ── Body fill ─────────────────────────────────────────────────
        if not is_enabled:
            body_color = QColor(EREBUS["plate"])
        elif is_pressed:
            body_color = QColor(EREBUS["void"])
        elif is_hover:
            body_color = QColor(EREBUS["ridge"]).lighter(115)
        else:
            body_color = QColor(EREBUS["ridge"])
        p.setBrush(body_color)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(0, 0, w, h, radius, radius)

        # ── Top cream hairline (milled lip) ───────────────────────────
        if is_enabled and not is_pressed:
            p.setPen(QPen(QColor(242, 237, 223, int(0.22 * 255)), 1))
            p.drawLine(radius, 1, w - radius, 1)

        # ── Pressed-state inset ───────────────────────────────────────
        if is_pressed and is_enabled:
            p.setPen(QPen(QColor(0, 0, 0, 210), 1))
            p.drawLine(radius, 1, w - radius, 1)

        # ── Bottom tell bar ───────────────────────────────────────────
        # Ember when the CTA is "start capture"; rec and pulsing when
        # the session is rolling (the button is now a session-wide
        # record indicator).
        bar_h = 2
        if not is_enabled:
            bar_color = QColor("#3a1a0e")
        elif is_rolling:
            base = QColor(EREBUS["rec"])
            if self._pulse_timer.isActive():
                sin_val = 0.5 + 0.5 * math.sin(
                    self._pulse_phase * 2.0 * math.pi
                )
                alpha = 0.65 + 0.35 * sin_val
                base.setAlphaF(alpha)
            bar_color = base
        elif is_pressed:
            bar_color = QColor(EREBUS["ember_deep"])
        elif is_hover:
            bar_color = QColor(EREBUS["ember_hot"])
        else:
            bar_color = QColor(EREBUS["ember"])
        p.setBrush(bar_color)
        p.setPen(Qt.NoPen)
        # Bottom-rounded strip: draw a rounded rect then cap the top
        # of it with the body color so only the bottom corners round.
        p.drawRoundedRect(
            0, h - bar_h - 1, w, bar_h + 1, radius, radius
        )
        p.setBrush(body_color)
        p.drawRect(0, h - bar_h - 1 - radius, w, radius)

        # ── Line 1: label (Monaspace Neon, bold, letter-spaced) ──────
        label_text = self.text()
        if label_text:
            font: QFont = self.font()
            fam = font_family("label").split(",")[0].strip().strip('"')
            if fam:
                font.setFamily(fam)
            font.setPointSize(11)
            font.setBold(True)
            font.setCapitalization(QFont.AllUppercase)
            font.setLetterSpacing(QFont.AbsoluteSpacing, 1.6)
            p.setFont(font)

            if not is_enabled:
                text_color = QColor(EREBUS["ash"])
            elif is_rolling:
                text_color = QColor(EREBUS["ember_hot"])
            else:
                text_color = QColor(EREBUS["ember"])
            p.setPen(text_color)

            label_rect = QRectF(0, 14, w, 28)
            if is_pressed:
                label_rect.translate(0, 1)
            p.drawText(label_rect, Qt.AlignCenter, label_text)

        # ── Line 2: indicator dots + count badge ─────────────────────
        self._paint_indicator_row(p, w, h, is_rolling, is_pressed)

        p.end()

    def _paint_indicator_row(
        self,
        p: QPainter,
        w: int,
        h: int,
        is_rolling: bool,
        is_pressed: bool,
    ) -> None:
        total = self._total
        if total <= 0:
            return

        visible = min(total, MAX_VISIBLE_DOTS)
        truncated = total > MAX_VISIBLE_DOTS

        dot_r = 5  # radius
        gap = 8  # px between dot centers (beyond the diameter)
        dot_pitch = (dot_r * 2) + gap

        # Mono badge font
        badge_font: QFont = self.font()
        fam_b = font_family("display").split(",")[0].strip().strip('"')
        if fam_b:
            badge_font.setFamily(fam_b)
        badge_font.setPointSize(9)
        badge_font.setBold(False)
        p.setFont(badge_font)
        fm = p.fontMetrics()

        count_text = f"{self._armed}/{total}"
        count_w = fm.horizontalAdvance(count_text)

        # Width budget
        dots_width = visible * (dot_r * 2) + (visible - 1) * gap
        trunc_w = fm.horizontalAdvance("+99") + 6 if truncated else 0
        badge_gap = 12
        total_w = dots_width + trunc_w + badge_gap + count_w

        y_center = 66
        start_x = (w - total_w) / 2.0

        # ── Dots ─────────────────────────────────────────────────────
        # Armed-and-rolling: solid rec disc (actively recording).
        # Armed-but-stopped: ember ring (queued for next capture).
        # Slots beyond armed_count: bone outline only.
        cx = start_x + dot_r
        for i in range(visible):
            center = QPointF(cx, y_center)
            ring = QColor(EREBUS["bone"])
            ring.setAlphaF(0.60)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(ring, 1))
            p.drawEllipse(center, dot_r, dot_r)
            if i < self._armed:
                if is_rolling:
                    p.setBrush(QColor(EREBUS["rec"]))
                    p.setPen(Qt.NoPen)
                    p.drawEllipse(center, dot_r - 1.5, dot_r - 1.5)
                else:
                    # Queued: dim ember ring inside the bone outline
                    ember = QColor(EREBUS["ember"])
                    ember.setAlphaF(0.75)
                    p.setBrush(Qt.NoBrush)
                    p.setPen(QPen(ember, 1.4))
                    p.drawEllipse(center, dot_r - 1.5, dot_r - 1.5)
            cx += dot_pitch

        # Truncation indicator "+N" in bone
        if truncated:
            overflow = total - MAX_VISIBLE_DOTS
            bone = QColor(EREBUS["bone"])
            p.setPen(bone)
            trunc_rect = QRectF(
                cx - dot_r,
                y_center - 8,
                trunc_w + 4,
                16,
            )
            p.drawText(trunc_rect, Qt.AlignLeft | Qt.AlignVCenter, f"+{overflow}")
            cx += trunc_w

        # ── Count badge (N/M) to the right of the dots ───────────────
        badge_color = (
            QColor(EREBUS["cream"]) if is_rolling else QColor(EREBUS["bone"])
        )
        p.setPen(badge_color)
        badge_rect = QRectF(
            cx - dot_r + badge_gap,
            y_center - 8,
            count_w + 8,
            16,
        )
        p.drawText(badge_rect, Qt.AlignLeft | Qt.AlignVCenter, count_text)
