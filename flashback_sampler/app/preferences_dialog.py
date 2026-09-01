"""PreferencesDialog — the app's main settings page.

Home for app-wide preferences. Changes apply live via the supplied callbacks,
so there's no OK/Apply step — Close just dismisses.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)


class PreferencesDialog(QDialog):
    def __init__(
        self,
        *,
        show_notifications: bool,
        on_notifications_changed: Callable[[bool], None],
        global_hotkeys_enabled: bool = False,
        on_global_hotkeys_changed: Callable[[bool], None] | None = None,
        global_hotkeys_supported: bool = True,
        export_pool_dir: str = "",
        on_export_pool_dir_changed: Callable[[str], None] | None = None,
        export_bit_depth: str = "FLOAT",
        on_export_bit_depth_changed: Callable[[str], None] | None = None,
        drag_handle_mb: float = 200.0,
        on_drag_handle_mb_changed: Callable[[float], None] | None = None,
        scratch_dir: str = "",
        on_scratch_dir_changed: Callable[[str], None] | None = None,
        max_footprint_mb: float = 0.0,
        on_max_footprint_changed: Callable[[float], None] | None = None,
        mem_total_mb: float = 0.0,
        mem_free_mb: float = 0.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(380)

        root = QVBoxLayout(self)

        root.addWidget(QLabel("<b>Notifications</b>"))
        self.notify_check = QCheckBox("Show tray notifications (toasts)")
        self.notify_check.setChecked(show_notifications)
        self.notify_check.toggled.connect(lambda c: on_notifications_changed(c))
        root.addWidget(self.notify_check)

        root.addSpacing(10)
        root.addWidget(QLabel("<b>Keybindings</b>"))
        self.global_hotkeys_check = QCheckBox("Enable keybindings while minimized")
        self.global_hotkeys_check.setChecked(
            global_hotkeys_enabled and global_hotkeys_supported
        )
        self.global_hotkeys_check.setEnabled(global_hotkeys_supported)
        if not global_hotkeys_supported:
            self.global_hotkeys_check.setToolTip("Not available on this platform yet.")
        if on_global_hotkeys_changed is not None:
            self.global_hotkeys_check.toggled.connect(
                lambda c: on_global_hotkeys_changed(c)
            )
        root.addWidget(self.global_hotkeys_check)
        hint = QLabel(
            "Lets Check Out / Start / Stop fire from a global shortcut "
            "(e.g. Ctrl+Alt+O) even when the window is hidden to the tray."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8c867b; font-size: 8pt;")
        root.addWidget(hint)

        root.addSpacing(10)
        root.addWidget(QLabel("<b>Export</b>"))
        dir_row = QHBoxLayout()
        self.export_dir_edit = QLineEdit(export_pool_dir)
        self.export_dir_edit.setReadOnly(True)
        self.export_dir_btn = QPushButton("Browse…")

        def _pick_export_dir() -> None:
            chosen = QFileDialog.getExistingDirectory(
                self, "Export folder", self.export_dir_edit.text()
            )
            if not chosen:
                return
            self.export_dir_edit.setText(chosen)
            if on_export_pool_dir_changed is not None:
                on_export_pool_dir_changed(chosen)

        self.export_dir_btn.clicked.connect(_pick_export_dir)
        dir_row.addWidget(self.export_dir_edit, 1)
        dir_row.addWidget(self.export_dir_btn)
        root.addLayout(dir_row)

        self.export_depth_combo = QComboBox()
        for label, value in (
            ("32-bit float", "FLOAT"),
            ("24-bit PCM", "PCM_24"),
            ("16-bit PCM", "PCM_16"),
        ):
            self.export_depth_combo.addItem(label, value)
        depth_idx = self.export_depth_combo.findData(export_bit_depth)
        if depth_idx >= 0:
            self.export_depth_combo.setCurrentIndex(depth_idx)

        def _depth_changed(_i: int) -> None:
            if on_export_bit_depth_changed is not None:
                on_export_bit_depth_changed(self.export_depth_combo.currentData())

        self.export_depth_combo.currentIndexChanged.connect(_depth_changed)
        root.addWidget(self.export_depth_combo)

        drag_cap_row = QHBoxLayout()
        drag_cap_row.addWidget(QLabel("Drag-out handles (tunable): add up to"))
        self.drag_cap_spin = QSpinBox()
        self.drag_cap_spin.setRange(0, 100000)
        self.drag_cap_spin.setSuffix(" MB")
        self.drag_cap_spin.setValue(int(drag_handle_mb))

        def _drag_cap_changed(v: int) -> None:
            if on_drag_handle_mb_changed is not None:
                on_drag_handle_mb_changed(float(v))

        self.drag_cap_spin.valueChanged.connect(_drag_cap_changed)
        drag_cap_row.addWidget(self.drag_cap_spin, 1)
        root.addLayout(drag_cap_row)
        self.drag_cap_hint = QLabel(
            "of extra parent audio before and after a dragged slice, with "
            "markers at the slice, so the DAW can recover more than you "
            "sliced. The slice itself is always exported whole. "
            "0 = slice only — use it on constrained systems: the handles "
            "also size the buffer-deck root's RAM copy."
        )
        self.drag_cap_hint.setWordWrap(True)
        self.drag_cap_hint.setStyleSheet("color: #8c867b; font-size: 8pt;")
        root.addWidget(self.drag_cap_hint)

        root.addSpacing(10)
        root.addWidget(QLabel("<b>Scratch</b>"))
        scratch_row = QHBoxLayout()
        self.scratch_dir_edit = QLineEdit(scratch_dir)
        self.scratch_dir_edit.setReadOnly(True)
        self.scratch_dir_btn = QPushButton("Browse…")

        def _pick_scratch_dir() -> None:
            chosen = QFileDialog.getExistingDirectory(
                self, "Scratch folder", self.scratch_dir_edit.text()
            )
            if not chosen:
                return
            self.scratch_dir_edit.setText(chosen)
            if on_scratch_dir_changed is not None:
                on_scratch_dir_changed(chosen)

        self.scratch_dir_btn.clicked.connect(_pick_scratch_dir)
        scratch_row.addWidget(self.scratch_dir_edit, 1)
        scratch_row.addWidget(self.scratch_dir_btn)
        root.addLayout(scratch_row)
        scratch_hint = QLabel(
            "Checkouts are written here as they are made. Applies at next launch."
        )
        scratch_hint.setWordWrap(True)
        scratch_hint.setStyleSheet("color: #8c867b; font-size: 8pt;")
        root.addWidget(scratch_hint)

        root.addSpacing(10)
        root.addWidget(QLabel("<b>Memory</b>"))
        footprint_row = QHBoxLayout()
        footprint_row.addWidget(QLabel("Max footprint"))
        self.footprint_spin = QSpinBox()
        self.footprint_spin.setRange(0, 1 << 30)
        self.footprint_spin.setSingleStep(1024)
        self.footprint_spin.setSuffix(" MB")
        self.footprint_spin.setSpecialValueText("no cap")  # shown at 0
        self.footprint_spin.setValue(int(max_footprint_mb))

        def _footprint_edited() -> None:
            if on_max_footprint_changed is not None:
                on_max_footprint_changed(float(self.footprint_spin.value()))

        # editingFinished, not valueChanged: one callback per typed value,
        # not one per keystroke.
        self.footprint_spin.editingFinished.connect(_footprint_edited)
        footprint_row.addWidget(self.footprint_spin, 1)
        root.addLayout(footprint_row)
        self.footprint_hint = QLabel(
            "A safety line for the session's resident audio, not a reservation. "
            "Default is 25 % of physical RAM; 0 = no cap. "
            f"Physical RAM {mem_total_mb:,.0f} MB, free now {mem_free_mb:,.0f} MB."
        )
        self.footprint_hint.setWordWrap(True)
        self.footprint_hint.setStyleSheet("color: #8c867b; font-size: 8pt;")
        root.addWidget(self.footprint_hint)

        root.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
