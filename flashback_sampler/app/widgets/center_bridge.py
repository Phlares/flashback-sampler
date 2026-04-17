"""CenterBridge — narrow vertical column between the two turntables."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout

from flashback_sampler.app.widgets.tactile_button import TactileButton


class CenterBridge(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaximumWidth(70)
        self.setMinimumWidth(50)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)
        layout.addStretch()

        self.stop_btn = TactileButton("STOP", variant="secondary")
        self.stop_btn.setMinimumWidth(50)
        self.stop_btn.setMinimumHeight(36)
        layout.addWidget(self.stop_btn)

        self.start_btn = TactileButton("START", variant="primary")
        self.start_btn.setMinimumWidth(50)
        self.start_btn.setMinimumHeight(36)
        layout.addWidget(self.start_btn)

        layout.addStretch()
