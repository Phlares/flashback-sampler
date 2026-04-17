"""TurntableWindow — dual-turntable wireframe layout.

Parallel to MainWindow. Launch with --ui turntable.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

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
        # Per-source chips in the NavBar — clicking toggles armed state
        for i, slot_chip in enumerate(self.nav_bar.source_slots):
            slot_chip.clicked.connect(lambda _=None, idx=i: self._on_source_chip_clicked(idx))
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
        dlg = AddSourceDialog(
            default_name=default_name,
            default_buffer_seconds=default_buffer_s,
            max_buffer_seconds=default_buffer_s,
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
