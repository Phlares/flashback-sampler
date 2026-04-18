"""NavBar — bottom strip with ARM ALL, source indicators, and config readouts."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
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
    contextMenuRequested = Signal(QPoint)

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

    def set_name(self, name: str) -> None:
        self._name = name
        self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        elif ev.button() == Qt.RightButton:
            self.contextMenuRequested.emit(ev.globalPosition().toPoint())

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
    chipClicked = Signal(int)
    chipContextMenuRequested = Signal(int, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(27)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(2)
        self._layout = layout

        self.arm_all_btn = QPushButton("ARM ALL")
        self.arm_all_btn.setFixedHeight(20)
        self.arm_all_btn.setStyleSheet(
            f"QPushButton {{ background: {EREBUS['rec']}; color: {EREBUS['cream']};"
            f" border: none; font-size: 7pt; padding: 0 6px; }}"
        )
        layout.addWidget(self.arm_all_btn)

        self._add_separator(layout)

        # Chips are created dynamically by set_source_names; 3 are
        # pre-created so existing tests and the initial paint behave
        # the same. ADD SOURCE+ sits immediately after the last chip
        # and new chips are inserted at its layout index so the button
        # always stays to the right of all visible sources.
        self.source_slots: list[SourceIndicator] = []
        for _ in range(3):
            self._create_chip()

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

    def set_source_names(self, names: list[str]) -> None:
        """Update chip labels and visibility, creating more chips as
        needed to match the slot count. Chips beyond the current slot
        count are hidden so SOURCE 2 / SOURCE 3 / … only appear after
        the user adds them via ADD SOURCE+."""
        while len(self.source_slots) < len(names):
            self._create_chip()
        for i, chip in enumerate(self.source_slots):
            if i < len(names):
                chip.set_name(names[i].upper())  # NavBar chips use uppercase
                chip.setVisible(True)
            else:
                chip.setVisible(False)

    def _create_chip(self) -> SourceIndicator:
        """Create and wire one more source chip, inserted into the
        layout immediately before ADD SOURCE+ (or at the end if the
        button hasn't been built yet)."""
        i = len(self.source_slots)
        chip = SourceIndicator(i, f"SOURCE {i + 1}")
        # Forward the per-chip signals through the NavBar so callers
        # wire up once, regardless of how many chips appear later.
        chip.clicked.connect(
            lambda _=None, idx=i: self.chipClicked.emit(idx)
        )
        chip.contextMenuRequested.connect(
            lambda pos, idx=i: self.chipContextMenuRequested.emit(idx, pos)
        )
        self.source_slots.append(chip)
        btn = getattr(self, "add_source_btn", None)
        if btn is not None:
            self._layout.insertWidget(self._layout.indexOf(btn), chip)
        else:
            self._layout.addWidget(chip)
        return chip

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
