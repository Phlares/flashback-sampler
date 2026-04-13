"""
CheckoutTrack — Track 2, shown when a checkout is selected.

Renders the selected Checkout's audio as a static peak-bin waveform,
with an ember playhead that follows ScrubPlayer.cursor_seconds. Click
on the waveform to seek. Trim handles and the full IN / PLAY / OUT
transport are stubs for M6.1.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from flashback_sampler.app.theme import EREBUS
from flashback_sampler.app.widgets.waveform_view import WaveformView


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


class ClickableWaveform(WaveformView):
    """WaveformView subclass that emits a click-to-seek signal."""

    seekRequested = Signal(float)  # 0..1 horizontal fraction

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() != Qt.LeftButton or self.width() <= 0:
            return
        frac = max(0.0, min(1.0, ev.position().x() / self.width()))
        self.seekRequested.emit(frac)

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if ev.buttons() & Qt.LeftButton and self.width() > 0:
            frac = max(0.0, min(1.0, ev.position().x() / self.width()))
            self.seekRequested.emit(frac)


class CheckoutTrack(QWidget):
    """
    Visualizes one Checkout. The owning controller (main window) sets the
    checkout via `set_checkout(co)` and feeds playback position via
    `set_cursor(seconds)` on every tick.
    """

    seekRequested = Signal(float)  # seconds into the clip

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checkout_id: str | None = None
        self._duration_s: float = 0.0
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
        self._wave = ClickableWaveform(self)
        self._wave.setMinimumHeight(110)
        self._wave.set_labels("CLIP", "— — —")
        self._wave.seekRequested.connect(self._on_seek_frac)
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
        if checkout is None:
            self._checkout_id = None
            self._duration_s = 0.0
            self._wave.set_data(None)
            self._wave.set_playhead(None)
            self._wave.set_labels("CLIP", "— — —")
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
        self._caption.setText(f"CLIP  {checkout.id}")
        self._meta.setText(
            f"{checkout.ram_bytes / 1024 / 1024:.1f} MB   "
            f"{_mmss(self._duration_s)}"
        )
        self._pos_label.setText(f"00:00 / {_mmss(self._duration_s)}")
        self._state_label.setText(f"[{checkout.state.upper()}]")

    def set_cursor(self, seconds: float) -> None:
        """Feed playback cursor position in seconds for the playhead."""
        if self._duration_s <= 0:
            return
        frac = max(0.0, min(1.0, seconds / self._duration_s))
        self._wave.set_playhead(frac)
        self._pos_label.setText(
            f"{_mmss(seconds)} / {_mmss(self._duration_s)}"
        )

    def current_checkout_id(self) -> str | None:
        return self._checkout_id

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_seek_frac(self, frac: float) -> None:
        if self._duration_s <= 0:
            return
        self.seekRequested.emit(frac * self._duration_s)


def _mmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = int(seconds - m * 60)
    return f"{m:02d}:{s:02d}"
