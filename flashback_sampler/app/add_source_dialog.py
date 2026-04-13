"""
AddSourceDialog — modal that asks the user to pick a QualityPreset
and name a new CaptureSlot. Returns an (preset, name) tuple on
Accepted, None on Cancelled.
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
    QVBoxLayout,
)

from flashback_sampler.app.theme import EREBUS
from flashback_sampler.app.time_format import format_time_cs
from flashback_sampler.core.quality_presets import (
    DEFAULT_PRESET_NAME,
    PRESETS,
    QualityPreset,
    preset_by_name,
)


class AddSourceDialog(QDialog):
    """
    Pure Qt dialog — no dependency on the rest of the app layer. The
    host instantiates this with a default name suggestion, exec()s
    it, and reads the selected preset via `result_preset()` /
    `result_name()` when exec() returned Accepted.
    """

    def __init__(self, default_name: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Source")
        self.setModal(True)
        self.resize(440, 380)
        self._build_ui(default_name=default_name)

    def _build_ui(self, default_name: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)

        title = QLabel("NEW CAPTURE SLOT")
        title.setProperty("role", "label")
        root.addWidget(title)

        # Name field row
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_lbl = QLabel("NAME")
        name_lbl.setProperty("role", "label")
        name_lbl.setFixedWidth(64)
        name_row.addWidget(name_lbl)
        self._name_edit = QLineEdit(default_name)
        self._name_edit.setPlaceholderText("(auto: SOURCE N)")
        name_row.addWidget(self._name_edit, 1)
        root.addLayout(name_row)

        # Preset picker (QListWidget — one row per QualityPreset)
        preset_lbl = QLabel("QUALITY PRESET")
        preset_lbl.setProperty("role", "label")
        root.addWidget(preset_lbl)

        self._preset_list = QListWidget()
        for preset in PRESETS:
            item = QListWidgetItem(
                f"  {preset.name:<9s}   {preset.sample_rate // 1000}K  "
                f"{preset.channels}CH   {format_time_cs(preset.buffer_seconds)}"
                f"   ~{preset.ram_mb():5.0f} MB"
            )
            item.setData(Qt.UserRole, preset.name)
            self._preset_list.addItem(item)
        self._preset_list.itemSelectionChanged.connect(
            self._on_preset_selection_changed
        )
        # Select the default
        for i in range(self._preset_list.count()):
            if self._preset_list.item(i).data(Qt.UserRole) == DEFAULT_PRESET_NAME:
                self._preset_list.setCurrentRow(i)
                break
        root.addWidget(self._preset_list, 1)

        # Preset description line
        self._desc_label = QLabel("")
        self._desc_label.setWordWrap(True)
        self._desc_label.setStyleSheet(f"color: {EREBUS['bone']};")
        root.addWidget(self._desc_label)
        self._on_preset_selection_changed()

        # OK / Cancel
        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _on_preset_selection_changed(self) -> None:
        preset = self.result_preset()
        if preset is None:
            self._desc_label.setText("")
            return
        self._desc_label.setText(preset.description)

    def result_preset(self) -> QualityPreset | None:
        item = self._preset_list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.UserRole)
        return preset_by_name(name)

    def result_name(self) -> str:
        return self._name_edit.text().strip()
