"""Tests for TurntableWidget."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.widgets.turntable_widget import TurntableWidget


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_buffer_turntable_instantiates(qapp):
    tt = TurntableWidget(side="buffer")
    assert tt is not None
    assert tt.side() == "buffer"


def test_clip_turntable_instantiates(qapp):
    tt = TurntableWidget(side="clip")
    assert tt.side() == "clip"


def test_default_track_count(qapp):
    tt = TurntableWidget(side="buffer")
    assert tt.track_count() == 3


def test_set_track_count(qapp):
    tt = TurntableWidget(side="buffer")
    tt.set_track_count(5)
    assert tt.track_count() == 5


def test_selected_track_default(qapp):
    tt = TurntableWidget(side="buffer")
    assert tt.selected_track() == 0


def test_select_track(qapp):
    tt = TurntableWidget(side="buffer")
    tt.select_track(2)
    assert tt.selected_track() == 2


def test_select_track_clamps(qapp):
    tt = TurntableWidget(side="buffer")
    tt.select_track(99)
    assert tt.selected_track() == 2  # clamped to max (track_count - 1)


def test_header_position_buffer(qapp):
    tt = TurntableWidget(side="buffer")
    assert tt.header_angle_deg() == 0  # 3 o'clock = 0° in Qt arc convention


def test_header_position_clip(qapp):
    tt = TurntableWidget(side="clip")
    assert tt.header_angle_deg() == 180  # 9 o'clock
