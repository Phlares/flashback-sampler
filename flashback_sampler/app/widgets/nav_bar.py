"""NavBar — bottom strip with ARM ALL, source indicators, and config readouts."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton

from flashback_sampler.app.theme import EREBUS, font_family

TRACK_COLORS = [
    "#EDF9B8",  # Source 1 — yellow-green
    "#FABCBC",  # Source 2 — pink
    "#E3B9FF",  # Source 3 — lavender
]

STATUS_COLORS = {
    "armed": "#FF0000",
    "paused": "#FF9500",
    "inactive": "#B3ACAC",
}


class SourceIndicator(QWidget):
    clicked = Signal()

    def __init__(self, index: int, name: str, parent=None):
        super().__init__(parent)
        self._index = index
        self._name = name
        self._color = QColor(TRACK_COLORS[index % len(TRACK_COLORS)])
        self._status = "armed"
        self.setFixedHeight(16)
        self.setMinimumWidth(60)
        self.setCursor(Qt.PointingHandCursor)

    def set_status(self, status: str) -> None:
        self._status = status
        self.update()

    def mousePressEvent(self, ev):
        self.clicked.emit()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        indicator_size = 8
        p.setBrush(QColor(STATUS_COLORS.get(self._status, "#B3ACAC")))
        p.setPen(Qt.NoPen)
        y_center = h // 2
        p.drawRect(2, y_center - indicator_size // 2, indicator_size, indicator_size)
        p.setPen(QColor(self._color))
        fam = font_family("label").split(",")[0].strip().strip('"')
        font = p.font()
        font.setFamily(fam)
        font.setPointSize(7)
        p.setFont(font)
        p.drawText(indicator_size + 6, 0, w - indicator_size - 8, h, Qt.AlignVCenter, self._name)
        p.end()


class NavBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(27)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(2)

        self.arm_all_btn = QPushButton("ARM ALL")
        self.arm_all_btn.setFixedHeight(20)
        self.arm_all_btn.setStyleSheet(
            f"QPushButton {{ background: {EREBUS['rec']}; color: {EREBUS['cream']};"
            f" border: none; font-size: 7pt; padding: 0 6px; }}"
        )
        layout.addWidget(self.arm_all_btn)

        self._add_separator(layout)

        self.source_slots: list[SourceIndicator] = []
        default_sources = ["SOURCE 1", "SOURCE 2", "SOURCE 3"]
        for i, name in enumerate(default_sources):
            slot = SourceIndicator(i, name)
            self.source_slots.append(slot)
            layout.addWidget(slot)

        self.add_source_btn = QPushButton("ADD SOURCE+")
        self.add_source_btn.setFixedHeight(20)
        self.add_source_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {EREBUS['bone']};"
            f" border: 1px solid {EREBUS['ash']}; font-size: 7pt; padding: 0 4px; }}"
        )
        layout.addWidget(self.add_source_btn)

        self._add_separator(layout)
        layout.addStretch()
        self._add_separator(layout)

        self.clip_length_label = QLabel("3:00")
        self.clip_length_label.setStyleSheet(f"color: {EREBUS['bone']}; font-size: 8pt;")
        layout.addWidget(self.clip_length_label)

        self._add_separator(layout)

        self.buffer_length_label = QLabel("15:00")
        self.buffer_length_label.setStyleSheet(f"color: {EREBUS['bone']}; font-size: 8pt;")
        layout.addWidget(self.buffer_length_label)

        self._add_separator(layout)

        self.project_size_label = QLabel("~4.31 GB")
        self.project_size_label.setStyleSheet(f"color: {EREBUS['bone']}; font-size: 8pt;")
        layout.addWidget(self.project_size_label)

    def _add_separator(self, layout: QHBoxLayout) -> None:
        sep = QWidget()
        sep.setFixedWidth(1)
        sep.setFixedHeight(16)
        sep.setStyleSheet(f"background: {EREBUS['ash']};")
        layout.addWidget(sep)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(EREBUS["void"]))
        p.end()
