"""Tests for TurntableWidget."""
from __future__ import annotations

import numpy as np
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


def test_needle_head_centered_below_selected_rim_header_buffer(qapp):
    tt = TurntableWidget(side="buffer")
    tt.resize(300, 300)
    tt.select_track(1)
    g = tt.geometry()
    header_cx, header_cy = g.rim_header_center("buffer", 1)
    needle = g.needle_head_rect("buffer", 1)
    # Needle centered on header's x
    assert abs(needle.center().x() - header_cx) < 0.5
    # Needle sits below header (higher y)
    assert needle.top() > header_cy


def test_needle_head_centered_below_selected_rim_header_clip(qapp):
    tt = TurntableWidget(side="clip")
    tt.resize(300, 300)
    tt.select_track(2)
    g = tt.geometry()
    header_cx, header_cy = g.rim_header_center("clip", 2)
    needle = g.needle_head_rect("clip", 2)
    assert abs(needle.center().x() - header_cx) < 0.5
    assert needle.top() > header_cy


def test_arm_target_is_bottom_inner_corner_buffer(qapp):
    tt = TurntableWidget(side="buffer")
    tt.resize(400, 300)
    g = tt.geometry()
    x, y = g.arm_target("buffer", 400.0, 300.0)
    # Bottom-right-ish (buffer arm points right-inward toward OUT→ column)
    assert abs(x - 398) < 5    # near right edge
    assert abs(y - 298) < 5    # near bottom edge


def test_arm_target_is_bottom_inner_corner_clip(qapp):
    tt = TurntableWidget(side="clip")
    tt.resize(400, 300)
    g = tt.geometry()
    x, y = g.arm_target("clip", 400.0, 300.0)
    # Bottom-left-ish (clip arm points left-inward toward OUT→ column)
    assert abs(x - 2) < 5
    assert abs(y - 298) < 5


def test_paint_event_no_exceptions(qapp):
    tt = TurntableWidget(side="buffer")
    tt.resize(300, 300)
    tt.repaint()  # must not raise
    tt2 = TurntableWidget(side="clip")
    tt2.resize(300, 300)
    tt2.repaint()


def test_set_track_waveform_stores_data(qapp):
    tt = TurntableWidget(side="buffer")
    tt.resize(300, 300)
    samples = np.zeros(100, dtype=np.float32)
    tt.set_track_waveform(0, samples)
    assert 0 in tt._track_waveforms
    tt.repaint()  # must not raise


def test_set_track_waveform_stores_tuple(qapp):
    tt = TurntableWidget(side="buffer")
    samples = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    tt.set_track_waveform(0, samples, fill_fraction=0.5)
    stored = tt._track_waveforms[0]
    assert isinstance(stored, tuple)
    stored_samples, stored_ff = stored
    assert np.allclose(stored_samples, samples)
    assert stored_ff == 0.5


def test_set_track_waveform_clamps_fill_fraction(qapp):
    tt = TurntableWidget(side="buffer")
    tt.set_track_waveform(0, np.zeros(10, dtype=np.float32), fill_fraction=1.5)
    assert tt._track_waveforms[0][1] == 1.0
    tt.set_track_waveform(0, np.zeros(10, dtype=np.float32), fill_fraction=-0.2)
    assert tt._track_waveforms[0][1] == 0.0


def test_paint_with_partial_fill_no_exceptions(qapp):
    tt = TurntableWidget(side="buffer")
    tt.resize(300, 300)
    tt.set_track_waveform(0, np.array([0.5, 0.7, 0.3], dtype=np.float32), fill_fraction=0.25)
    tt.repaint()


def test_set_track_selection_stores_range(qapp):
    tt = TurntableWidget(side="buffer")
    tt.set_track_selection(1, 0.2, 0.4, "#FFD900")
    assert 1 in tt._track_selections
    start, end, color = tt._track_selections[1]
    assert start == 0.2 and end == 0.4 and color == "#FFD900"


def test_set_track_selection_clears_on_none(qapp):
    tt = TurntableWidget(side="buffer")
    tt.set_track_selection(1, 0.2, 0.4, "#FFD900")
    tt.set_track_selection(1, None, None, "#FFD900")
    assert 1 not in tt._track_selections


def test_set_track_selection_clears_on_zero_span(qapp):
    tt = TurntableWidget(side="buffer")
    tt.set_track_selection(1, 0.5, 0.5, "#FFD900")
    assert 1 not in tt._track_selections


def test_paint_with_selection_no_exceptions(qapp):
    tt = TurntableWidget(side="buffer")
    tt.resize(300, 300)
    tt.set_track_selection(0, 0.1, 0.4, "#FFD900")
    tt.repaint()  # must not raise


def test_selection_arc_angle_mapping_newest_at_play(qapp):
    """A selection of [0.7, 1.0] for buffer side should sweep from
    play_angle clockwise by 108°. Verify by setting selection + checking
    no exception + checking internal state matches fractions we passed."""
    tt = TurntableWidget(side="buffer")
    tt.resize(300, 300)
    tt.set_track_selection(0, 0.7, 1.0, "#FFD900")
    # Selection is stored as fractions — paintEvent does the angle conversion.
    stored = tt._track_selections[0]
    start, end, color = stored
    assert start == 0.7 and end == 1.0
    tt.repaint()  # must not raise
