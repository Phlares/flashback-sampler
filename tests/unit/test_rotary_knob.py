"""
Rotary knob interaction tests — setValue / setRange clamping, and the
wheel / keyboard stepping logic with modifiers.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.widgets.rotary_knob import (
    WHEEL_STEP_COARSE_MULTIPLIER,
    WHEEL_STEP_FINE_MULTIPLIER,
    WHEEL_STEP_FRAC,
    RotaryKnob,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ─────────────────────────────────────────────────────────────────────────
# Range / clamp / default
# ─────────────────────────────────────────────────────────────────────────


def test_set_range_preserves_value_if_still_in_range(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 100.0)
    k.setValue(40.0)
    k.setRange(0.0, 50.0)
    assert k.value() == 40.0


def test_set_range_clamps_value_if_out_of_range(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 100.0)
    k.setValue(80.0)
    k.setRange(0.0, 50.0)
    assert k.value() == 50.0


def test_set_value_clamps_to_range(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 100.0)
    k.setValue(-10.0)
    assert k.value() == 0.0
    k.setValue(9999.0)
    assert k.value() == 100.0


def test_value_changed_signal_fires(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 100.0)
    captured: list[float] = []
    k.valueChanged.connect(lambda v: captured.append(v))
    k.setValue(42.0)
    assert captured == [42.0]


def test_value_changed_not_fired_on_no_change(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 100.0)
    k.setValue(42.0)
    captured: list[float] = []
    k.valueChanged.connect(lambda v: captured.append(v))
    k.setValue(42.0)  # same value
    assert captured == []


def test_default_value_applied_by_double_click_reset(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 100.0)
    k.setDefaultValue(25.0)
    k.setValue(80.0)
    k.setValue(k._default_value)  # the path double-click uses
    assert k.value() == 25.0


# ─────────────────────────────────────────────────────────────────────────
# _modifier_step_value — the wheel/key step math
# ─────────────────────────────────────────────────────────────────────────


def test_modifier_step_default_is_one_sixtieth_of_range(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    step = k._modifier_step_value(Qt.NoModifier)
    assert step == pytest.approx(600.0 * WHEEL_STEP_FRAC)  # 10.0


def test_modifier_step_shift_is_coarse(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    step = k._modifier_step_value(Qt.ShiftModifier)
    assert step == pytest.approx(
        600.0 * WHEEL_STEP_FRAC * WHEEL_STEP_COARSE_MULTIPLIER
    )  # 50.0


def test_modifier_step_ctrl_is_fine(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    step = k._modifier_step_value(Qt.ControlModifier)
    assert step == pytest.approx(
        600.0 * WHEEL_STEP_FRAC * WHEEL_STEP_FINE_MULTIPLIER
    )  # 2.0


# ─────────────────────────────────────────────────────────────────────────
# Keyboard interaction — uses key events on a real widget
# ─────────────────────────────────────────────────────────────────────────


def _send_key(knob, key, modifiers=Qt.NoModifier):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    ev = QKeyEvent(QEvent.KeyPress, key, modifiers)
    knob.keyPressEvent(ev)


def test_arrow_up_increases_value(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    k.setValue(100.0)
    _send_key(k, Qt.Key_Up)
    assert k.value() > 100.0
    assert k.value() == pytest.approx(110.0)  # default step of 10


def test_arrow_down_decreases_value(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    k.setValue(100.0)
    _send_key(k, Qt.Key_Down)
    assert k.value() == pytest.approx(90.0)


def test_shift_arrow_is_5x_step(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    k.setValue(100.0)
    _send_key(k, Qt.Key_Up, Qt.ShiftModifier)
    assert k.value() == pytest.approx(100.0 + 50.0)


def test_ctrl_arrow_is_fine_step(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    k.setValue(100.0)
    _send_key(k, Qt.Key_Up, Qt.ControlModifier)
    assert k.value() == pytest.approx(100.0 + 2.0)


def test_home_jumps_to_min(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    k.setValue(300.0)
    _send_key(k, Qt.Key_Home)
    assert k.value() == 0.0


def test_end_jumps_to_max(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    k.setValue(300.0)
    _send_key(k, Qt.Key_End)
    assert k.value() == 600.0


def test_enter_snaps_to_default(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    k.setDefaultValue(0.0)
    k.setValue(400.0)
    _send_key(k, Qt.Key_Return)
    assert k.value() == 0.0


def test_arrows_clamp_at_boundaries(qapp):
    k = RotaryKnob()
    k.setRange(0.0, 600.0)
    k.setValue(0.0)
    _send_key(k, Qt.Key_Down)
    assert k.value() == 0.0  # can't go below min
    k.setValue(600.0)
    _send_key(k, Qt.Key_Up)
    assert k.value() == 600.0  # can't go above max
