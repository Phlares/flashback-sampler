"""PreferencesDialog — the app's main settings page.

Minimal today (a single notifications toggle), but the home for future
app-wide preferences. Changes apply live via the supplied callback, so
there's no OK/Apply step — Close just dismisses.
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
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(320)
        self._on_notifications_changed = on_notifications_changed

        root = QVBoxLayout(self)
        root.addWidget(QLabel("<b>Notifications</b>"))
        self.notify_check = QCheckBox("Show tray notifications (toasts)")
        self.notify_check.setChecked(show_notifications)
        self.notify_check.toggled.connect(self._on_toggled)
        root.addWidget(self.notify_check)
        root.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    def _on_toggled(self, checked: bool) -> None:
        self._on_notifications_changed(checked)
