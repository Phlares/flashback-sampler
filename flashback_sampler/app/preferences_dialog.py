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
        scratch_dir: str = "",
        on_scratch_dir_changed: Callable[[str], None] | None = None,
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

        root.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
