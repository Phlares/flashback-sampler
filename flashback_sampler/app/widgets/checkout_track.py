"""
CheckoutTrack — Track 2, shown when a checkout is selected.

Renders the selected Checkout's audio as a static peak-bin waveform,
with an ember playhead that follows ScrubPlayer.cursor_seconds. Click
on the waveform to seek. Shift+click-drag to paint a trim selection.
Right-click to open the clip context menu (Export Selection, Set
Mark-In / Mark-Out to Playhead, Clear Trim).
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from flashback_sampler.app.theme import EREBUS
from flashback_sampler.app.time_format import format_time_cs
from flashback_sampler.app.widgets.selectable_waveform import SelectableWaveform


N_CLIP_BINS = 500


def _compute_clip_bins(audio: np.ndarray, n_bins: int) -> np.ndarray:
    """
    Pure helper: downsample a Checkout.audio ndarray to (n_bins, 2, ch)
    min/max peak bins — same shape as AudioCircularBuffer.get_peak_bins.
    Extracted so tests can verify the math without a Checkout or a Qt
    widget.
    """
    if audio.ndim != 2:
        raise ValueError("audio must be (N, channels)")
    n, ch = audio.shape
    out = np.zeros((n_bins, 2, ch), dtype=np.float32)
    if n == 0:
        return out
    edges = np.linspace(0, n, n_bins + 1, dtype=np.int64)
    for i in range(n_bins):
        a, b = int(edges[i]), int(edges[i + 1])
        if b <= a:
            if i > 0:
                out[i] = out[i - 1]
            continue
        chunk = audio[a:b]
        out[i, 0] = chunk.min(axis=0)
        out[i, 1] = chunk.max(axis=0)
    return out


class ClipWaveform(SelectableWaveform):
    """
    SelectableWaveform subclass that adds click-to-seek alongside the
    inherited Shift+drag-to-select and edge-drag behaviours.

    Interaction priority on left click:
      1. Cursor over an existing mark-in/out edge → drag that edge
         (inherited from SelectableWaveform)
      2. Shift modifier → start a new trim selection drag (inherited)
      3. Otherwise → click-to-seek / drag-to-scrub the playhead
    Right click → contextMenuRequested. Double-click → clear trim.
    """

    seekRequested = Signal(float)  # 0..1 horizontal fraction

    def __init__(self, parent=None):
        super().__init__(parent)
        # ClipWaveform's idle mode is click-to-seek, not drag-to-select,
        # so the default cursor is ArrowCursor. SelectableWaveform still
        # flips it to SizeHorCursor when hovering an edge.
        self._idle_cursor = Qt.ArrowCursor
        self.setCursor(self._idle_cursor)

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.LeftButton:
            # Priority 1: edge drag (base-class handles the state)
            if self._edge_at(ev.position().x()) is not None:
                super().mousePressEvent(ev)
                return
            # Priority 2: Shift-drag for new trim selection
            if ev.modifiers() & Qt.ShiftModifier:
                super().mousePressEvent(ev)
                return
            # Priority 3: click-to-seek
            if self.width() > 0:
                frac = max(0.0, min(1.0, ev.position().x() / self.width()))
                self.seekRequested.emit(frac)
            ev.accept()
            return
        # Right click and others → SelectableWaveform handles them
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        # If a trim drag / edge drag is in progress, let
        # SelectableWaveform keep updating it. Otherwise a bare left
        # drag = continuous click-to-seek (scrubbing the playhead).
        if self._is_dragging or self._dragging_edge is not None:
            super().mouseMoveEvent(ev)
            return
        if ev.buttons() & Qt.LeftButton and self.width() > 0:
            frac = max(0.0, min(1.0, ev.position().x() / self.width()))
            self.seekRequested.emit(frac)
            ev.accept()
            return
        # Hover path — let base class update the cursor
        super().mouseMoveEvent(ev)


class CheckoutTrack(QWidget):
    """
    Visualizes one Checkout. The owning controller (main window) sets the
    checkout via `set_checkout(co)` and feeds playback position via
    `set_cursor(seconds)` on every tick.
    """

    seekRequested = Signal(float)  # seconds into the clip
    trimChanged = Signal(int, int)  # trim_in_samples, trim_out_samples
    trimCleared = Signal()
    contextMenuRequested = Signal(QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checkout = None  # current Checkout (kept for trim updates)
        self._checkout_id: str | None = None
        self._duration_s: float = 0.0
        # When True, ScrubPlayer is playing a slice of the full clip
        # (Checkout.trimmed_audio()) and its cursor_seconds is
        # relative to the trim-in point. set_cursor() shifts the
        # playhead forward by trim_in when computing the frac.
        self._preview_trimmed: bool = False
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        # top caption row
        top = QHBoxLayout()
        top.setSpacing(12)
        self._caption = QLabel("— NO CHECKOUT SELECTED —")
        self._caption.setProperty("role", "label")
        self._caption.setStyleSheet(f"color: {EREBUS['ember']};")
        top.addWidget(self._caption)
        top.addStretch(1)
        self._meta = QLabel("")
        self._meta.setProperty("role", "label")
        top.addWidget(self._meta)
        root.addLayout(top, 0)

        # waveform
        self._wave = ClipWaveform(self)
        self._wave.setMinimumHeight(110)
        self._wave.set_labels("CLIP", "— — —")
        self._wave.seekRequested.connect(self._on_seek_frac)
        self._wave.manualSelectionChanged.connect(self._on_trim_drag_committed)
        self._wave.manualSelectionCleared.connect(self._on_trim_cleared)
        self._wave.contextMenuRequested.connect(self.contextMenuRequested.emit)
        root.addWidget(self._wave, 1)

        # bottom time row
        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        self._pos_label = QLabel("00:00 / 00:00")
        self._pos_label.setProperty("role", "label")
        bottom.addWidget(self._pos_label)
        bottom.addStretch(1)
        self._state_label = QLabel("")
        self._state_label.setProperty("role", "label")
        bottom.addWidget(self._state_label)
        root.addLayout(bottom, 0)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def set_checkout(self, checkout) -> None:
        """
        Bind a Checkout (from flashback_sampler.core.checkout.Checkout)
        or None to clear. Called whenever the list selection changes.
        """
        self._checkout = checkout
        if checkout is None:
            self._checkout_id = None
            self._duration_s = 0.0
            self._wave.set_data(None)
            self._wave.set_playhead(None)
            self._wave.clear_manual_selection()
            self._wave.set_labels("CLIP", "— — —")
            self._wave.set_timeline(total_seconds=0.0)
            self._caption.setText("— NO CHECKOUT SELECTED —")
            self._meta.setText("")
            self._pos_label.setText("00:00 / 00:00")
            self._state_label.setText("")
            return

        self._checkout_id = checkout.id
        self._duration_s = checkout.duration_seconds
        bins = _compute_clip_bins(checkout.audio, N_CLIP_BINS)
        self._wave.set_data(bins)
        self._wave.set_playhead(0.0)
        self._wave.set_labels(
            "CLIP",
            f"{checkout.sample_rate // 1000}K   {checkout.channels}CH",
        )
        # Clip timeline: 00:00 at the left, clip duration at the right
        self._wave.set_timeline(total_seconds=self._duration_s, anchor="left")
        self._caption.setText(f"CLIP  {checkout.id}")
        self._meta.setText(
            f"{checkout.ram_bytes / 1024 / 1024:.1f} MB   "
            f"{format_time_cs(self._duration_s)}"
        )
        self._pos_label.setText(f"00:00 / {format_time_cs(self._duration_s)}")
        self._state_label.setText(f"[{checkout.state.upper()}]")

        # Reflect any pre-existing trim on the widget
        self._refresh_trim_overlay_from_checkout()

    def _refresh_trim_overlay_from_checkout(self) -> None:
        co = self._checkout
        if co is None or co.audio.shape[0] == 0:
            self._wave.set_manual_selection(None, None)
            return
        n = co.audio.shape[0]
        ti = max(0, int(co.trim_in_samples))
        to = co.trim_out_samples if co.trim_out_samples > 0 else n
        if ti == 0 and to == n:
            self._wave.set_manual_selection(None, None)
            return
        self._wave.set_manual_selection(ti / n, to / n)

    def set_cursor(self, seconds: float) -> None:
        """
        Feed playback cursor position in seconds for the playhead.

        When `_preview_trimmed` is True, `seconds` is relative to the
        trim-in point (the ScrubPlayer is bound to trimmed audio), so we
        offset by trim_in_samples / sample_rate to get the absolute
        position within the full clip for painting and for the readout.
        """
        if self._duration_s <= 0:
            return
        absolute = float(seconds)
        co = self._checkout
        if self._preview_trimmed and co is not None and co.trim_in_samples > 0:
            absolute += co.trim_in_samples / co.sample_rate
        frac = max(0.0, min(1.0, absolute / self._duration_s))
        self._wave.set_playhead(frac)
        self._pos_label.setText(
            f"{format_time_cs(absolute)} / {format_time_cs(self._duration_s)}"
        )

    def set_preview_trimmed(self, trimmed: bool) -> None:
        """
        Tell the track whether the ScrubPlayer is currently playing
        trimmed audio (so set_cursor can apply the trim_in offset).
        """
        self._preview_trimmed = bool(trimmed)

    def current_checkout_id(self) -> str | None:
        return self._checkout_id

    # ------------------------------------------------------------------
    # Trim writes — mutate the bound Checkout in place
    # ------------------------------------------------------------------

    def set_mark_in(self, seconds: float) -> None:
        co = self._checkout
        if co is None:
            return
        n = co.audio.shape[0]
        ti = max(0, min(n, int(seconds * co.sample_rate)))
        to = co.trim_out_samples if co.trim_out_samples > 0 else n
        if ti >= to:
            return
        co.trim_in_samples = ti
        self._refresh_trim_overlay_from_checkout()
        self.trimChanged.emit(co.trim_in_samples, co.trim_out_samples)

    def set_mark_out(self, seconds: float) -> None:
        co = self._checkout
        if co is None:
            return
        n = co.audio.shape[0]
        to = max(0, min(n, int(seconds * co.sample_rate)))
        ti = max(0, int(co.trim_in_samples))
        if to <= ti:
            return
        co.trim_out_samples = to
        self._refresh_trim_overlay_from_checkout()
        self.trimChanged.emit(co.trim_in_samples, co.trim_out_samples)

    def clear_trim(self) -> None:
        co = self._checkout
        if co is None:
            return
        co.trim_in_samples = 0
        co.trim_out_samples = 0
        self._refresh_trim_overlay_from_checkout()
        self.trimCleared.emit()

    def trim_range_seconds(self) -> tuple[float, float] | None:
        """Return the current [in, out] range in seconds, or None if full."""
        co = self._checkout
        if co is None or co.audio.shape[0] == 0:
            return None
        n = co.audio.shape[0]
        ti = max(0, int(co.trim_in_samples))
        to = co.trim_out_samples if co.trim_out_samples > 0 else n
        if ti == 0 and to == n:
            return None
        return (ti / co.sample_rate, to / co.sample_rate)

    # ------------------------------------------------------------------
    # Internal slots
    # ------------------------------------------------------------------

    def _on_seek_frac(self, frac: float) -> None:
        if self._duration_s <= 0:
            return
        self.seekRequested.emit(frac * self._duration_s)

    def _on_trim_drag_committed(self, start_frac: float, end_frac: float) -> None:
        """Shift+drag on the clip waveform → set Checkout.trim_in/out."""
        co = self._checkout
        if co is None:
            return
        n = co.audio.shape[0]
        ti = max(0, min(n, int(round(start_frac * n))))
        to = max(ti + 1, min(n, int(round(end_frac * n))))
        co.trim_in_samples = ti
        co.trim_out_samples = to
        self.trimChanged.emit(ti, to)

    def _on_trim_cleared(self) -> None:
        co = self._checkout
        if co is None:
            return
        if co.trim_in_samples != 0 or co.trim_out_samples != 0:
            co.trim_in_samples = 0
            co.trim_out_samples = 0
            self.trimCleared.emit()
