"""
ProcessPickerDialog — pick a running Windows process to capture from
via WASAPI per-process loopback.

Enumerates every process with a readable executable name via the
psapi.dll helpers in flashback_sampler.io.win32_process_loopback.
The user sees a filterable list of `PID  EXE_NAME` rows; clicking
OK returns a CaptureDevice with kind="process_loopback", id=str(pid),
name=exe_name. On non-Windows platforms, the dialog shows a single
row explaining that the feature is Windows-only.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from flashback_sampler.app.audio_devices import CaptureDevice
from flashback_sampler.app.theme import EREBUS
from flashback_sampler.io.win32_process_loopback import (
    enumerate_audio_processes,
    is_supported,
)


class ProcessPickerDialog(QDialog):
    """
    Modal dialog that presents running processes. exec() returns
    Accepted on OK (use `result_device()` to extract the chosen
    CaptureDevice) or Rejected on Cancel.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Capture from Process")
        self.setModal(True)
        self.resize(540, 520)
        self._selected_device: CaptureDevice | None = None
        self._all_rows: list[tuple[int, str]] = []
        self._build_ui()
        self._populate()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(10)

        title = QLabel("SELECT A PROCESS")
        title.setProperty("role", "label")
        root.addWidget(title)

        if not is_supported():
            hint = QLabel(
                "Per-process capture requires Windows 10 build 19041 "
                "(May 2020) or newer. On this platform, Add Source → "
                "your mic or the system speakers loopback instead."
            )
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {EREBUS['bone']};")
            root.addWidget(hint)
            root.addStretch(1)

            btns = QDialogButtonBox(QDialogButtonBox.Cancel)
            btns.rejected.connect(self.reject)
            root.addWidget(btns)
            return

        # Filter row
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_lbl = QLabel("FILTER")
        filter_lbl.setProperty("role", "label")
        filter_lbl.setFixedWidth(60)
        filter_row.addWidget(filter_lbl)
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("(type to filter by exe name)")
        self._filter_edit.textChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._filter_edit, 1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setFocusPolicy(Qt.NoFocus)
        refresh_btn.clicked.connect(self._populate)
        filter_row.addWidget(refresh_btn, 0)
        root.addLayout(filter_row)

        # Process list
        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _i: self.accept())
        root.addWidget(self._list, 1)

        # OK / Cancel
        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _populate(self) -> None:
        if not hasattr(self, "_list"):
            return
        self._all_rows = enumerate_audio_processes()
        self._apply_filter(self._filter_edit.text() if hasattr(self, "_filter_edit") else "")

    def _on_filter_changed(self, text: str) -> None:
        self._apply_filter(text)

    def _apply_filter(self, needle: str) -> None:
        self._list.clear()
        needle_low = (needle or "").strip().lower()
        for pid, name in self._all_rows:
            if needle_low and needle_low not in name.lower():
                continue
            label = f"  {pid:>6}   {name}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, (pid, name))
            self._list.addItem(item)

    def accept(self) -> None:  # noqa: D401
        if hasattr(self, "_list"):
            item = self._list.currentItem()
            if item is not None:
                data = item.data(Qt.UserRole)
                if isinstance(data, tuple) and len(data) == 2:
                    pid, name = data
                    self._selected_device = CaptureDevice(
                        kind="process_loopback",
                        name=f"{name} (pid {pid})",
                        id=str(pid),
                    )
        super().accept()

    def result_device(self) -> CaptureDevice | None:
        return self._selected_device
