"""WaveformPanel — header labels + linear WaveformView + time readouts."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel

from flashback_sampler.app.theme import EREBUS, font_family
from flashback_sampler.app.widgets.waveform_view import WaveformView


class WaveformPanel(QWidget):
    def __init__(self, side: str = "buffer", parent=None):
        super().__init__(parent)
        self._side = side

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        # Header row
        header = QHBoxLayout()
        header.setSpacing(8)

        self.source_label = QLabel("SOURCE 1" if side == "buffer" else "CLIP")
        self.source_label.setProperty("role", "label")
        self.source_label.setStyleSheet(
            f"color: {EREBUS['cream']}; font-size: 8pt; letter-spacing: 1.5px;"
        )
        header.addWidget(self.source_label)

        self.duration_label = QLabel("3:00" if side == "buffer" else "")
        self.duration_label.setStyleSheet(
            f"color: {EREBUS['bone']}; font-size: 8pt;"
        )
        header.addWidget(self.duration_label)

        header.addStretch()

        self.title_label = QLabel("BUFFER" if side == "buffer" else "")
        self.title_label.setStyleSheet(
            f"color: {EREBUS['bone']}; font-size: 8pt; letter-spacing: 2px;"
        )
        header.addWidget(self.title_label)

        self.clip_id_label = QLabel("" if side == "buffer" else "XYZ123")
        self.clip_id_label.setStyleSheet(
            f"color: {EREBUS['cream']}; font-size: 8pt;"
        )
        header.addWidget(self.clip_id_label)

        layout.addLayout(header)

        # Waveform view
        self.waveform = WaveformView()
        self.waveform.setMinimumHeight(40)
        layout.addWidget(self.waveform, stretch=1)

        # Time readouts
        time_row = QHBoxLayout()
        self.time_left_label = QLabel("-07:31" if side == "buffer" else "00:00")
        self.time_left_label.setStyleSheet(
            f"color: {EREBUS['ash']}; font-size: 7pt;"
        )
        time_row.addWidget(self.time_left_label)
        time_row.addStretch()
        self.time_right_label = QLabel("" if side == "buffer" else "14:55")
        self.time_right_label.setStyleSheet(
            f"color: {EREBUS['ash']}; font-size: 7pt;"
        )
        time_row.addWidget(self.time_right_label)
        layout.addLayout(time_row)

    def set_source_name(self, name: str) -> None:
        self.source_label.setText(name)

    def set_duration_text(self, text: str) -> None:
        self.duration_label.setText(text)

    def set_clip_id(self, clip_id: str) -> None:
        self.clip_id_label.setText(clip_id)

    def set_times(self, left: str, right: str) -> None:
        self.time_left_label.setText(left)
        self.time_right_label.setText(right)
