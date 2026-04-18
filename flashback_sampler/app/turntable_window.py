"""TurntableWindow — dual-turntable wireframe layout.

Parallel to MainWindow. Launch with --ui turntable.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from flashback_sampler.app.audio_devices import CaptureDevice, list_capture_devices
from flashback_sampler.app.time_format import format_time_signed_cs
from flashback_sampler.app.process_picker_dialog import ProcessPickerDialog
from flashback_sampler.app.state import AppState
from flashback_sampler.app.theme import EREBUS

SELECTION_COLOR_BUFFER = "#FFD900"   # yellow
SELECTION_COLOR_CLIP = "#FF9500"     # orange
from flashback_sampler.app.widgets.center_bridge import CenterBridge
from flashback_sampler.app.widgets.nav_bar import NavBar
from flashback_sampler.app.widgets.tactile_button import TactileButton
from flashback_sampler.app.widgets.turntable_widget import TurntableWidget
from flashback_sampler.app.widgets.waveform_panel import WaveformPanel


def _peak_bins_from_audio(audio: np.ndarray, n_bins: int) -> np.ndarray:
    """Same shape as AudioCircularBuffer.get_peak_bins but operating on
    a static (N, channels) array — used to render a checkout's fixed audio."""
    if audio.ndim == 1:
        audio = audio[:, np.newaxis]
    n = int(audio.shape[0])
    channels = int(audio.shape[1])
    out = np.zeros((n_bins, 2, channels), dtype=np.float32)
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


class TurntableWindow(QMainWindow):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self._state = state
        self.setWindowTitle("Flashback — Turntable UI")
        self.setMinimumSize(960, 700)
        self.resize(1120, 800)

        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet(f"background-color: {EREBUS['chassis']};")

        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 0)
        root.setSpacing(4)

        # ── Row 1: Turntables + Center Bridge ────────────────────────
        turntable_row = QHBoxLayout()
        turntable_row.setSpacing(0)

        self.buffer_turntable = TurntableWidget(side="buffer")
        self.buffer_turntable.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        turntable_row.addWidget(self.buffer_turntable, stretch=1)

        self.center_bridge = CenterBridge()
        turntable_row.addWidget(self.center_bridge)

        self.clip_turntable = TurntableWidget(side="clip")
        self.clip_turntable.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        turntable_row.addWidget(self.clip_turntable, stretch=1)

        root.addLayout(turntable_row, stretch=6)

        # ── Row 2: Waveform Panels + OUT Button ──────────────────────
        waveform_row = QHBoxLayout()
        waveform_row.setSpacing(4)

        self.buffer_panel = WaveformPanel(side="buffer")
        waveform_row.addWidget(self.buffer_panel, stretch=1)

        self.out_btn = TactileButton("OUT →", variant="primary")
        self.out_btn.setFixedWidth(56)
        self.out_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        # Align OUT→ vertically with the container, not the full panel height.
        # Top spacing ≈ header row height (8pt label + panel top margin + spacing).
        out_col = QVBoxLayout()
        out_col.setContentsMargins(0, 0, 0, 0)
        out_col.setSpacing(0)
        out_col.addSpacing(20)
        out_col.addWidget(self.out_btn, stretch=1)
        waveform_row.addLayout(out_col)

        self.clip_panel = WaveformPanel(side="clip")
        waveform_row.addWidget(self.clip_panel, stretch=1)

        root.addLayout(waveform_row, stretch=2)

        # ── Row 3: Controls ──────────────────────────────────────────
        controls_row = QHBoxLayout()
        controls_row.setSpacing(4)

        # Left column: buffer controls packed tight, then stretch to push right
        buffer_col = QHBoxLayout()
        buffer_col.setSpacing(4)
        self.buffer_controls: list[TactileButton] = []
        for label in ["FLUSH", "−", "+", "◀", "▶", "PAUSE"]:
            btn = TactileButton(label, variant="secondary")
            btn.setMinimumWidth(40); btn.setMinimumHeight(36)
            self.buffer_controls.append(btn)
            buffer_col.addWidget(btn)
        buffer_col.addStretch()
        controls_row.addLayout(buffer_col, stretch=1)

        # Center column: LOOP, exact column width, no flanking stretches in THIS layout
        self.loop_btn = TactileButton("LOOP", variant="primary")
        self.loop_btn.setCheckable(True)
        self.loop_btn.setFixedWidth(56)
        self.loop_btn.setMinimumHeight(36)
        controls_row.addWidget(self.loop_btn)

        # Right column: stretch first, then clip controls
        clip_col = QHBoxLayout()
        clip_col.setSpacing(4)
        clip_col.addStretch()
        self.clip_controls: list[TactileButton] = []
        for label in ["PLAY", "−", "+", "◀", "▶", "SAVE"]:
            btn = TactileButton(label, variant="secondary")
            btn.setMinimumWidth(40); btn.setMinimumHeight(36)
            self.clip_controls.append(btn)
            clip_col.addWidget(btn)
        controls_row.addLayout(clip_col, stretch=1)

        root.addLayout(controls_row, stretch=1)

        # ── Row 4: Nav Bar ───────────────────────────────────────────
        self.nav_bar = NavBar()
        root.addWidget(self.nav_bar)

        # Buffer turntable has one ring per slot (at least 1). Clip
        # turntable rings correspond to checkouts and are set in
        # _refresh_clip_side() below.
        n = max(len(state.slots), 1)
        self.buffer_turntable.set_track_count(n)

        # Selection state for drift-with-audio.
        # abs_samples: (start, end) in total_written-space. Stays fixed; we
        # compute current fractions each tick based on the buffer's current
        # write position so the selection keeps pointing at the same audio
        # content as the buffer advances.
        # mode: "default" (anchored to "now" — re-computed every tick from
        # slot.duration_preset_idx/anchor_offset_s) or "user" (fixed abs
        # samples captured when the user drags).
        self._buffer_sel_abs: tuple[int, int] | None = None
        self._buffer_sel_mode: str = "default"  # "default" | "user"
        self._clip_sel_abs: tuple[int, int] | None = None
        self._clip_sel_mode: str = "default"

        self._wire_selection_sync()
        self._wire_controls()

        # Live audio polling @ ~30 Hz
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(33)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        self._refresh_source_names()
        # Paint the initial default selection immediately rather than waiting
        # for the first tick (33ms).
        self._update_selection_display()
        # Paint the initial (empty) clip side so its labels/rings are consistent.
        self._refresh_clip_side()

        # Lazy-create status bar for surfacing non-modal messages.
        self.statusBar().showMessage("Ready", 0)

    def _wire_selection_sync(self) -> None:
        """When the user drags a selection on a waveform, snapshot the
        buffer's absolute sample indices so the selection rides the audio.
        Each tick, _update_selection_display converts those absolute
        positions back into display fractions for both the linear panel
        and the disc ring."""
        def on_buffer_sel(start: float, end: float) -> None:
            if not self._state.slots:
                return
            slot = self._state.active_slot
            buf = slot.buffer
            buffered_s = float(buf.buffered_seconds)
            if buffered_s <= 0 or end <= start:
                return
            total = int(buf.total_written)
            sr = int(buf.sample_rate)
            samples_visible = int(buffered_s * sr)
            oldest_visible = total - samples_visible
            abs_start = oldest_visible + int(start * samples_visible)
            abs_end = oldest_visible + int(end * samples_visible)
            self._buffer_sel_abs = (abs_start, abs_end)
            self._buffer_sel_mode = "user"

        def on_buffer_clear() -> None:
            self._buffer_sel_abs = None
            self._buffer_sel_mode = "default"

        def on_clip_sel(start: float, end: float) -> None:
            if not self._state.slots:
                return
            slot = self._state.active_slot
            buf = slot.buffer
            buffered_s = float(buf.buffered_seconds)
            if buffered_s <= 0 or end <= start:
                return
            total = int(buf.total_written)
            sr = int(buf.sample_rate)
            samples_visible = int(buffered_s * sr)
            oldest_visible = total - samples_visible
            abs_start = oldest_visible + int(start * samples_visible)
            abs_end = oldest_visible + int(end * samples_visible)
            self._clip_sel_abs = (abs_start, abs_end)
            self._clip_sel_mode = "user"

        def on_clip_clear() -> None:
            self._clip_sel_abs = None
            self._clip_sel_mode = "default"

        self.buffer_panel.waveform.manualSelectionChanged.connect(on_buffer_sel)
        self.buffer_panel.waveform.manualSelectionCleared.connect(on_buffer_clear)
        self.clip_panel.waveform.manualSelectionChanged.connect(on_clip_sel)
        self.clip_panel.waveform.manualSelectionCleared.connect(on_clip_clear)

    def _wire_controls(self) -> None:
        # Transport
        self.center_bridge.start_btn.clicked.connect(self._on_start_clicked)
        self.center_bridge.stop_btn.clicked.connect(self._on_stop_clicked)
        # PAUSE is per-side (in buffer_controls[-1]) — maps to same stop_rolling for now
        pause_btn = self.buffer_controls[-1]   # "PAUSE" is index 5
        pause_btn.clicked.connect(self._on_stop_clicked)

        # Track selection on either turntable → update active slot
        self.buffer_turntable.track_selected.connect(self._on_track_selected)
        self.clip_turntable.track_selected.connect(self._on_track_selected)

        # OUT → check out current buffer selection as a new clip
        self.out_btn.clicked.connect(self._on_checkout_clicked)

        # NavBar actions
        self.nav_bar.arm_all_btn.clicked.connect(self._on_arm_all)
        self.nav_bar.add_source_btn.clicked.connect(self._on_add_source)
        # Per-source chips in the NavBar — left-click toggles armed state,
        # right-click opens the per-source context menu.
        for i, slot_chip in enumerate(self.nav_bar.source_slots):
            slot_chip.clicked.connect(lambda _=None, idx=i: self._on_source_chip_clicked(idx))
            slot_chip.contextMenuRequested.connect(
                lambda pos, idx=i: self._on_source_chip_context_menu(idx, pos)
            )
        self._refresh_source_indicators()

    def _on_start_clicked(self) -> None:
        started, err = self._state.start_rolling()
        if err is not None:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Start capture failed", str(err))
            return
        self._refresh_source_indicators()

    def _on_stop_clicked(self) -> None:
        self._state.stop_rolling()
        self._refresh_source_indicators()

    def _on_track_selected(self, index: int) -> None:
        sender = self.sender()
        if sender is self.clip_turntable:
            # Clip click: just show that clip in the panel, don't change slot
            if not self._state.slots:
                return
            checkouts = list(self._state.active_slot.checkout_manager.list())
            if 0 <= index < len(checkouts):
                self._display_clip_in_panel(checkouts[index], index, len(checkouts))
            return
        # Buffer click: change the active slot
        try:
            self._state.set_active_slot_index(index)
        except IndexError:
            return  # clicked a track beyond current slot count — ignore
        # Mirror the selection index on the clip side if it has that many rings
        if index < self.clip_turntable.track_count():
            self.clip_turntable.select_track(index)
        self._refresh_source_names()
        self._refresh_source_indicators()
        # Reset selection state for the new active slot so it shows its own
        # default; abs-sample snapshots belong to the prior slot.
        self._buffer_sel_mode = "default"
        self._buffer_sel_abs = None
        self._clip_sel_mode = "default"
        self._clip_sel_abs = None
        self._update_selection_display()
        # Refresh clip side to show the new slot's checkouts
        self._refresh_clip_side()

    def _on_checkout_clicked(self) -> None:
        if not self._state.slots:
            return
        slot = self._state.active_slot
        buf = slot.buffer
        total = int(buf.total_written)
        sr = int(buf.sample_rate)

        # Determine abs range from current selection mode
        if self._buffer_sel_mode == "user" and self._buffer_sel_abs is not None:
            abs_start, abs_end = self._buffer_sel_abs
        else:
            # Default mode — use slot.duration_preset_idx + anchor_offset_s from "now"
            from flashback_sampler.app.widgets.duration_preset import DEFAULT_PRESETS
            preset_idx = max(
                0, min(len(DEFAULT_PRESETS) - 1, slot.duration_preset_idx)
            )
            duration_s = DEFAULT_PRESETS[preset_idx]
            anchor_s = max(0.0, slot.anchor_offset_s)
            abs_end = total - int(anchor_s * sr)
            abs_start = abs_end - int(duration_s * sr)

        # Clamp abs_start to what's still in the ring
        oldest_available = max(0, total - buf.buffer_size)
        abs_start = max(abs_start, oldest_available)
        if abs_end <= abs_start:
            QMessageBox.warning(
                self, "Check out failed",
                "Selection is empty or has already scrolled out of the buffer."
            )
            return

        try:
            slot.checkout_manager.create_from_abs_range(abs_start, abs_end)
        except Exception as e:
            QMessageBox.warning(self, "Check out failed", str(e))
            return

        # Clear buffer selection → return to default behavior
        self._buffer_sel_mode = "default"
        self._buffer_sel_abs = None

        # Refresh clip side to include the new checkout; auto-select it
        self._refresh_clip_side(auto_select_newest=True)

    def _refresh_clip_side(self, auto_select_newest: bool = False) -> None:
        """Populate the clip_turntable rings and clip panel with checkouts
        from the ACTIVE slot. Newest checkout = outermost ring (highest index)."""
        if not self._state.slots:
            return
        slot = self._state.active_slot
        checkouts = list(slot.checkout_manager.list())  # oldest first
        n = len(checkouts)

        # Resize clip turntable. Keep a minimum of 1 ring for visual
        # consistency when there are no checkouts yet.
        self.clip_turntable.set_track_count(max(n, 1))

        # Clear out any track waveforms / selections / statuses that no
        # longer correspond to a checkout.
        self.clip_turntable._track_waveforms.clear()
        self.clip_turntable._track_selections.clear()

        if n == 0:
            # Nothing to render; force a repaint so cleared rings show.
            self.clip_turntable.update()
            self.clip_panel.waveform.set_data(
                np.zeros((1, 2, 1), dtype=np.float32)
            )
            self.clip_panel.set_source_name("CLIP")
            self.clip_panel.set_duration_text("")
            self.clip_panel.set_clip_id("")
            self.clip_panel.set_times("0:00.00", "0:00.00")
            return

        # Plot each checkout onto its ring.
        for j, co in enumerate(checkouts):
            bins = _peak_bins_from_audio(co.audio, n_bins=540)
            amp = (
                (bins[:, 1, :].max(axis=1) - bins[:, 0, :].min(axis=1)) / 2.0
            ).astype(np.float32)
            amp = np.clip(amp, 0.0, 1.0)
            self.clip_turntable.set_track_waveform(j, amp, fill_fraction=1.0)

        # Decide which clip's waveform the clip PANEL displays
        if auto_select_newest:
            sel_idx = n - 1
        else:
            sel_idx = max(0, min(self.clip_turntable.selected_track(), n - 1))
        self.clip_turntable.select_track(sel_idx)
        self._display_clip_in_panel(checkouts[sel_idx], sel_idx, n)

    def _display_clip_in_panel(self, co, index: int, total: int) -> None:
        """Render a single checkout's full audio into the clip WaveformPanel."""
        bins = _peak_bins_from_audio(co.audio, n_bins=360)
        self.clip_panel.waveform.set_data(bins)
        slot_name = (
            self._state.active_slot.name if self._state.slots else "?"
        )
        self.clip_panel.set_source_name(
            f"{slot_name.upper()} #{index + 1}/{total}"
        )
        self.clip_panel.set_duration_text(f"{co.duration_seconds:.1f}s")
        self.clip_panel.set_clip_id(co.id[:6].upper())
        self.clip_panel.set_times("0:00.00", f"{co.duration_seconds:.2f}s")

    def _on_arm_all(self) -> None:
        for slot in self._state.slots:
            slot.armed = True
        self._refresh_source_indicators()

    def _on_source_chip_clicked(self, slot_idx: int) -> None:
        if not (0 <= slot_idx < len(self._state.slots)):
            return
        slot = self._state.slots[slot_idx]
        slot.armed = not slot.armed
        self._refresh_source_indicators()

    def _on_add_source(self) -> None:
        from flashback_sampler.app.add_source_dialog import AddSourceDialog
        from PySide6.QtWidgets import QMessageBox
        active = self._state.active_slot
        default_name = f"Source {len(self._state.slots) + 1}"
        default_buffer_s = active.buffer_seconds
        max_buffer_s = max(3600.0, default_buffer_s * 4)
        dlg = AddSourceDialog(
            default_name=default_name,
            default_buffer_seconds=default_buffer_s,
            max_buffer_seconds=max_buffer_s,
            default_sample_rate=active.sample_rate,
            default_channels=active.channels,
            parent=self,
        )
        if dlg.exec() != AddSourceDialog.Accepted:
            return
        preset = dlg.result_preset()
        if preset is None:
            return
        name = dlg.result_name() or default_name
        try:
            self._state.add_slot(preset, name=name)
        except Exception as e:
            QMessageBox.warning(self, "Add source failed", str(e))
            return
        # Refresh visuals to match new slot count. Clip turntable reflects
        # checkouts on the ACTIVE slot, not slot count, so re-run the clip
        # refresh rather than forcing track count to match slots.
        n = len(self._state.slots)
        self.buffer_turntable.set_track_count(n)
        self._refresh_source_indicators()
        self._refresh_source_names()
        self._refresh_clip_side()

    def _refresh_source_indicators(self) -> None:
        """Update the NavBar source chips to reflect current slot armed/capturing state."""
        for i, chip in enumerate(self.nav_bar.source_slots):
            if i >= len(self._state.slots):
                chip.set_status("inactive")
                continue
            slot = self._state.slots[i]
            if self._state.rolling and slot.armed:
                chip.set_status("armed")
            elif slot.armed:
                chip.set_status("paused")   # armed but not rolling yet
            else:
                chip.set_status("inactive")

    def _update_selection_display(self) -> None:
        """Convert stored absolute sample positions (or default anchor /
        duration when in "default" mode) into current display fractions
        and apply them to both the linear WaveformPanel and the matching
        disc ring. Called every tick so user selections ride the audio as
        the buffer advances."""
        from flashback_sampler.app.widgets.duration_preset import DEFAULT_PRESETS
        if not self._state.slots:
            return
        slot = self._state.active_slot
        buf = slot.buffer
        buffered_s = float(buf.buffered_seconds)
        if buffered_s <= 0:
            # Nothing to show yet
            return
        sr = int(buf.sample_rate)
        total = int(buf.total_written)

        def compute_fracs_buffer_side() -> tuple[float, float] | None:
            if self._buffer_sel_mode == "default":
                preset_idx = max(
                    0, min(len(DEFAULT_PRESETS) - 1, slot.duration_preset_idx)
                )
                duration_s = DEFAULT_PRESETS[preset_idx]
                anchor_s = max(0.0, slot.anchor_offset_s)
                end_ago = anchor_s
                start_ago = anchor_s + duration_s
            else:  # user
                if self._buffer_sel_abs is None:
                    return None
                abs_start, abs_end = self._buffer_sel_abs
                start_ago = (total - abs_start) / sr
                end_ago = (total - abs_end) / sr
            end_frac = 1.0 - end_ago / buffered_s
            start_frac = 1.0 - start_ago / buffered_s
            # Clamp to visible range
            end_frac = max(0.0, min(1.0, end_frac))
            start_frac = max(0.0, min(1.0, start_frac))
            if end_frac <= start_frac:
                return None
            return (start_frac, end_frac)

        def compute_fracs_clip_side() -> tuple[float, float] | None:
            if self._clip_sel_mode == "default":
                # No default for clip side — clip selection waits for user input
                return None
            if self._clip_sel_abs is None:
                return None
            abs_start, abs_end = self._clip_sel_abs
            start_ago = (total - abs_start) / sr
            end_ago = (total - abs_end) / sr
            end_frac = max(0.0, min(1.0, 1.0 - end_ago / buffered_s))
            start_frac = max(0.0, min(1.0, 1.0 - start_ago / buffered_s))
            if end_frac <= start_frac:
                return None
            return (start_frac, end_frac)

        def apply(fracs: tuple[float, float] | None, color: str,
                  panel, turntable) -> None:
            idx = self._state.active_slot_index
            if fracs is None:
                if not panel.waveform._is_dragging:
                    panel.waveform.blockSignals(True)
                    panel.waveform.clear_manual_selection()
                    panel.waveform.blockSignals(False)
                turntable.set_track_selection(idx, None, None, color)
                return
            s, e = fracs
            if not panel.waveform._is_dragging:
                panel.waveform.blockSignals(True)
                panel.waveform.set_manual_selection(s, e)
                panel.waveform.blockSignals(False)
            turntable.set_track_selection(idx, s, e, color)

        apply(compute_fracs_buffer_side(), SELECTION_COLOR_BUFFER,
              self.buffer_panel, self.buffer_turntable)
        apply(compute_fracs_clip_side(), SELECTION_COLOR_CLIP,
              self.clip_panel, self.clip_turntable)

    def _refresh_source_names(self) -> None:
        """Propagate slot names from state into NavBar chips and the active
        waveform panel's source label."""
        names = [slot.name for slot in self._state.slots]
        self.nav_bar.set_source_names(names)
        # Buffer panel shows the currently-active slot's name
        active_name = (
            self._state.active_slot.name if self._state.slots else "SOURCE 1"
        )
        self.buffer_panel.set_source_name(active_name.upper())
        # Clip panel's source label stays "CLIP" for now (clip-side names
        # belong to checkouts which come in a later phase)

    # ------------------------------------------------------------------
    # Live audio polling
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """Pull peak-bin data from each slot's buffer and push into UI.
        Active slot drives the buffer WaveformPanel; each slot's bins
        also go to its corresponding track ring as a radial plot."""
        slots = self._state.slots
        active_idx = self._state.active_slot_index

        # Active slot → buffer panel's linear waveform view + timestamps
        if 0 <= active_idx < len(slots):
            active_buf = slots[active_idx].buffer
            try:
                bins = active_buf.get_peak_bins(
                    seconds=active_buf.duration, n_bins=360
                )
                self.buffer_panel.waveform.set_data(bins)
            except Exception:
                pass  # capture may not be running yet
            # Update buffer panel time labels from real buffered_seconds
            try:
                buffered_s = float(active_buf.buffered_seconds)
                left = format_time_signed_cs(-buffered_s)
                right = "0:00.00"
                self.buffer_panel.set_times(left, right)
            except Exception:
                pass

        # Each slot → its ring on the buffer turntable as a radial plot
        for i, slot in enumerate(slots):
            if i >= self.buffer_turntable.track_count():
                break
            try:
                buffered_s = slot.buffered_seconds()
                capacity_s = slot.buffer.duration
                fill_frac = 0.0
                if capacity_s > 0:
                    fill_frac = max(0.0, min(1.0, buffered_s / capacity_s))
                if fill_frac < 1e-4:
                    # No data yet — clear the ring waveform
                    self.buffer_turntable.set_track_waveform(
                        i, np.zeros(0, dtype=np.float32), fill_fraction=0.0
                    )
                    continue
                n_bins = max(16, int(360 * fill_frac))
                bins = slot.buffer.get_peak_bins(seconds=buffered_s, n_bins=n_bins)
                # Peak amplitude per bin: half the peak-to-peak over channels
                amp = (
                    (bins[:, 1, :].max(axis=1) - bins[:, 0, :].min(axis=1)) / 2.0
                ).astype(np.float32)
                # Bins are already in [-1, 1] from float audio; peak-to-peak/2 is in [0, 1].
                # Clip defensively; no renormalization.
                amp = np.clip(amp, 0.0, 1.0)
                self.buffer_turntable.set_track_waveform(i, amp, fill_fraction=fill_frac)
            except Exception:
                pass

        # Recompute selection fractions from stored abs samples (or defaults)
        # so user selections drift with audio and the radial arc updates to
        # the current fill fraction.
        self._update_selection_display()

    def closeEvent(self, event) -> None:
        self._tick_timer.stop()
        self._state.shutdown()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Per-source context menu
    # ------------------------------------------------------------------

    def _switch_to_slot(self, slot_index: int) -> None:
        if not (0 <= slot_index < len(self._state.slots)):
            return
        try:
            self._state.set_active_slot_index(slot_index)
        except IndexError:
            return
        # Mirror on both turntables
        if slot_index < self.buffer_turntable.track_count():
            self.buffer_turntable.select_track(slot_index)
        if slot_index < self.clip_turntable.track_count():
            self.clip_turntable.select_track(slot_index)
        self._refresh_source_names()
        # Reset selection state for the new active slot.
        self._buffer_sel_mode = "default"
        self._buffer_sel_abs = None
        self._clip_sel_mode = "default"
        self._clip_sel_abs = None
        self._update_selection_display()
        self._refresh_clip_side()
        self._refresh_source_indicators()

    def _on_source_chip_context_menu(
        self, slot_index: int, global_pos: QPoint
    ) -> None:
        if not (0 <= slot_index < len(self._state.slots)):
            return
        slot = self._state.slots[slot_index]
        can_remove = len(self._state.slots) > 1
        has_buffered = slot.buffered_seconds() > 0.1

        menu = QMenu(self)

        switch_act = QAction(f"Switch to {slot.name}", self)
        switch_act.setEnabled(slot_index != self._state.active_slot_index)
        switch_act.triggered.connect(
            lambda _c=False, i=slot_index: self._switch_to_slot(i)
        )
        menu.addAction(switch_act)

        prime_label = "Stop Recording" if slot.is_capturing() else "Start Recording"
        prime_act = QAction(prime_label, self)
        prime_act.triggered.connect(
            lambda _c=False, i=slot_index: self._on_source_chip_clicked(i)
        )
        menu.addAction(prime_act)

        menu.addSeparator()

        # Capture Source submenu — per-slot device routing. "Use
        # Default (global)" at the top sets slot.capture_spec = None so
        # the slot follows whatever the Audio menu has selected.
        cap_menu = menu.addMenu("Capture Source")
        self._populate_slot_capture_source_menu(cap_menu, slot_index)

        menu.addSeparator()

        flush_act = QAction("Flush Buffer…", self)
        flush_act.setEnabled(has_buffered)
        flush_act.triggered.connect(
            lambda _c=False, i=slot_index: self._flush_slot_buffer(i)
        )
        menu.addAction(flush_act)

        menu.addSeparator()

        remove_act = QAction("Remove Source…", self)
        remove_act.setEnabled(can_remove)
        remove_act.triggered.connect(
            lambda _c=False, i=slot_index: self._remove_slot_with_confirmation(i)
        )
        menu.addAction(remove_act)

        qpt = QPoint(int(global_pos.x()), int(global_pos.y()))
        menu.exec(qpt)

    def _populate_slot_capture_source_menu(
        self, cap_menu: QMenu, slot_index: int
    ) -> None:
        """Build (or rebuild) the per-slot Capture Source submenu."""
        slot = self._state.slots[slot_index]
        current_spec = self._state.effective_capture_spec_for_slot(slot)
        using_override = slot.capture_spec is not None

        group = QActionGroup(cap_menu)
        group.setExclusive(True)

        global_default = QAction("Use Default (global)", cap_menu)
        global_default.setCheckable(True)
        global_default.setChecked(not using_override)
        global_default.triggered.connect(
            lambda _c=False, i=slot_index: self._set_slot_capture_spec(i, None)
        )
        group.addAction(global_default)
        cap_menu.addAction(global_default)

        cap_menu.addSeparator()

        # Capture from Process... — opens the Windows-only process
        # picker, returns a CaptureDevice with kind="process_loopback"
        proc_act = QAction("Capture from Process…", cap_menu)
        proc_act.triggered.connect(
            lambda _c=False, i=slot_index: self._pick_process_for_slot(i)
        )
        cap_menu.addAction(proc_act)

        cap_menu.addSeparator()

        devices = list_capture_devices()
        if not devices:
            hint = QAction("(no capture devices)", cap_menu)
            hint.setEnabled(False)
            cap_menu.addAction(hint)
            return

        for dev in devices:
            label = dev.name + ("   [default]" if dev.is_default else "")
            act = QAction(label, cap_menu)
            act.setCheckable(True)
            if (
                using_override
                and current_spec is not None
                and current_spec.kind == dev.kind
                and current_spec.id == dev.id
            ):
                act.setChecked(True)
            group.addAction(act)
            cap_menu.addAction(act)
            act.triggered.connect(
                lambda _c=False, d=dev, i=slot_index: self._set_slot_capture_spec(i, d)
            )

    def _pick_process_for_slot(self, slot_index: int) -> None:
        """Open the ProcessPickerDialog and, on accept, set the slot's
        capture_spec to a per-process CaptureDevice."""
        dlg = ProcessPickerDialog(parent=self)
        if dlg.exec() != ProcessPickerDialog.Accepted:
            return
        device = dlg.result_device()
        if device is None:
            return
        self._set_slot_capture_spec(slot_index, device)

    def _set_slot_capture_spec(
        self, slot_index: int, device: CaptureDevice | None
    ) -> None:
        """Set a slot's per-slot capture override (or clear it to follow
        the global default). If the slot is currently capturing, stop
        and restart with the new device so the change takes effect
        immediately."""
        if not (0 <= slot_index < len(self._state.slots)):
            return
        slot = self._state.slots[slot_index]
        slot.capture_spec = device

        if not slot.is_capturing():
            return

        # Restart on the new source
        try:
            slot.stop_capture()
            new_source = self._state.build_capture_for_slot(slot)
            slot.bind_capture(new_source)
            slot.start_capture()
        except Exception as e:
            QMessageBox.warning(
                self,
                "Capture restart failed",
                f"Could not switch capture source on "
                f"{slot.name!r}:\n\n{e}",
            )

    def _flush_slot_buffer(self, slot_index: int) -> None:
        if not (0 <= slot_index < len(self._state.slots)):
            return
        slot = self._state.slots[slot_index]
        buffered = slot.buffered_seconds()
        reply = QMessageBox.question(
            self,
            "Flush buffer?",
            f"Discard {buffered:.1f}s of buffered audio on {slot.name!r}?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        slot.buffer.flush()

    def _remove_slot_with_confirmation(self, slot_index: int) -> None:
        if not (0 <= slot_index < len(self._state.slots)):
            return
        slot = self._state.slots[slot_index]
        reply = QMessageBox.question(
            self,
            "Remove source?",
            (
                f"This will stop capture on {slot.name!r} and discard its "
                f"{slot.buffered_seconds():.1f} s of buffered audio.\n\n"
                "Existing checkouts on this slot will also be lost — "
                "they live in the slot's CheckoutManager."
            ),
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            self._state.remove_slot(slot_index)
        except Exception as e:
            QMessageBox.warning(self, "Remove failed", str(e))
            return
        # Adjust buffer turntable track count to match the new slot count;
        # the clip turntable now reflects checkouts via _refresh_clip_side.
        n = len(self._state.slots)
        self.buffer_turntable.set_track_count(max(n, 1))
        # Mirror selection on the (now-current) active slot
        active_idx = self._state.active_slot_index
        if 0 <= active_idx < self.buffer_turntable.track_count():
            self.buffer_turntable.select_track(active_idx)
        self._refresh_source_names()
        self._refresh_source_indicators()
        self._refresh_clip_side()

    def _populate_demo_data(self) -> None:
        rng = np.random.default_rng(seed=42)
        for tt in (self.buffer_turntable, self.clip_turntable):
            for i in range(tt.track_count()):
                n = 540
                t = np.linspace(0, 2 * np.pi, n, endpoint=False)
                amp = 0.4 * np.sin(t * (2 + i)) + 0.15 * rng.standard_normal(n)
                tt.set_track_waveform(i, amp.astype(np.float32))
        self.buffer_panel.set_demo_waveform()
        self.clip_panel.set_demo_waveform()
