"""
CaptureAllButton — state logic + signal wiring tests. Paint code is
exercised by the main-window smoke path; this file focuses on the
pure Python contract.

The button's state is (armed_count, total_count, is_rolling). Label
flips on `is_rolling`: START CAPTURE when stopped, STOP CAPTURE when
rolling. Pulse timer runs only while rolling.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.widgets.capture_all_button import CaptureAllButton


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_initial_state_is_stopped_with_start_label(qapp):
    b = CaptureAllButton()
    assert b.armed_count() == 0
    assert b.total_count() == 1
    assert b.is_rolling() is False
    assert b.text() == "START CAPTURE"


def test_stopped_with_some_armed_keeps_start_label(qapp):
    b = CaptureAllButton()
    b.set_state(2, 4, False)
    assert b.armed_count() == 2
    assert b.total_count() == 4
    assert b.is_rolling() is False
    assert b.text() == "START CAPTURE"


def test_stopped_with_all_armed_keeps_start_label(qapp):
    b = CaptureAllButton()
    b.set_state(4, 4, False)
    assert b.armed_count() == 4
    assert b.is_rolling() is False
    assert b.text() == "START CAPTURE"


def test_rolling_flips_label_to_stop_capture(qapp):
    b = CaptureAllButton()
    b.set_state(3, 4, True)
    assert b.is_rolling() is True
    assert b.text() == "STOP CAPTURE"


def test_rolling_with_zero_armed_still_shows_stop_label(qapp):
    b = CaptureAllButton()
    b.set_state(0, 4, True)
    assert b.text() == "STOP CAPTURE"


def test_set_state_clamps_armed_to_total(qapp):
    b = CaptureAllButton()
    b.set_state(9, 4, False)
    assert b.armed_count() == 4


def test_set_state_clamps_negative_armed_to_zero(qapp):
    b = CaptureAllButton()
    b.set_state(-3, 4, False)
    assert b.armed_count() == 0


def test_set_state_enforces_minimum_total_of_one(qapp):
    b = CaptureAllButton()
    b.set_state(0, 0, False)
    assert b.total_count() == 1


def test_pulse_timer_starts_when_rolling(qapp):
    b = CaptureAllButton()
    b.set_state(4, 4, True)
    assert b._pulse_timer.isActive() is True


def test_pulse_timer_stops_when_stopped(qapp):
    b = CaptureAllButton()
    b.set_state(4, 4, True)
    assert b._pulse_timer.isActive() is True
    b.set_state(4, 4, False)
    assert b._pulse_timer.isActive() is False
    assert b._pulse_phase == 0.0


def test_pulse_does_not_run_when_stopped_even_with_all_armed(qapp):
    b = CaptureAllButton()
    b.set_state(4, 4, False)
    assert b._pulse_timer.isActive() is False


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
