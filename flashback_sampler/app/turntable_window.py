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

        # Set track counts to match current slot count (at least 1)
        n = max(len(state.slots), 1)
        self.buffer_turntable.set_track_count(n)
        self.clip_turntable.set_track_count(n)

        self._wire_selection_sync()
        self._wire_controls()

        # Live audio polling @ ~30 Hz
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(33)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        self._refresh_source_names()
        self._apply_default_buffer_selection()

        # Lazy-create status bar for surfacing non-modal messages.
        self.statusBar().showMessage("Ready", 0)

    def _wire_selection_sync(self) -> None:
        """When user drags a selection on a waveform, paint the matching arc
        on the currently-selected track of that side's record."""
        def on_buffer_sel(start: float, end: float) -> None:
            idx = self.buffer_turntable.selected_track()
            self.buffer_turntable.set_track_selection(idx, start, end, SELECTION_COLOR_BUFFER)

        def on_buffer_clear() -> None:
            idx = self.buffer_turntable.selected_track()
            self.buffer_turntable.set_track_selection(idx, None, None, SELECTION_COLOR_BUFFER)

        def on_clip_sel(start: float, end: float) -> None:
            idx = self.clip_turntable.selected_track()
            self.clip_turntable.set_track_selection(idx, start, end, SELECTION_COLOR_CLIP)

        def on_clip_clear() -> None:
            idx = self.clip_turntable.selected_track()
            self.clip_turntable.set_track_selection(idx, None, None, SELECTION_COLOR_CLIP)

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
        # Keep the other turntable's selection mirrored so both discs point at the same slot
        try:
            self._state.set_active_slot_index(index)
        except IndexError:
            return  # clicked a track beyond current slot count — ignore
        # Sync the OTHER turntable's visual selection
        sender = self.sender()
        other = self.clip_turntable if sender is self.buffer_turntable else self.buffer_turntable
        if other.selected_track() != index:
            other.select_track(index)
        self._refresh_source_names()
        self._apply_default_buffer_selection()

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
        # Refresh visuals to match new slot count
        n = len(self._state.slots)
        self.buffer_turntable.set_track_count(n)
        self.clip_turntable.set_track_count(n)
        self._refresh_source_indicators()
        self._refresh_source_names()

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

    def _apply_default_buffer_selection(self) -> None:
        """Paint the default checkout-range selection (3:00 back from now)
        on the buffer waveform + the active track's disc ring."""
        from flashback_sampler.app.widgets.duration_preset import DEFAULT_PRESETS
        if not self._state.slots:
            return
        slot = self._state.active_slot
        capacity_s = slot.buffer.duration
        if capacity_s <= 0:
            return
        preset_idx = max(0, min(len(DEFAULT_PRESETS) - 1, slot.duration_preset_idx))
        duration_s = DEFAULT_PRESETS[preset_idx]
        anchor_s = max(0.0, slot.anchor_offset_s)
        end_frac = max(0.0, min(1.0, 1.0 - anchor_s / capacity_s))
        start_frac = max(0.0, min(1.0, 1.0 - (anchor_s + duration_s) / capacity_s))
        if end_frac <= start_frac:
            return
        # Linear waveform (block the signal so it doesn't re-fire into set_track_selection)
        self.buffer_panel.waveform.blockSignals(True)
        self.buffer_panel.waveform.set_manual_selection(start_frac, end_frac)
        self.buffer_panel.waveform.blockSignals(False)
        # Disc selection arc
        idx = self._state.active_slot_index
        self.buffer_turntable.set_track_selection(
            idx, start_frac, end_frac, SELECTION_COLOR_BUFFER
        )

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
        self._apply_default_buffer_selection()
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
        # Adjust track counts on both turntables to match the new slot count
        n = len(self._state.slots)
        self.buffer_turntable.set_track_count(max(n, 1))
        self.clip_turntable.set_track_count(max(n, 1))
        # Mirror selection on the (now-current) active slot
        active_idx = self._state.active_slot_index
        if 0 <= active_idx < self.buffer_turntable.track_count():
            self.buffer_turntable.select_track(active_idx)
        if 0 <= active_idx < self.clip_turntable.track_count():
            self.clip_turntable.select_track(active_idx)
        self._refresh_source_names()
        self._refresh_source_indicators()

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
