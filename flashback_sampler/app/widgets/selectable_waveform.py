"""
SelectableWaveform — WaveformView subclass with drag-to-select and
right-click-for-context-menu interaction.

Used by BufferTrack for the live buffer view so the user can mark a
region on the waveform directly and right-click to "Check Out Segment."
Distinct from `ClickableWaveform` (which does click-to-seek for the
checkout clip in Track 2).

The widget reports selection positions purely as fractions in [0, 1] —
the owning controller is responsible for translating fractions to
absolute sample positions (so the selection stays pinned to real audio
even as the live waveform scrolls).
"""

from __future__ import annotations

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPen

from flashback_sampler.app.theme import EREBUS
from flashback_sampler.app.widgets.waveform_view import (
    WaveformView,
    _paint_selection_duration_label,
)


# How close (in widget pixels) the cursor must be to an existing
# selection edge to grab it for dragging. 6 px is comfortable with a
# mouse; a finer pointing device may want less.
EDGE_GRAB_PX = 6


class SelectableWaveform(WaveformView):
    """
    WaveformView that supports:
    - Left click-drag on empty space: paint a new manual selection
      band and emit manualSelectionChanged on release.
    - Left click-drag on an existing mark-in/out edge: slide that
      edge and emit manualSelectionChanged on release. The cursor
      changes to Qt.SizeHorCursor while hovering over an edge.
    - Right click: emit contextMenuRequested with the global position.
    - Double click: clear the current manual selection.
    """

    manualSelectionChanged = Signal(float, float)  # start_frac, end_frac
    manualSelectionCleared = Signal()
    contextMenuRequested = Signal(QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manual_start: float | None = None
        self._manual_end: float | None = None
        self._drag_anchor: float | None = None  # mouse press x, as frac
        self._is_dragging: bool = False
        self._dragging_edge: str | None = None  # "start" | "end" | None
        self._idle_cursor: Qt.CursorShape = Qt.CrossCursor
        self.setMouseTracking(True)
        self.setCursor(self._idle_cursor)

    # ------------------------------------------------------------------
    # Public API for the controller
    # ------------------------------------------------------------------

    def has_manual_selection(self) -> bool:
        return (
            self._manual_start is not None
            and self._manual_end is not None
            and self._manual_end > self._manual_start
        )

    def manual_selection(self) -> tuple[float, float] | None:
        if not self.has_manual_selection():
            return None
        return (float(self._manual_start), float(self._manual_end))

    def is_user_interacting(self) -> bool:
        """True while the user is actively dragging — either painting a
        new selection or sliding an existing edge. Controllers should not
        overwrite the manual selection while this returns True, otherwise
        a periodic refresh tick will snap the drag back to its prior
        position."""
        return self._is_dragging or self._dragging_edge is not None

    def set_manual_selection(
        self,
        start_frac: float | None,
        end_frac: float | None,
    ) -> None:
        """
        Called by the controller (e.g. BufferTrack) to reflect a
        selection whose position has been recomputed from absolute
        samples. Does NOT emit manualSelectionChanged — that signal
        is reserved for user-initiated changes.
        """
        if start_frac is None or end_frac is None or end_frac <= start_frac:
            self._manual_start = None
            self._manual_end = None
        else:
            self._manual_start = float(start_frac)
            self._manual_end = float(end_frac)
        self.update()

    def clear_manual_selection(self) -> None:
        if self._manual_start is None and self._manual_end is None:
            return
        self._manual_start = None
        self._manual_end = None
        self.update()
        self.manualSelectionCleared.emit()

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def _pos_frac(self, x: float) -> float:
        if self.width() <= 0:
            return 0.0
        return max(0.0, min(1.0, x / self.width()))

    def _inner_bounds(self) -> tuple[int, int]:
        """Mirror the (inner_x, inner_w) used by WaveformView.paintEvent."""
        w = self.width()
        inner_x = 6
        inner_w = max(1, w - inner_x - 6)
        return inner_x, inner_w

    def _edge_at(self, x: float) -> str | None:
        """
        Return "start" or "end" if the widget x-coordinate is within
        EDGE_GRAB_PX of an existing selection edge, else None.
        """
        if not self.has_manual_selection():
            return None
        inner_x, inner_w = self._inner_bounds()
        start_x = inner_x + float(self._manual_start) * inner_w
        end_x = inner_x + float(self._manual_end) * inner_w
        # If the two edges are very close, prefer whichever is nearest
        d_start = abs(x - start_x)
        d_end = abs(x - end_x)
        if d_start <= EDGE_GRAB_PX and d_start <= d_end:
            return "start"
        if d_end <= EDGE_GRAB_PX:
            return "end"
        return None

    def _refresh_hover_cursor(self, x: float) -> None:
        """Update the mouse cursor based on whether we're over an edge."""
        if self._edge_at(x) is not None:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(self._idle_cursor)

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            # Priority 1: grab an existing mark edge and drag it
            edge = self._edge_at(ev.position().x())
            if edge is not None:
                self._dragging_edge = edge
                self._is_dragging = False
                self._drag_anchor = None
                self.setCursor(Qt.SizeHorCursor)
                ev.accept()
                return
            # Priority 2: begin a new selection drag
            self._drag_anchor = self._pos_frac(ev.position().x())
            self._is_dragging = True
            ev.accept()
            return
        if ev.button() == Qt.RightButton:
            # Context menu: let the host decide what to show based on
            # whether a manual selection currently exists.
            self.contextMenuRequested.emit(ev.globalPosition())
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        x = ev.position().x()

        # Edge-drag takes priority over new-selection drag
        if self._dragging_edge is not None and self.has_manual_selection():
            new_frac = self._pos_frac(x)
            # Leave at least one widget pixel between the two edges
            inner_x, inner_w = self._inner_bounds()
            epsilon = 1.0 / max(1, inner_w)
            if self._dragging_edge == "start":
                self._manual_start = max(
                    0.0, min(float(self._manual_end) - epsilon, new_frac)
                )
            else:  # "end"
                self._manual_end = min(
                    1.0, max(float(self._manual_start) + epsilon, new_frac)
                )
            self.update()
            ev.accept()
            return

        if self._is_dragging and self._drag_anchor is not None:
            cur = self._pos_frac(x)
            lo = min(self._drag_anchor, cur)
            hi = max(self._drag_anchor, cur)
            self._manual_start = lo
            self._manual_end = hi
            self.update()
            ev.accept()
            return

        # Hover: update the cursor based on proximity to an edge
        if ev.buttons() == Qt.NoButton:
            self._refresh_hover_cursor(x)

        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            # Finish an edge drag
            if self._dragging_edge is not None:
                self._dragging_edge = None
                self._refresh_hover_cursor(ev.position().x())
                if self.has_manual_selection():
                    self.manualSelectionChanged.emit(
                        float(self._manual_start), float(self._manual_end)
                    )
                ev.accept()
                return
            # Finish a new-selection drag
            if self._is_dragging:
                self._is_dragging = False
                self._drag_anchor = None
                if self.has_manual_selection():
                    self.manualSelectionChanged.emit(
                        float(self._manual_start), float(self._manual_end)
                    )
                else:
                    self._manual_start = None
                    self._manual_end = None
                    self.update()
                    self.manualSelectionCleared.emit()
                ev.accept()
                return
        super().mouseReleaseEvent(ev)

    def leaveEvent(self, ev) -> None:  # noqa: N802
        # Reset to the idle cursor when the mouse leaves the widget
        self.setCursor(self._idle_cursor)
        super().leaveEvent(ev)

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            self.clear_manual_selection()
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    # ------------------------------------------------------------------
    # Paint — augments the base WaveformView with a manual selection
    # band, drawn in a slightly stronger ember than the anchor section
    # so the user can tell them apart at a glance.
    # ------------------------------------------------------------------

    def paintEvent(self, ev) -> None:  # noqa: N802
        super().paintEvent(ev)
        if not self.has_manual_selection():
            return

        # Re-derive the inner content rect the same way WaveformView does
        w = self.width()
        h = self.height()
        label_strip = 18
        inner_top = 1 + label_strip
        inner_x = 6
        inner_w = w - inner_x - 6
        inner_y = inner_top
        inner_h = h - inner_top - 6
        if inner_w <= 2 or inner_h <= 2:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        s = float(self._manual_start)
        e = float(self._manual_end)
        x1 = inner_x + s * inner_w
        x2 = inner_x + e * inner_w

        # Slightly brighter translucent ember for manual selection
        fill = QColor(EREBUS["ember"])
        fill.setAlpha(int(0.22 * 255))
        p.fillRect(
            QRectF(x1, float(inner_y), max(0.5, x2 - x1), float(inner_h)),
            fill,
        )
        # Solid ember edges on both sides — manual selection is
        # committed; the anchor's "dashed start / solid end" semantics
        # don't apply here.
        edge_pen = QPen(QColor(EREBUS["ember"]), 2)
        p.setPen(edge_pen)
        p.drawLine(QLineF(x1, float(inner_y), x1, float(inner_y + inner_h)))
        p.drawLine(QLineF(x2, float(inner_y), x2, float(inner_y + inner_h)))

        # Duration label centered on the band — uses the timeline
        # total_seconds to convert the frac span to real time.
        if self._timeline_total_s > 0:
            dur = (e - s) * self._timeline_total_s
            _paint_selection_duration_label(
                p,
                x1=x1,
                x2=x2,
                y_top=float(inner_y),
                y_bot=float(inner_y + inner_h),
                duration_seconds=dur,
            )
        p.end()
