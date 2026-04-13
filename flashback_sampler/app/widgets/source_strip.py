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

from flashback_sampler.app.widgets.capture_all_button import (
    CAPTURE_ALL_HEIGHT,
    CaptureAllButton,
)
from flashback_sampler.app.widgets.slot_chip import (
    CHIP_HEIGHT,
    SlotChip,
    short_source_name,
)
from flashback_sampler.app.widgets.tactile_button import TactileButton


class SourceStrip(QWidget):
    activeChanged = Signal(int)
    primeToggled = Signal(int)
    addSourceRequested = Signal()
    captureAllClicked = Signal()
    contextMenuRequested = Signal(int, QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chips: list[SlotChip] = []
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        # CAPTURE ALL master fader at the left edge of the row. Replaces
        # the old "SOURCES" label — the button IS the semantic header.
        self._master_btn = CaptureAllButton()
        self._master_btn.clicked.connect(self.captureAllClicked.emit)
        outer.addWidget(self._master_btn, 0)

        # Scrollable horizontal area for the chips so many slots still
        # fit on a narrow window. Taller now to match the 104 px master
        # button height with a matching vertical gutter.
        self._scroll = QScrollArea(self)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFixedHeight(CAPTURE_ALL_HEIGHT + 8)

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

    def set_master_state(
        self,
        armed_count: int,
        total_count: int,
        is_rolling: bool = False,
    ) -> None:
        """Push (armed, total, rolling) into the master START/STOP button."""
        self._master_btn.set_state(armed_count, total_count, is_rolling)

    def master_button(self) -> "CaptureAllButton":
        return self._master_btn

    def set_slots(
        self,
        slots: list,
        active_index: int,
        source_names: list | None = None,
        is_rolling: bool = False,
    ) -> None:
        """
        Reconcile the chip list with the current slots list. Creates
        new chips when slots are added, destroys chips when slots are
        removed, and updates in-place otherwise.

        `source_names`, if provided, is a list parallel to `slots`
        containing the short display name of each slot's effective
        capture device. If None, no source line is shown.
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
            src_short = ""
            if source_names is not None and i < len(source_names):
                src_short = source_names[i] or ""
            # A capture-source error pre-empts the device line so the
            # user sees WHY a chip isn't recording without reading
            # stdout. The chip renders the error line in red.
            err_line = ""
            try:
                err = slot.last_error() if hasattr(slot, "last_error") else None
            except Exception:
                err = None
            if err:
                # Strip noisy prefixes and trim to chip width
                short = err.split(":", 1)[0]
                if len(short) > 22:
                    short = short[:21] + "…"
                err_line = short
            self._chips[i].set_state(
                name=slot.name,
                fill_percent=fill_pct,
                is_active=(i == active_index),
                is_capturing=slot.is_capturing(),
                is_armed=bool(getattr(slot, "armed", True)),
                is_rolling=bool(is_rolling),
                xrun_count=slot.xrun_count(),
                ram_mb=slot.ram_mb(),
                source_short=err_line or src_short,
                has_error=bool(err_line),
            )

    def _on_chip_clicked(self, index: int) -> None:
        # The chip list may have been resized since the lambda was
        # bound, so re-fetch the bound chip's current index instead of
        # trusting the captured one. Simplest: just emit with index
        # — we reconcile lambda bindings every set_slots call since
        # chips are created fresh and old lambdas go away with them.
        self.activeChanged.emit(index)
