"""Unit tests for the Add Source dialog's sample-rate offerings."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.add_source_dialog import (
    SAMPLE_RATE_CHOICES,
    AddSourceDialog,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_hi_res_rates_offered_descending():
    assert SAMPLE_RATE_CHOICES == (
        192000, 176400, 96000, 88200, 48000, 44100, 32000, 22050, 16000, 8000
    )


def test_dialog_still_defaults_to_48k(qapp):
    dlg = AddSourceDialog(default_name="Deck 1")
    assert dlg.result_preset().sample_rate == 48000


def test_ram_readout_states_reservation(qapp):
    # The ring is fully committed at slot creation -- the readout must
    # say "Reserves", not just show a bare number, so it can't be read
    # as a live/current usage figure.
    dlg = AddSourceDialog(
        default_name="Deck 1",
        default_buffer_seconds=900.0,
        default_sample_rate=48_000,
        default_channels=2,
    )
    text = dlg._ram_label.text()
    assert text.startswith("Reserves")
    assert "48k STEREO" in text
    assert "15:00" in text
