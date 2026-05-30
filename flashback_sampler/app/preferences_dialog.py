"""PreferencesDialog — the app's main settings page.

Home for app-wide preferences. Changes apply live via the supplied callbacks,
so there's no OK/Apply step — Close just dismisses.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
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

        root.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
