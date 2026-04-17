"""Tests for TurntableWidget."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, Qt, QEvent
from PySide6.QtGui import QMouseEvent
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


def test_anchor_is_upper_inner_corner_buffer(qapp):
    tt = TurntableWidget(side="buffer")
    tt.resize(300, 300)
    g = tt.geometry()
    assert abs(g.anchor_x - (g.cx + g.disc_r)) < 0.5
    assert abs(g.anchor_y - (g.cy - g.disc_r)) < 0.5


def test_anchor_is_upper_inner_corner_clip(qapp):
    tt = TurntableWidget(side="clip")
    tt.resize(300, 300)
    g = tt.geometry()
    assert abs(g.anchor_x - (g.cx - g.disc_r)) < 0.5
    assert abs(g.anchor_y - (g.cy - g.disc_r)) < 0.5


def test_chip_click_selects_track(qapp):
    tt = TurntableWidget(side="buffer")
    tt.resize(300, 300)
    g = tt.geometry()
    target_idx = 1
    chip_cx, chip_cy = g.chip_center("buffer", target_idx, tt.track_count())
    pos = QPointF(chip_cx, chip_cy)
    ev = QMouseEvent(QEvent.MouseButtonPress, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    tt.mousePressEvent(ev)
    assert tt.selected_track() == target_idx


def test_paint_event_no_exceptions(qapp):
    tt = TurntableWidget(side="buffer")
    tt.resize(300, 300)
    tt.repaint()  # must not raise
    tt2 = TurntableWidget(side="clip")
    tt2.resize(300, 300)
    tt2.repaint()
