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

from flashback_sampler.app.theme import EREBUS
from flashback_sampler.app.widgets.center_bridge import CenterBridge
from flashback_sampler.app.widgets.nav_bar import NavBar
from flashback_sampler.app.widgets.tactile_button import TactileButton
from flashback_sampler.app.widgets.turntable_widget import TurntableWidget
from flashback_sampler.app.widgets.waveform_panel import WaveformPanel


class TurntableWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
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

        self.buffer_controls: list[TactileButton] = []
        for label in ["FLUSH", "−", "+", "◀", "▶", "PAUSE"]:
            btn = TactileButton(label, variant="secondary")
            btn.setMinimumWidth(40)
            btn.setMinimumHeight(36)
            self.buffer_controls.append(btn)
            controls_row.addWidget(btn)

        controls_row.addStretch()

        self.loop_btn = TactileButton("LOOP", variant="primary")
        self.loop_btn.setCheckable(True)
        self.loop_btn.setFixedWidth(56)          # matches OUT→ for column alignment
        self.loop_btn.setMinimumHeight(36)
        controls_row.addWidget(self.loop_btn)

        controls_row.addStretch()

        self.clip_controls: list[TactileButton] = []
        for label in ["PLAY", "−", "+", "◀", "▶", "SAVE"]:
            btn = TactileButton(label, variant="secondary")
            btn.setMinimumWidth(40)
            btn.setMinimumHeight(36)
            self.clip_controls.append(btn)
            controls_row.addWidget(btn)

        root.addLayout(controls_row, stretch=1)

        # ── Row 4: Nav Bar ───────────────────────────────────────────
        self.nav_bar = NavBar()
        root.addWidget(self.nav_bar)

        self._populate_demo_data()

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
