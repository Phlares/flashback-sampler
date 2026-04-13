"""
SourceStrip — horizontal row of SlotChips plus the "+ Add Source"
button. Lives at the top of the main window above Track 1.

The host (main_window) calls `set_slots()` on every tick to push the
current slot state. The strip emits three signals:

- activeChanged(int): user clicked a chip to switch active slot
- addSourceRequested(): user clicked the + button
- contextMenuRequested(int, QPointF): user right-clicked a chip

No audio logic lives here — the host wires all of it.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from flashback_sampler.app.widgets.slot_chip import CHIP_HEIGHT, SlotChip
from flashback_sampler.app.widgets.tactile_button import TactileButton


class SourceStrip(QWidget):
    activeChanged = Signal(int)
    primeToggled = Signal(int)
    addSourceRequested = Signal()
    contextMenuRequested = Signal(int, QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chips: list[SlotChip] = []
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        label = QLabel("SOURCES")
        label.setProperty("role", "label")
        outer.addWidget(label, 0)

        # Scrollable horizontal area for the chips so many slots still
        # fit on a narrow window. QScrollArea around a container widget.
        self._scroll = QScrollArea(self)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFixedHeight(CHIP_HEIGHT + 12)

        self._chip_container = QWidget()
        self._chip_row = QHBoxLayout(self._chip_container)
        self._chip_row.setContentsMargins(2, 4, 2, 4)
        self._chip_row.setSpacing(6)
        self._chip_row.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._chip_container.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed
        )
        self._scroll.setWidget(self._chip_container)
        outer.addWidget(self._scroll, 1)

        self._add_btn = TactileButton("+ ADD SOURCE", variant="secondary")
        self._add_btn.setFixedWidth(148)
        self._add_btn.setMinimumHeight(CHIP_HEIGHT - 4)
        self._add_btn.clicked.connect(self.addSourceRequested.emit)
        outer.addWidget(self._add_btn, 0)

    # ------------------------------------------------------------------
    # State push from the host
    # ------------------------------------------------------------------

    def set_slots(
        self,
        slots: list,
        active_index: int,
    ) -> None:
        """
        Reconcile the chip list with the current slots list. Creates
        new chips when slots are added, destroys chips when slots are
        removed, and updates in-place otherwise.
        """
        # Resize the chip list
        while len(self._chips) < len(slots):
            chip = SlotChip(self._chip_container)
            idx = len(self._chips)
            chip.clicked.connect(
                lambda _checked=False, i=idx: self._on_chip_clicked(i)
            )
            chip.primeToggled.connect(
                lambda i=idx: self.primeToggled.emit(i)
            )
            chip.contextMenuRequested.connect(
                lambda pos, i=idx: self.contextMenuRequested.emit(i, pos)
            )
            self._chip_row.addWidget(chip)
            self._chips.append(chip)
        while len(self._chips) > len(slots):
            chip = self._chips.pop()
            self._chip_row.removeWidget(chip)
            chip.setParent(None)
            chip.deleteLater()

        # Push state
        for i, slot in enumerate(slots):
            buf = slot.buffer
            fill_pct = (
                100.0 * buf.buffered_seconds / buf.duration
                if buf.duration > 0
                else 0.0
            )
            self._chips[i].set_state(
                name=slot.name,
                fill_percent=fill_pct,
                is_active=(i == active_index),
                is_capturing=slot.is_capturing(),
                xrun_count=slot.xrun_count(),
                ram_mb=slot.ram_mb(),
            )

    def _on_chip_clicked(self, index: int) -> None:
        # The chip list may have been resized since the lambda was
        # bound, so re-fetch the bound chip's current index instead of
        # trusting the captured one. Simplest: just emit with index
        # — we reconcile lambda bindings every set_slots call since
        # chips are created fresh and old lambdas go away with them.
        self.activeChanged.emit(index)
