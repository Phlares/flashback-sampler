"""
MainWindow — the 960×520 landscape Erebus chassis.

M4 scope (this file):
- Window geometry + chassis background
- Start / Stop Capture button wired to LoopbackCapture
- Live RMS readout (numeric, updated at 30 Hz via QTimer)
- Check Out button wired to CheckoutManager
- Active-checkout list with Save and Discard

M5–M8 scope (not here yet):
- Custom-painted WaveformView (recessed screen)
- RotaryKnob with hub-mounted time readout
- TactileButton with thermal tell bar
- VU LevelMeter with thermal segments
- Two-track layout (State A / State B)
- Monaspace font loading
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from flashback_sampler.app.state import AppState, make_loopback_capture


# Duration presets for the checkout stepper. Keep in sync with the M6
# 8-preset cluster from the frontend-design spec.
DURATION_PRESETS_S: tuple[float, ...] = (
    15.0, 30.0, 60.0, 120.0, 180.0, 300.0, 600.0, 900.0,
)
DEFAULT_DURATION_INDEX = 4  # 180 s = 3:00


class MainWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()
        self._state = state
        self._start_time: float = 0.0
        self._duration_idx = DEFAULT_DURATION_INDEX
        self._previewing_id: str | None = None

        self.setWindowTitle("flashback-sampler")
        self.setMinimumSize(720, 460)
        self.resize(960, 560)

        self._build_ui()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(33)  # ~30 Hz
        self._refresh_timer.timeout.connect(self._tick)
        self._refresh_timer.start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)

        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(24, 24, 24, 12)
        vbox.setSpacing(18)

        # --- Title strip -----------------------------------------------
        title = QLabel("FLASHBACK")
        title.setProperty("role", "readout")
        vbox.addWidget(title)

        # --- Live status row -------------------------------------------
        live_label = QLabel("LIVE BUFFER")
        live_label.setProperty("role", "label")
        vbox.addWidget(live_label)

        self._rms_label = QLabel("L  —   R  —")
        self._rms_label.setProperty("role", "readout")
        vbox.addWidget(self._rms_label)

        self._buffered_label = QLabel("00:00 / 15:00   fill 0%")
        self._buffered_label.setProperty("role", "label")
        vbox.addWidget(self._buffered_label)

        # --- Transport row ---------------------------------------------
        transport_row = QHBoxLayout()
        transport_row.setSpacing(12)

        self._capture_btn = QPushButton("START CAPTURE")
        self._capture_btn.setProperty("variant", "primary")
        self._capture_btn.clicked.connect(self._toggle_capture)
        transport_row.addWidget(self._capture_btn)

        self._checkout_btn = QPushButton(
            f"CHECK OUT {_mmss(DURATION_PRESETS_S[DEFAULT_DURATION_INDEX])}"
        )
        self._checkout_btn.setProperty("variant", "primary")
        self._checkout_btn.clicked.connect(self._create_checkout)
        self._checkout_btn.setEnabled(False)
        transport_row.addWidget(self._checkout_btn)

        # Minimal duration stepper — full 8-preset cluster lands in M6
        self._dur_down_btn = QPushButton("–")
        self._dur_down_btn.setFixedWidth(36)
        self._dur_down_btn.clicked.connect(lambda: self._step_duration(-1))
        transport_row.addWidget(self._dur_down_btn)

        self._dur_up_btn = QPushButton("+")
        self._dur_up_btn.setFixedWidth(36)
        self._dur_up_btn.clicked.connect(lambda: self._step_duration(+1))
        transport_row.addWidget(self._dur_up_btn)

        transport_row.addStretch(1)
        vbox.addLayout(transport_row)

        # --- Checkout list ---------------------------------------------
        list_label = QLabel("CHECKED-OUT CLIPS")
        list_label.setProperty("role", "label")
        vbox.addWidget(list_label)

        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._on_selection_changed)
        vbox.addWidget(self._list, 1)

        action_row = QHBoxLayout()
        action_row.setSpacing(12)

        self._preview_btn = QPushButton("▶  PREVIEW")
        self._preview_btn.clicked.connect(self._toggle_preview)
        self._preview_btn.setEnabled(False)
        action_row.addWidget(self._preview_btn)

        self._save_btn = QPushButton("SAVE")
        self._save_btn.setProperty("variant", "primary")
        self._save_btn.clicked.connect(self._save_selected)
        self._save_btn.setEnabled(False)
        action_row.addWidget(self._save_btn)

        self._discard_btn = QPushButton("DISCARD")
        self._discard_btn.clicked.connect(self._discard_selected)
        self._discard_btn.setEnabled(False)
        action_row.addWidget(self._discard_btn)

        action_row.addStretch(1)
        vbox.addLayout(action_row)

        # --- Status bar ------------------------------------------------
        sb = self.statusBar()
        self._device_label = QLabel("DEV  (not capturing)")
        sb.addWidget(self._device_label)
        self._sr_label = QLabel(f"SR  {self._state.sample_rate} HZ")
        sb.addPermanentWidget(self._sr_label)
        self._xrun_label = QLabel("XR  00")
        sb.addPermanentWidget(self._xrun_label)

    # ------------------------------------------------------------------
    # Tick — pulls status from the core at ~30 Hz
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        buf = self._state.buffer
        rms = buf.get_rms_levels(window_seconds=0.1)
        if rms.size >= 2:
            self._rms_label.setText(
                f"L  {_db(rms[0]):>6}    R  {_db(rms[1]):>6}"
            )
        elif rms.size == 1:
            self._rms_label.setText(f"L  {_db(rms[0]):>6}")

        bs = buf.buffered_seconds
        cap = buf.duration
        pct = 100.0 * bs / cap if cap else 0.0
        self._buffered_label.setText(
            f"{_mmss(bs)} / {_mmss(cap)}   fill {pct:5.1f}%"
        )

        # Checkout is allowed whenever the buffer has anything in it,
        # regardless of whether capture is currently running — user can
        # stop capture and still pull a clip from what they recorded.
        self._checkout_btn.setEnabled(bs > 0.5)

        if self._state.is_capturing():
            cap_src = self._state.capture
            xruns = cap_src.xrun_count() if hasattr(cap_src, "xrun_count") else 0
            self._xrun_label.setText(f"XR  {xruns:02d}")

        # Auto-flip the preview button back when playback drains naturally
        if self._previewing_id is not None and not self._state.scrub_player.is_playing:
            self._previewing_id = None
            self._preview_btn.setText("▶  PREVIEW")

    # ------------------------------------------------------------------
    # Capture control
    # ------------------------------------------------------------------

    def _toggle_capture(self) -> None:
        if self._state.is_capturing():
            self._state.capture.stop()
            self._capture_btn.setText("START CAPTURE")
            self._device_label.setText("DEV  (stopped)")
            # NOTE: do NOT disable the checkout button — buffered audio
            # from before the stop is still valid to check out. The tick
            # loop will re-enable it on the next pass based on buffered
            # seconds.
            return

        try:
            cap = make_loopback_capture(self._state)
            cap.start()
            self._state.set_capture(cap)
        except Exception as e:  # pragma: no cover — hardware path
            QMessageBox.critical(
                self,
                "Capture failed",
                f"Could not start loopback capture:\n\n{e}",
            )
            return

        self._start_time = time.monotonic()
        self._capture_btn.setText("STOP CAPTURE")
        self._device_label.setText("DEV  LOOPBACK (DEFAULT SPEAKER)")

    # ------------------------------------------------------------------
    # Duration stepper
    # ------------------------------------------------------------------

    def _step_duration(self, delta: int) -> None:
        new_idx = max(0, min(len(DURATION_PRESETS_S) - 1, self._duration_idx + delta))
        if new_idx == self._duration_idx:
            return
        self._duration_idx = new_idx
        self._checkout_btn.setText(
            f"CHECK OUT {_mmss(DURATION_PRESETS_S[self._duration_idx])}"
        )

    def _current_duration_s(self) -> float:
        return DURATION_PRESETS_S[self._duration_idx]

    # ------------------------------------------------------------------
    # Checkout control
    # ------------------------------------------------------------------

    def _create_checkout(self) -> None:
        try:
            co = self._state.checkout_manager.create(
                duration_s=self._current_duration_s()
            )
        except Exception as e:
            QMessageBox.warning(self, "Checkout failed", str(e))
            return
        self._refresh_checkout_list()
        # Auto-select the new one
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.UserRole) == co.id:
                self._list.setCurrentRow(i)
                break

    def _refresh_checkout_list(self) -> None:
        prev_selected_id = None
        cur = self._list.currentItem()
        if cur is not None:
            prev_selected_id = cur.data(Qt.UserRole)

        self._list.clear()
        for co in self._state.checkout_manager.list():
            mins = int(co.duration_seconds // 60)
            secs = int(co.duration_seconds - mins * 60)
            label = (
                f"  {co.id}   {mins:02d}:{secs:02d}"
                f"   {co.ram_bytes / 1024 / 1024:5.1f} MB"
                f"   [{co.state}]"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, co.id)
            self._list.addItem(item)
            if prev_selected_id == co.id:
                self._list.setCurrentItem(item)

    def _selected_checkout_id(self) -> str | None:
        item = self._list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _on_selection_changed(self) -> None:
        sel = self._selected_checkout_id()
        has = sel is not None
        self._save_btn.setEnabled(has)
        self._discard_btn.setEnabled(has)
        self._preview_btn.setEnabled(has)
        # If the user switches selection mid-preview, stop the old playback
        if self._previewing_id is not None and sel != self._previewing_id:
            self._stop_preview()

    # ------------------------------------------------------------------
    # Preview — wire ScrubPlayer to the selected checkout
    # ------------------------------------------------------------------

    def _toggle_preview(self) -> None:
        if self._previewing_id is not None:
            self._stop_preview()
            return

        cid = self._selected_checkout_id()
        if cid is None:
            return
        try:
            co = self._state.checkout_manager.get(cid)
        except KeyError:
            return

        player = self._state.scrub_player
        try:
            player.bind(co.audio)
            player.open()  # lazy — first call creates the output stream
            player.play()
        except Exception as e:
            QMessageBox.warning(
                self,
                "Preview failed",
                f"Could not start preview playback:\n\n{e}",
            )
            return

        self._previewing_id = cid
        self._preview_btn.setText("■  STOP PREVIEW")

    def _stop_preview(self) -> None:
        try:
            self._state.scrub_player.pause()
        except Exception:  # pragma: no cover
            pass
        self._previewing_id = None
        self._preview_btn.setText("▶  PREVIEW")

    def _save_selected(self) -> None:
        cid = self._selected_checkout_id()
        if cid is None:
            return
        target, selected = QFileDialog.getSaveFileName(
            self,
            "Save checkout",
            str(Path.home() / "Documents" / f"flashback_{cid}.wav"),
            "WAV audio (*.wav);;FLAC audio (*.flac)",
        )
        if not target:
            return
        fmt = "FLAC" if selected.startswith("FLAC") or target.lower().endswith(
            ".flac"
        ) else "WAV"
        try:
            self._state.checkout_manager.save(cid, Path(target), fmt=fmt)
        except Exception as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self._refresh_checkout_list()

    def _discard_selected(self) -> None:
        cid = self._selected_checkout_id()
        if cid is None:
            return
        if self._previewing_id == cid:
            self._stop_preview()
        try:
            self._state.checkout_manager.discard(cid)
        except Exception as e:
            QMessageBox.warning(self, "Discard failed", str(e))
            return
        self._refresh_checkout_list()

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802
        self._refresh_timer.stop()
        self._state.shutdown()
        super().closeEvent(event)


def _db(rms: float) -> str:
    """Format an RMS float as a small dBFS string, or '—' if silent."""
    if rms <= 1e-6:
        return "  —  "
    import math

    db = 20.0 * math.log10(max(rms, 1e-6))
    return f"{db:+5.1f}"


def _mmss(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = int(seconds - m * 60)
    return f"{m:02d}:{s:02d}"
