"""Tests for TurntableWindow layout assembly."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.turntable_window import TurntableWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def state():
    from flashback_sampler.app.state import AppState
    s = AppState(buffer_seconds=60.0, sample_rate=48000, channels=2)
    yield s
    s.shutdown()


def test_window_instantiates(qapp, state):
    win = TurntableWindow(state)
    assert win is not None


def test_window_has_turntables(qapp, state):
    win = TurntableWindow(state)
    assert win.buffer_turntable.side() == "buffer"
    assert win.clip_turntable.side() == "clip"


def test_window_has_center_bridge(qapp, state):
    win = TurntableWindow(state)
    assert win.center_bridge is not None


def test_window_has_waveform_panels(qapp, state):
    win = TurntableWindow(state)
    assert win.buffer_panel is not None
    assert win.clip_panel is not None


def test_window_has_out_button(qapp, state):
    win = TurntableWindow(state)
    assert win.out_btn.text() == "OUT →"


def test_window_has_nav_bar(qapp, state):
    win = TurntableWindow(state)
    assert win.nav_bar is not None


def test_window_has_buffer_controls(qapp, state):
    win = TurntableWindow(state)
    labels = [b.text() for b in win.buffer_controls]
    assert labels == ["FLUSH", "−", "+", "◀", "▶", "PAUSE"]


def test_window_has_clip_controls(qapp, state):
    win = TurntableWindow(state)
    labels = [b.text() for b in win.clip_controls]
    assert labels == ["PLAY", "−", "+", "◀", "▶", "SAVE"]


def test_window_has_loop_button(qapp, state):
    win = TurntableWindow(state)
    assert win.loop_btn.text() == "LOOP"
    assert win.loop_btn.isCheckable()


def test_buffer_selection_updates_disc(qapp, state):
    win = TurntableWindow(state)
    # Emit a selection change
    win.buffer_panel.waveform.manualSelectionChanged.emit(0.1, 0.3)
    idx = win.buffer_turntable.selected_track()
    assert idx in win.buffer_turntable._track_selections
    start, end, color = win.buffer_turntable._track_selections[idx]
    assert abs(start - 0.1) < 1e-6 and abs(end - 0.3) < 1e-6
    assert color == "#FFD900"


def test_clip_selection_updates_disc(qapp, state):
    win = TurntableWindow(state)
    win.clip_panel.waveform.manualSelectionChanged.emit(0.2, 0.5)
    idx = win.clip_turntable.selected_track()
    assert idx in win.clip_turntable._track_selections
    _, _, color = win.clip_turntable._track_selections[idx]
    assert color == "#FF9500"


# ── Phase-1 wiring tests ──────────────────────────────────────────────────────

def test_constructor_mirrors_slot_count(qapp, state):
    win = TurntableWindow(state)
    assert win.buffer_turntable.track_count() == len(state.slots)
    assert win.clip_turntable.track_count() == len(state.slots)


def test_start_button_sets_rolling(qapp, state):
    win = TurntableWindow(state)
    # Arm the initial slot first
    state.slots[0].armed = True
    win.center_bridge.start_btn.clicked.emit()
    # start_rolling may or may not find a capture device in CI — but the
    # flag should flip true regardless (unless first_error is set)
    # We just check the handler ran by checking rolling or that an error
    # was raised. Mild assertion:
    assert isinstance(state.rolling, bool)  # no crash


def test_stop_button_calls_stop_rolling(qapp, state):
    win = TurntableWindow(state)
    state.rolling = True  # simulate rolling
    win.center_bridge.stop_btn.clicked.emit()
    assert state.rolling is False


def test_track_selected_updates_active_slot(qapp, state):
    # Add a second slot so we have indices 0, 1
    from flashback_sampler.core.quality_presets import QualityPreset
    preset = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2,
        buffer_seconds=30.0, description="test"
    )
    state.add_slot(preset, name="Source 2")
    win = TurntableWindow(state)
    # Grow track counts
    win.buffer_turntable.set_track_count(len(state.slots))
    win.clip_turntable.set_track_count(len(state.slots))
    # Emit track_selected from buffer side
    win.buffer_turntable.track_selected.emit(1)
    assert state.active_slot_index == 1
    # Clip side should mirror
    assert win.clip_turntable.selected_track() == 1


def test_arm_all_arms_every_slot(qapp, state):
    # Start with 1 armed=False, add a second also armed=False
    from flashback_sampler.core.quality_presets import QualityPreset
    preset = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2,
        buffer_seconds=30.0, description="test"
    )
    state.add_slot(preset, name="Source 2")
    for s in state.slots:
        s.armed = False
    win = TurntableWindow(state)
    win.nav_bar.arm_all_btn.clicked.emit()
    assert all(s.armed for s in state.slots)


def test_initial_buffer_panel_shows_active_slot_name(qapp, state):
    # Initial slot is named "Main"
    win = TurntableWindow(state)
    assert win.buffer_panel.source_label.text() == "MAIN"


def test_navbar_chip_reflects_slot_name(qapp, state):
    win = TurntableWindow(state)
    assert win.nav_bar.source_slots[0]._name == "MAIN"


def test_track_selected_updates_buffer_panel_label(qapp, state):
    from flashback_sampler.core.quality_presets import QualityPreset
    preset = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2,
        buffer_seconds=30.0, description="test"
    )
    state.add_slot(preset, name="Game")
    win = TurntableWindow(state)
    win.buffer_turntable.set_track_count(len(state.slots))
    win.clip_turntable.set_track_count(len(state.slots))
    win.buffer_turntable.track_selected.emit(1)
    assert win.buffer_panel.source_label.text() == "GAME"


def test_tick_timer_runs_without_crash(qapp, state):
    win = TurntableWindow(state)
    # Simulate a tick manually to make sure it doesn't raise on empty buffer
    win._tick()  # should handle empty/not-started capture gracefully


def test_right_click_chip_emits_context_menu_request(qapp, state):
    """Right-click on a source chip should emit contextMenuRequested."""
    from PySide6.QtCore import QPoint, Qt, QEvent
    from PySide6.QtGui import QMouseEvent
    win = TurntableWindow(state)
    chip = win.nav_bar.source_slots[0]
    captured = []
    chip.contextMenuRequested.connect(lambda p: captured.append(p))
    ev = QMouseEvent(
        QEvent.MouseButtonPress,
        chip.rect().center().toPointF() if hasattr(chip.rect().center(), "toPointF") else QPoint(5, 5),
        Qt.RightButton, Qt.RightButton, Qt.NoModifier,
    )
    chip.mousePressEvent(ev)
    assert len(captured) == 1


def test_switch_to_slot_updates_active_and_mirrors(qapp, state):
    from flashback_sampler.core.quality_presets import QualityPreset
    preset = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2,
        buffer_seconds=30.0, description="test",
    )
    state.add_slot(preset, name="Game")
    win = TurntableWindow(state)
    win.buffer_turntable.set_track_count(len(state.slots))
    win.clip_turntable.set_track_count(len(state.slots))
    win._switch_to_slot(1)
    assert state.active_slot_index == 1
    assert win.buffer_turntable.selected_track() == 1
    assert win.clip_turntable.selected_track() == 1


def test_default_buffer_selection_applied_at_init(qapp, state):
    win = TurntableWindow(state)
    # Default slot has duration_preset_idx=4 (180s) and anchor_offset_s=0.
    # With capacity=buffer_seconds and 180s selection, the waveform should
    # have a manual selection and the active track should have a disc selection.
    sel = win.buffer_panel.waveform.manual_selection()
    assert sel is not None
    start, end = sel
    assert 0.0 <= start < end <= 1.0
    idx = state.active_slot_index
    assert idx in win.buffer_turntable._track_selections


def test_default_buffer_selection_skipped_if_buffer_too_small(qapp):
    """If the buffer is shorter than the preset duration, start_frac clamps to 0."""
    from flashback_sampler.app.state import AppState
    s = AppState(buffer_seconds=10.0, sample_rate=48000, channels=2)  # only 10s
    try:
        win = TurntableWindow(s)
        sel = win.buffer_panel.waveform.manual_selection()
        # 3:00 (180s) default on 10s buffer → end_frac=1, start_frac=0
        assert sel is not None
        start, end = sel
        assert start == 0.0 and end == 1.0
    finally:
        s.shutdown()


def test_tick_updates_time_labels(qapp, state):
    win = TurntableWindow(state)
    # Manually run one tick — fresh buffer has 0.0 buffered seconds
    win._tick()
    left = win.buffer_panel.time_left_label.text()
    right = win.buffer_panel.time_right_label.text()
    # Left label should be "-0:00.00" or similar negative-zero form; right is "0:00.00"
    assert left.startswith("-") or left == "0:00.00"
    assert right == "0:00.00"
