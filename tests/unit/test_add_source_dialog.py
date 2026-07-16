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
