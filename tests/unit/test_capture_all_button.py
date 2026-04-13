"""
CaptureAllButton — state logic + signal wiring tests. Paint code is
exercised by the main-window smoke path; this file focuses on the
pure Python contract.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.widgets.capture_all_button import CaptureAllButton


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_initial_state_is_none_primed(qapp):
    b = CaptureAllButton()
    assert b.primed_count() == 0
    assert b.total_count() == 1
    assert b.is_all_primed() is False
    assert b.text() == "CAPTURE ALL"


def test_set_state_none_primed_shows_capture_all_label(qapp):
    b = CaptureAllButton()
    b.set_state(0, 4)
    assert b.primed_count() == 0
    assert b.total_count() == 4
    assert b.is_all_primed() is False
    assert b.text() == "CAPTURE ALL"


def test_set_state_some_primed_keeps_capture_all_label(qapp):
    b = CaptureAllButton()
    b.set_state(2, 4)
    assert b.primed_count() == 2
    assert b.total_count() == 4
    assert b.is_all_primed() is False
    assert b.text() == "CAPTURE ALL"


def test_set_state_all_primed_flips_label_to_stop_all(qapp):
    b = CaptureAllButton()
    b.set_state(4, 4)
    assert b.primed_count() == 4
    assert b.total_count() == 4
    assert b.is_all_primed() is True
    assert b.text() == "STOP ALL"


def test_set_state_clamps_primed_to_total(qapp):
    b = CaptureAllButton()
    b.set_state(9, 4)  # primed > total
    assert b.primed_count() == 4


def test_set_state_clamps_negative_primed_to_zero(qapp):
    b = CaptureAllButton()
    b.set_state(-3, 4)
    assert b.primed_count() == 0


def test_set_state_enforces_minimum_total_of_one(qapp):
    b = CaptureAllButton()
    b.set_state(0, 0)
    assert b.total_count() == 1


def test_is_all_primed_false_when_total_zero_clamped_to_one(qapp):
    b = CaptureAllButton()
    # total clamps to 1, primed stays 0 → not all primed
    b.set_state(0, 0)
    assert b.is_all_primed() is False


def test_pulse_timer_starts_when_all_primed(qapp):
    b = CaptureAllButton()
    b.set_state(4, 4)
    assert b._pulse_timer.isActive() is True


def test_pulse_timer_stops_when_any_unprimed(qapp):
    b = CaptureAllButton()
    b.set_state(4, 4)
    assert b._pulse_timer.isActive() is True
    b.set_state(3, 4)
    assert b._pulse_timer.isActive() is False
    assert b._pulse_phase == 0.0


def test_clicked_signal_fires(qapp):
    b = CaptureAllButton()
    fired = []
    b.clicked.connect(lambda: fired.append(1))
    b.click()
    assert fired == [1]


def test_fixed_size_is_the_spec_size(qapp):
    from flashback_sampler.app.widgets.capture_all_button import (
        CAPTURE_ALL_HEIGHT,
        CAPTURE_ALL_WIDTH,
    )
    b = CaptureAllButton()
    assert b.minimumWidth() == CAPTURE_ALL_WIDTH
    assert b.minimumHeight() == CAPTURE_ALL_HEIGHT
    assert b.maximumWidth() == CAPTURE_ALL_WIDTH
    assert b.maximumHeight() == CAPTURE_ALL_HEIGHT
