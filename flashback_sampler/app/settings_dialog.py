"""
SettingsDialog — modal settings editor for buffer + checkout + save defaults.

Backed by the JSON config at %APPDATA%/flashback-sampler/config.json
(same file as M7 device selections). Splits into pure-Python helpers
(validation, default resolution) so unit tests can cover the logic
without instantiating a QDialog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)


# Default values + validation bounds. Centralized so both the dialog
# and the settings-load helper use the same numbers.
DEFAULT_BUFFER_MINUTES = 15.0
MIN_BUFFER_MINUTES = 0.1
MAX_BUFFER_MINUTES = 120.0

DEFAULT_MAX_CHECKOUTS = 16
MIN_MAX_CHECKOUTS = 1
MAX_MAX_CHECKOUTS = 64

DEFAULT_MAX_RAM_MB = 1024
MIN_MAX_RAM_MB = 128
MAX_MAX_RAM_MB = 8192

DEFAULT_PROJECT_RAM_BUDGET_MB = 4096
MIN_PROJECT_RAM_BUDGET_MB = 256
MAX_PROJECT_RAM_BUDGET_MB = 32768


@dataclass
class AppSettings:
    """
    Serializable snapshot of the user-editable settings. Maps 1-to-1 to
    the config.json "settings" key.
    """
    buffer_minutes: float = DEFAULT_BUFFER_MINUTES
    max_checkouts: int = DEFAULT_MAX_CHECKOUTS
    max_ram_mb: int = DEFAULT_MAX_RAM_MB
    project_ram_budget_mb: int = DEFAULT_PROJECT_RAM_BUDGET_MB
    save_directory: str = ""  # empty = fall back to Documents

    def to_dict(self) -> dict:
        return {
            "buffer_minutes": float(self.buffer_minutes),
            "max_checkouts": int(self.max_checkouts),
            "max_ram_mb": int(self.max_ram_mb),
            "project_ram_budget_mb": int(self.project_ram_budget_mb),
            "save_directory": str(self.save_directory),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "AppSettings":
        """Parse a (possibly partial / malformed) settings dict with clamping."""
        if not isinstance(data, dict):
            return cls()

        def _get_float(key, default, lo, hi) -> float:
            v = data.get(key, default)
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))

        def _get_int(key, default, lo, hi) -> int:
            v = data.get(key, default)
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))

        return cls(
            buffer_minutes=_get_float(
                "buffer_minutes",
                DEFAULT_BUFFER_MINUTES,
                MIN_BUFFER_MINUTES,
                MAX_BUFFER_MINUTES,
            ),
            max_checkouts=_get_int(
                "max_checkouts",
                DEFAULT_MAX_CHECKOUTS,
                MIN_MAX_CHECKOUTS,
                MAX_MAX_CHECKOUTS,
            ),
            max_ram_mb=_get_int(
                "max_ram_mb",
                DEFAULT_MAX_RAM_MB,
                MIN_MAX_RAM_MB,
                MAX_MAX_RAM_MB,
            ),
            project_ram_budget_mb=_get_int(
                "project_ram_budget_mb",
                DEFAULT_PROJECT_RAM_BUDGET_MB,
                MIN_PROJECT_RAM_BUDGET_MB,
                MAX_PROJECT_RAM_BUDGET_MB,
            ),
            save_directory=str(data.get("save_directory", "") or ""),
        )

    def resolved_save_directory(self) -> Path:
        """
        Return the effective save directory: the configured one if it
        exists, otherwise ~/Documents, otherwise ~.
        """
        if self.save_directory:
            p = Path(self.save_directory)
            if p.exists():
                return p
        docs = Path.home() / "Documents"
        if docs.exists():
            return docs
        return Path.home()


def load_settings_from_config(config_data: dict) -> AppSettings:
    """
    Extract AppSettings from a full config dict (the kind returned by
    flashback_sampler.app.config.load_config()). The settings live
    under the top-level "settings" key; anything else in the config
    (device selections, etc.) is ignored.
    """
    return AppSettings.from_dict(config_data.get("settings"))


def apply_settings_to_config(config_data: dict, settings: AppSettings) -> dict:
    """Return a NEW config dict with the updated settings merged in."""
    new_cfg = dict(config_data)
    new_cfg["settings"] = settings.to_dict()
    return new_cfg


class SettingsDialog(QDialog):
    """
    Modal settings editor. Instantiate with an initial AppSettings,
    exec() it, then call .result_settings() to retrieve the user's
    edits (only valid when exec() returned Accepted).
    """

    def __init__(self, initial: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self._initial = initial
        self._build_ui()
        self._populate_from(initial)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self._buffer_mins = QSpinBox()
        self._buffer_mins.setRange(1, int(MAX_BUFFER_MINUTES * 10))
        self._buffer_mins.setSuffix("  ×0.1 MIN")
        self._buffer_mins.setToolTip(
            "Ring buffer length in tenths of a minute. 150 = 15.0 min. "
            "Changing this discards currently buffered audio (existing "
            "checkouts are preserved)."
        )
        form.addRow(QLabel("BUFFER DURATION"), self._buffer_mins)

        self._max_checkouts = QSpinBox()
        self._max_checkouts.setRange(MIN_MAX_CHECKOUTS, MAX_MAX_CHECKOUTS)
        form.addRow(QLabel("MAX SIMULTANEOUS CHECKOUTS"), self._max_checkouts)

        self._max_ram = QSpinBox()
        self._max_ram.setRange(MIN_MAX_RAM_MB, MAX_MAX_RAM_MB)
        self._max_ram.setSuffix("  MB")
        form.addRow(QLabel("MAX CHECKOUT RAM"), self._max_ram)

        self._project_ram = QSpinBox()
        self._project_ram.setRange(MIN_PROJECT_RAM_BUDGET_MB, MAX_PROJECT_RAM_BUDGET_MB)
        self._project_ram.setSuffix("  MB")
        self._project_ram.setToolTip(
            "Total RAM budget across ALL capture slots. Adding a new "
            "source that would push the project past this cap is "
            "blocked by the Add Source dialog."
        )
        form.addRow(QLabel("PROJECT RAM BUDGET"), self._project_ram)

        # Save directory row with a Browse button
        save_dir_row = QHBoxLayout()
        save_dir_row.setSpacing(6)
        self._save_dir_edit = QLineEdit()
        self._save_dir_edit.setPlaceholderText("(Documents folder)")
        save_dir_row.addWidget(self._save_dir_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._pick_save_dir)
        save_dir_row.addWidget(browse_btn, 0)
        form.addRow(QLabel("DEFAULT SAVE DIRECTORY"), save_dir_row)

        root.addLayout(form)
        root.addSpacing(8)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def _populate_from(self, s: AppSettings) -> None:
        self._buffer_mins.setValue(int(round(s.buffer_minutes * 10)))
        self._max_checkouts.setValue(int(s.max_checkouts))
        self._max_ram.setValue(int(s.max_ram_mb))
        self._project_ram.setValue(int(s.project_ram_budget_mb))
        self._save_dir_edit.setText(s.save_directory or "")

    def _pick_save_dir(self) -> None:
        current = self._save_dir_edit.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Default save directory", current
        )
        if folder:
            self._save_dir_edit.setText(folder)

    def result_settings(self) -> AppSettings:
        return AppSettings(
            buffer_minutes=self._buffer_mins.value() / 10.0,
            max_checkouts=self._max_checkouts.value(),
            max_ram_mb=self._max_ram.value(),
            project_ram_budget_mb=self._project_ram.value(),
            save_directory=self._save_dir_edit.text().strip(),
        )

    def buffer_changed_from_initial(self, new: AppSettings) -> bool:
        """Whether the user edited the buffer duration since opening."""
        return abs(new.buffer_minutes - self._initial.buffer_minutes) > 1e-6
