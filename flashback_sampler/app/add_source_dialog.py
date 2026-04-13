"""
AddSourceDialog — factual modal for building a new CaptureSlot.

Fields:
- NAME: user-given label for the slot ("Discord", "Game", etc.)
- BUFFER LENGTH: seconds of audio to retain (1 .. settings cap)
- SAMPLE RATE: 48000 / 44100 / 32000 / 22050 / 16000 / 8000
- CHANNELS: 1 mono / 2 stereo
- Live RAM footprint readout that updates as you dial

Returns a `result_preset()` (ad-hoc QualityPreset built from the raw
values, with name="CUSTOM") and `result_name()` when exec() returned
Accepted. The plan's named preset ladder (FULL/MUSIC/VOICE/CHAT/
SCRATCH) is gone — the user wants direct control.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from flashback_sampler.app.theme import EREBUS
from flashback_sampler.app.time_format import format_time_cs
from flashback_sampler.core.quality_presets import (
    QualityPreset,
    compute_ram_mb,
)


# Sample rate options, ordered high → low so the default (48k) lands
# at the top of the dropdown.
SAMPLE_RATE_CHOICES: tuple[int, ...] = (
    48_000, 44_100, 32_000, 22_050, 16_000, 8_000,
)
CHANNEL_CHOICES: tuple[tuple[int, str], ...] = (
    (2, "2  STEREO"),
    (1, "1  MONO"),
)


class AddSourceDialog(QDialog):
    """
    Build a new CaptureSlot from raw buffer_length / sample_rate /
    channels / name inputs. The host instantiates with current
    defaults (pulled from settings / CLI args); on accept it calls
    `result_preset()` to get a QualityPreset and `result_name()` to
    get the slot's user-given name.
    """

    def __init__(
        self,
        default_name: str = "",
        default_buffer_seconds: float = 300.0,
        max_buffer_seconds: float = 7200.0,
        default_sample_rate: int = 48_000,
        default_channels: int = 2,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Add Source")
        self.setModal(True)
        self.resize(420, 300)
        self._max_buffer_seconds = int(max(1.0, max_buffer_seconds))
        self._build_ui(
            default_name=default_name,
            default_buffer_seconds=default_buffer_seconds,
            default_sample_rate=default_sample_rate,
            default_channels=default_channels,
        )

    def _build_ui(
        self,
        default_name: str,
        default_buffer_seconds: float,
        default_sample_rate: int,
        default_channels: int,
    ) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)

        title = QLabel("NEW CAPTURE SLOT")
        title.setProperty("role", "label")
        root.addWidget(title)

        form = QFormLayout()
        form.setSpacing(10)

        self._name_edit = QLineEdit(default_name)
        self._name_edit.setPlaceholderText("(auto: SOURCE N)")
        form.addRow(QLabel("NAME"), self._name_edit)

        self._buffer_spin = QSpinBox()
        self._buffer_spin.setRange(1, self._max_buffer_seconds)
        self._buffer_spin.setSuffix("  SEC")
        self._buffer_spin.setSingleStep(10)
        self._buffer_spin.setValue(
            max(1, min(self._max_buffer_seconds, int(round(default_buffer_seconds))))
        )
        self._buffer_spin.valueChanged.connect(self._update_ram_readout)
        form.addRow(QLabel("BUFFER LENGTH"), self._buffer_spin)

        self._sr_combo = QComboBox()
        for rate in SAMPLE_RATE_CHOICES:
            self._sr_combo.addItem(f"{rate // 1000} K   ({rate} HZ)", rate)
        # Select the default
        idx = self._sr_combo.findData(default_sample_rate)
        if idx >= 0:
            self._sr_combo.setCurrentIndex(idx)
        self._sr_combo.currentIndexChanged.connect(self._update_ram_readout)
        form.addRow(QLabel("SAMPLE RATE"), self._sr_combo)

        self._ch_combo = QComboBox()
        for n, label in CHANNEL_CHOICES:
            self._ch_combo.addItem(label, n)
        idx = self._ch_combo.findData(default_channels)
        if idx >= 0:
            self._ch_combo.setCurrentIndex(idx)
        self._ch_combo.currentIndexChanged.connect(self._update_ram_readout)
        form.addRow(QLabel("CHANNELS"), self._ch_combo)

        root.addLayout(form)
        root.addSpacing(4)

        # Live footprint readout
        self._ram_label = QLabel("")
        self._ram_label.setStyleSheet(
            f"color: {EREBUS['bone']}; padding: 4px 0;"
        )
        root.addWidget(self._ram_label)
        self._update_ram_readout()

        root.addStretch(1)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    # ------------------------------------------------------------------
    # Live RAM readout
    # ------------------------------------------------------------------

    def _current_inputs(self) -> tuple[int, int, int]:
        return (
            int(self._sr_combo.currentData()),
            int(self._ch_combo.currentData()),
            int(self._buffer_spin.value()),
        )

    def _update_ram_readout(self) -> None:
        sr, ch, secs = self._current_inputs()
        mb = compute_ram_mb(sr, ch, float(secs))
        ch_label = "STEREO" if ch == 2 else "MONO"
        self._ram_label.setText(
            f"≈ {mb:6.1f} MB    ({sr // 1000}k {ch_label}, "
            f"{format_time_cs(secs)})"
        )

    # ------------------------------------------------------------------
    # Result accessors
    # ------------------------------------------------------------------

    def result_preset(self) -> QualityPreset | None:
        sr, ch, secs = self._current_inputs()
        return QualityPreset(
            name="CUSTOM",
            sample_rate=sr,
            channels=ch,
            buffer_seconds=float(secs),
            description="",
        )

    def result_name(self) -> str:
        return self._name_edit.text().strip()
