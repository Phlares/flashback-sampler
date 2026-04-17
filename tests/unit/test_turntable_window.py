"""Tests for TurntableWindow layout assembly."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.turntable_window import TurntableWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_window_instantiates(qapp):
    win = TurntableWindow()
    assert win is not None


def test_window_has_turntables(qapp):
    win = TurntableWindow()
    assert win.buffer_turntable.side() == "buffer"
    assert win.clip_turntable.side() == "clip"


def test_window_has_center_bridge(qapp):
    win = TurntableWindow()
    assert win.center_bridge is not None


def test_window_has_waveform_panels(qapp):
    win = TurntableWindow()
    assert win.buffer_panel is not None
    assert win.clip_panel is not None


def test_window_has_out_button(qapp):
    win = TurntableWindow()
    assert win.out_btn.text() == "OUT →"


def test_window_has_nav_bar(qapp):
    win = TurntableWindow()
    assert win.nav_bar is not None


def test_window_has_buffer_controls(qapp):
    win = TurntableWindow()
    labels = [b.text() for b in win.buffer_controls]
    assert labels == ["FLUSH", "−", "+", "◀", "▶", "PAUSE"]


def test_window_has_clip_controls(qapp):
    win = TurntableWindow()
    labels = [b.text() for b in win.clip_controls]
    assert labels == ["PLAY", "−", "+", "◀", "▶", "SAVE"]


def test_window_has_loop_button(qapp):
    win = TurntableWindow()
    assert win.loop_btn.text() == "LOOP"
    assert win.loop_btn.isCheckable()


def test_buffer_selection_updates_disc(qapp):
    win = TurntableWindow()
    # Emit a selection change
    win.buffer_panel.waveform.manualSelectionChanged.emit(0.1, 0.3)
    idx = win.buffer_turntable.selected_track()
    assert idx in win.buffer_turntable._track_selections
    start, end, color = win.buffer_turntable._track_selections[idx]
    assert abs(start - 0.1) < 1e-6 and abs(end - 0.3) < 1e-6
    assert color == "#FFD900"


def test_clip_selection_updates_disc(qapp):
    win = TurntableWindow()
    win.clip_panel.waveform.manualSelectionChanged.emit(0.2, 0.5)
    idx = win.clip_turntable.selected_track()
    assert idx in win.clip_turntable._track_selections
    _, _, color = win.clip_turntable._track_selections[idx]
    assert color == "#FF9500"
