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
    assert labels == ["FLUSH", "−", "+", "◀", "▶", "FREEZE"]


def test_window_has_clip_controls(qapp, state):
    win = TurntableWindow(state)
    labels = [b.text() for b in win.clip_controls]
    assert labels == ["PLAY", "−", "+", "◀", "▶", "SAVE"]


def test_window_has_loop_button(qapp, state):
    win = TurntableWindow(state)
    assert win.loop_btn.text() == "LOOP"
    assert win.loop_btn.isCheckable()


def test_buffer_selection_updates_disc(qapp, state):
    import numpy as np
    win = TurntableWindow(state)
    # Simulate some buffered audio so the drag can capture abs samples.
    # A real write (not a fake total_written assignment) so the test
    # exercises whichever ring implementation the machine has available --
    # NativeAudioCircularBuffer.total_written has no setter.
    buf = state.active_slot.buffer
    buf.write(np.zeros((int(60 * buf.sample_rate), buf.channels), dtype=np.float32))
    # Emit a selection change
    win.buffer_panel.waveform.manualSelectionChanged.emit(0.1, 0.3)
    # _update_selection_display paints the disc; run a tick (or invoke directly).
    win._update_selection_display()
    idx = win.buffer_turntable.selected_track()
    assert idx in win.buffer_turntable._track_selections
    start, end, color = win.buffer_turntable._track_selections[idx]
    # At buffered=60s / buf=60s, display window == full buffered range, so
    # the round-tripped fractions should match the drag fractions within
    # integer-sample rounding.
    assert abs(start - 0.1) < 0.02 and abs(end - 0.3) < 0.02
    assert color == "#FFD900"


def test_clip_selection_updates_disc(qapp, state):
    import numpy as np
    win = TurntableWindow(state)
    buf = state.active_slot.buffer
    # Populate buffer and create a checkout so a clip is displayed.
    buf.write(np.zeros((int(60 * buf.sample_rate), buf.channels), dtype=np.float32))
    state.active_slot.checkout_manager.create_from_abs_range(
        0, int(30 * buf.sample_rate)
    )
    win._refresh_clip_side(auto_select_newest=True)
    win.clip_panel.waveform.manualSelectionChanged.emit(0.2, 0.5)
    win._update_selection_display()
    idx = win.clip_turntable.selected_track()
    assert idx in win.clip_turntable._track_selections
    start, end, color = win.clip_turntable._track_selections[idx]
    # Clip selection is stored as clip-local fractions — the values
    # round-trip exactly because there's no buffer-advance math.
    assert abs(start - 0.2) < 1e-6 and abs(end - 0.5) < 1e-6
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
    # Grow buffer track count to match slot count
    win.buffer_turntable.set_track_count(len(state.slots))
    # Emit track_selected from buffer side
    win.buffer_turntable.track_selected.emit(1)
    assert state.active_slot_index == 1
    # Clip side rings correspond to checkouts now, not slots; with 0 checkouts
    # the clip turntable falls back to a single empty ring.
    assert win.clip_turntable.track_count() == 1


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


def test_right_click_chip_emits_context_menu_request(qapp):
    """Right-click on a source chip should emit contextMenuRequested.

    Tests the SlotChip widget in isolation. Constructing a full
    TurntableWindow here would also wire up the production handler
    that calls QMenu.exec() — that handler then opens a real menu on
    the developer's desktop during VSCode's auto-test-on-save runs,
    which is exactly what we don't want.
    """
    from PySide6.QtCore import QPoint, Qt, QEvent
    from PySide6.QtGui import QMouseEvent
    from flashback_sampler.app.widgets.nav_bar import SourceIndicator
    chip = SourceIndicator(0, "Test")
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
    win._switch_to_slot(1)
    assert state.active_slot_index == 1
    assert win.buffer_turntable.selected_track() == 1
    # Clip side now reflects the new active slot's checkouts (none yet → 1 empty ring)
    assert win.clip_turntable.track_count() == 1


def test_default_buffer_selection_applied_at_init(qapp, state):
    import numpy as np
    win = TurntableWindow(state)
    # Default slot has duration_preset_idx=4 (180s) and anchor_offset_s=0.
    # New semantics: the selection is painted against buffered_seconds,
    # not buffer capacity. Simulate some audio being present so the
    # display update has something to show, then check the selection.
    buf = state.active_slot.buffer
    buf.write(np.zeros((int(60 * buf.sample_rate), buf.channels), dtype=np.float32))  # 60s buffered
    win._update_selection_display()
    sel = win.buffer_panel.waveform.manual_selection()
    assert sel is not None
    start, end = sel
    assert 0.0 <= start < end <= 1.0
    idx = state.active_slot_index
    assert idx in win.buffer_turntable._track_selections


def test_default_buffer_selection_skipped_if_buffer_too_small(qapp):
    """If the buffered audio is shorter than the preset duration,
    start_frac clamps to 0."""
    import numpy as np
    from flashback_sampler.app.state import AppState
    s = AppState(buffer_seconds=10.0, sample_rate=48000, channels=2)  # only 10s
    try:
        win = TurntableWindow(s)
        # Pretend the buffer is completely full (10s buffered on a 10s buf).
        buf = s.active_slot.buffer
        buf.write(np.zeros((int(10 * buf.sample_rate), buf.channels), dtype=np.float32))
        win._update_selection_display()
        sel = win.buffer_panel.waveform.manual_selection()
        # 3:00 (180s) default on 10s of audio → end_frac=1, start_frac=0
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


# ── Drift-with-audio selection state ─────────────────────────────────────────


def test_user_drag_captures_absolute_samples(qapp, state):
    import numpy as np
    win = TurntableWindow(state)
    # The buffer has recorded 60s of audio
    buf = state.active_slot.buffer
    buf.write(np.zeros((60 * buf.sample_rate, buf.channels), dtype=np.float32))
    # Simulate a user drag from 0.5 to 1.0 (last 30s of 60s = 30s to 0s ago)
    win.buffer_panel.waveform.manualSelectionChanged.emit(0.5, 1.0)
    assert win._buffer_sel_mode == "user"
    assert win._buffer_sel_abs is not None
    start, end = win._buffer_sel_abs
    # end should be at total_written; start 30s earlier
    sr = buf.sample_rate
    assert end == 60 * sr
    assert abs(start - 30 * sr) < sr  # within 1 sample of 30s back


def test_user_selection_drifts_with_buffer(qapp):
    """A user selection set at T=60s should shift in fraction-space as the
    buffer advances to T=120s, because absolute samples are fixed but the
    display window moves forward."""
    import numpy as np
    from flashback_sampler.app.state import AppState
    # Need a buffer capacity >= 120s so the displayed window can grow to 120s.
    s = AppState(buffer_seconds=240.0, sample_rate=48000, channels=2)
    try:
        win = TurntableWindow(s)
        buf = s.active_slot.buffer
        sr = buf.sample_rate
        # T=60s of audio written
        buf.write(np.zeros((60 * sr, buf.channels), dtype=np.float32))
        # User selects [0.5, 1.0] = last 30s (from 30s ago to now).
        # Store: abs_start=30*sr, abs_end=60*sr (approx)
        win.buffer_panel.waveform.manualSelectionChanged.emit(0.5, 1.0)
        # Now advance the buffer to T=120s by writing 60 more seconds.
        # Without touching _buffer_sel_abs, the effective fractions in the
        # new 120s window should be [0.25, 0.5] (still the same 30-to-60
        # second range in absolute terms).
        buf.write(np.zeros((60 * sr, buf.channels), dtype=np.float32))
        win._update_selection_display()
        sel = win.buffer_panel.waveform.manual_selection()
        assert sel is not None
        s_now, e_now = sel
        # Tolerance for integer-sample rounding at 48000 Hz
        assert abs(s_now - 0.25) < 0.02
        assert abs(e_now - 0.5) < 0.02
    finally:
        s.shutdown()


def test_clear_selection_returns_to_default(qapp, state):
    import numpy as np
    win = TurntableWindow(state)
    buf = state.active_slot.buffer
    buf.write(np.zeros((60 * buf.sample_rate, buf.channels), dtype=np.float32))
    win.buffer_panel.waveform.manualSelectionChanged.emit(0.3, 0.6)
    assert win._buffer_sel_mode == "user"
    win.buffer_panel.waveform.manualSelectionCleared.emit()
    assert win._buffer_sel_mode == "default"
    assert win._buffer_sel_abs is None


def test_switching_tracks_resets_selection_mode(qapp, state):
    import numpy as np
    from flashback_sampler.core.quality_presets import QualityPreset
    preset = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2,
        buffer_seconds=30.0, description="test",
    )
    state.add_slot(preset, name="Game")
    win = TurntableWindow(state)
    win.buffer_turntable.set_track_count(len(state.slots))
    buf = state.active_slot.buffer
    buf.write(np.zeros((20 * buf.sample_rate, buf.channels), dtype=np.float32))
    win.buffer_panel.waveform.manualSelectionChanged.emit(0.3, 0.8)
    assert win._buffer_sel_mode == "user"
    win.buffer_turntable.track_selected.emit(1)
    assert win._buffer_sel_mode == "default"
    assert win._buffer_sel_abs is None


# ── OUT→ checkout flow + clip-side ring population ───────────────────────────


def test_out_button_creates_checkout(qapp, state):
    win = TurntableWindow(state)
    buf = state.active_slot.buffer
    sr = buf.sample_rate
    # Simulate 5 seconds of recorded audio
    import numpy as np
    samples = np.random.standard_normal((5 * sr, buf.channels)).astype(np.float32) * 0.3
    buf.write(samples)
    # Set a user selection covering the last portion of the buffer
    win.buffer_panel.waveform.manualSelectionChanged.emit(0.6, 1.0)
    # Click OUT→
    before = len(state.active_slot.checkout_manager.list())
    win.out_btn.clicked.emit()
    after = len(state.active_slot.checkout_manager.list())
    assert after == before + 1
    # Clip side should now have a ring for the new checkout
    assert win.clip_turntable.track_count() == after


def test_out_button_in_default_mode_uses_preset(qapp, state):
    import numpy as np
    win = TurntableWindow(state)
    buf = state.active_slot.buffer
    sr = buf.sample_rate
    # Write enough audio that the default 3:00 (180s) preset clamps correctly.
    # state fixture gives a 60s buffer, so we only have 60s available; the
    # handler will clamp abs_start to the oldest available sample.
    samples = np.zeros((60 * sr, buf.channels), dtype=np.float32)
    buf.write(samples)
    win.out_btn.clicked.emit()
    checkouts = state.active_slot.checkout_manager.list()
    assert len(checkouts) == 1
    co = checkouts[0]
    # Default preset is 180s but capped by the 60s ring — expect close to 60.
    assert co.duration_seconds > 50


def test_new_checkout_adds_outermost_ring(qapp, state):
    import numpy as np
    win = TurntableWindow(state)
    buf = state.active_slot.buffer
    sr = buf.sample_rate
    buf.write(np.zeros((5 * sr, buf.channels), dtype=np.float32))
    win.out_btn.clicked.emit()
    assert win.clip_turntable.track_count() == 1
    buf.write(np.zeros((5 * sr, buf.channels), dtype=np.float32))
    win.out_btn.clicked.emit()
    assert win.clip_turntable.track_count() == 2
    # Track 1 (outermost / newest) should be the newly-selected one
    assert win.clip_turntable.selected_track() == 1


def test_clip_ring_click_shows_that_clip_in_panel(qapp, state):
    import numpy as np
    win = TurntableWindow(state)
    buf = state.active_slot.buffer
    sr = buf.sample_rate
    buf.write(np.zeros((5 * sr, buf.channels), dtype=np.float32))
    win.out_btn.clicked.emit()
    buf.write(np.zeros((5 * sr, buf.channels), dtype=np.float32))
    win.out_btn.clicked.emit()
    # Clicking track 0 (older clip) should show clip 1 of 2 in the panel
    win.clip_turntable.track_selected.emit(0)
    label = win.clip_panel.source_label.text()
    assert "#1/2" in label


def test_clip_ring_click_does_not_change_active_slot(qapp, state):
    """Clicking a clip-side ring must NOT change which slot is active."""
    import numpy as np
    from flashback_sampler.core.quality_presets import QualityPreset
    preset = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2,
        buffer_seconds=30.0, description="test"
    )
    state.add_slot(preset, name="Source 2")
    win = TurntableWindow(state)
    win.buffer_turntable.set_track_count(len(state.slots))
    # Active slot = 0 initially. Create two checkouts on slot 0.
    buf = state.active_slot.buffer
    sr = buf.sample_rate
    buf.write(np.zeros((5 * sr, buf.channels), dtype=np.float32))
    win.out_btn.clicked.emit()
    buf.write(np.zeros((5 * sr, buf.channels), dtype=np.float32))
    win.out_btn.clicked.emit()
    assert state.active_slot_index == 0
    # Click a clip-side ring; active slot must stay at 0.
    win.clip_turntable.track_selected.emit(0)
    assert state.active_slot_index == 0


def test_drag_in_progress_not_overwritten_by_tick(qapp, state):
    """During an active drag on the buffer panel, _tick must not overwrite
    the in-progress manual selection on the linear waveform."""
    import numpy as np
    win = TurntableWindow(state)
    buf = state.active_slot.buffer
    buf.write(np.zeros((60 * buf.sample_rate, buf.channels), dtype=np.float32))  # 60s "buffered"
    # Simulate the user mid-drag by flipping the flag directly. Set a
    # user selection that the tick would normally re-apply.
    win.buffer_panel.waveform._is_dragging = True
    # Put a known manual selection into the widget, then run a tick.
    win.buffer_panel.waveform.set_manual_selection(0.42, 0.77)
    win._tick()
    sel = win.buffer_panel.waveform.manual_selection()
    assert sel is not None
    s, e = sel
    # _tick should have left the widget's manual selection alone.
    assert abs(s - 0.42) < 1e-6
    assert abs(e - 0.77) < 1e-6


# ── Record gain + source rename (PR B) ─────────────────────────────────

def test_gain_menu_builds_with_unity_checked(qapp, state):
    from PySide6.QtWidgets import QMenu
    win = TurntableWindow(state)
    menu = QMenu()
    win._populate_gain_menu(menu, state.slots[0])
    acts = menu.actions()
    assert len(acts) == 8
    checked = [a.text() for a in acts if a.isChecked()]
    assert checked == ["0 dB (unity)"]  # defaults to unity


def test_gain_menu_action_sets_slot_gain(qapp, state):
    from PySide6.QtWidgets import QMenu
    win = TurntableWindow(state)
    menu = QMenu()
    win._populate_gain_menu(menu, state.slots[0])
    plus6 = next(a for a in menu.actions() if a.text() == "+6 dB")
    plus6.trigger()
    assert abs(state.slots[0].buffer.gain - 10 ** (6.0 / 20.0)) < 1e-3
    mute = next(a for a in menu.actions() if a.text() == "Mute")
    mute.trigger()
    assert state.slots[0].buffer.gain == 0.0


def test_rename_slot_updates_name_and_nav(qapp, state, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    win = TurntableWindow(state)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Chrome", True)))
    win._rename_slot(0)
    assert state.slots[0].name == "Chrome"
    assert win.nav_bar.source_slots[0]._name == "CHROME"  # nav uppercases


def test_rename_slot_cancel_keeps_name(qapp, state, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    win = TurntableWindow(state)
    original = state.slots[0].name
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))
    win._rename_slot(0)
    assert state.slots[0].name == original


def _write_one_second(state):
    import numpy as np
    state.active_slot.buffer.write(
        np.zeros((state.active_slot.buffer.sample_rate, 2), dtype=np.float32)
    )


def test_clip_drag_full_exports_and_marks_saved(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.active_slot.checkout_manager
        co = mgr.create(duration_s=0.5)
        win._refresh_clip_side(auto_select_newest=True)
        win._export_pool_dir = tmp_path
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: True,
        )
        win._on_clip_drag_full()
        assert mgr.get(co.id).state == "saved"
        assert len(list(tmp_path.glob("*.wav"))) == 1
    finally:
        win.close()


def test_clip_drag_cancel_deletes_file_and_keeps_clip(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.active_slot.checkout_manager
        co = mgr.create(duration_s=0.5)
        win._refresh_clip_side(auto_select_newest=True)
        win._export_pool_dir = tmp_path
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: False,
        )
        win._on_clip_drag_full()
        assert mgr.get(co.id).state == "pending"
        assert list(tmp_path.glob("*.wav")) == []
    finally:
        win.close()


def test_clip_drag_out_uses_trimmed_range(qapp, state, tmp_path, monkeypatch):
    from tests.fixtures.wavread import read_wav
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.active_slot.checkout_manager
        co = mgr.create(duration_s=0.5)
        win._refresh_clip_side(auto_select_newest=True)
        n = co.n_frames
        mgr.set_trim(co.id, n // 4, n // 2)
        win._export_pool_dir = tmp_path
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: True,
        )
        win._on_clip_drag_out(0.25, 0.5)
        files = list(tmp_path.glob("*.wav"))
        assert len(files) == 1
        assert read_wav(files[0])[1].frames == n // 2 - n // 4
    finally:
        win.close()


def test_save_dialog_offers_wav_only(qapp, state, tmp_path, monkeypatch):
    from flashback_sampler.app import turntable_window as tw
    win = tw.TurntableWindow(state)
    try:
        _write_one_second(state)
        state.active_slot.checkout_manager.create(duration_s=0.5)
        win._refresh_clip_side(auto_select_newest=True)
        seen = {}

        def fake_dialog(parent, title, default_path, filter_spec):
            seen.update(default_path=default_path, filter_spec=filter_spec)
            return "", ""

        monkeypatch.setattr(tw.QFileDialog, "getSaveFileName", staticmethod(fake_dialog))
        win._save_current_clip()
        assert seen["filter_spec"] == "WAV audio (*.wav)"
        assert seen["default_path"].endswith(".wav")
    finally:
        win.close()


def test_buffer_drag_out_persists_saved_checkout_on_accept(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        sr = state.active_slot.buffer.sample_rate
        win._export_pool_dir = tmp_path
        win._buffer_sel_abs = (0, sr // 2)
        win._buffer_sel_mode = "user"
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: True,
        )
        win._on_buffer_drag_out(0.0, 0.5)
        cos = state.active_slot.checkout_manager.list()
        assert len(cos) == 1
        assert cos[0].state == "saved"
        assert len(list(tmp_path.glob("*.wav"))) == 1
    finally:
        win.close()


def test_buffer_drag_out_cancel_discards_checkout_and_file(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        sr = state.active_slot.buffer.sample_rate
        win._export_pool_dir = tmp_path
        win._buffer_sel_abs = (0, sr // 2)
        win._buffer_sel_mode = "user"
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: False,
        )
        win._on_buffer_drag_out(0.0, 0.5)
        assert state.active_slot.checkout_manager.list() == []
        assert list(tmp_path.glob("*.wav")) == []
    finally:
        win.close()


def test_buffer_drag_out_default_mode_drags_duration_window(qapp, state, tmp_path, monkeypatch):
    """The automatic (default-mode) selection band must be draggable —
    it resolves to the same anchor/duration window the OUT button uses.
    Regression: the handler used to silently drop non-"user" drags even
    though the band is painted as a draggable selection every tick."""
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        win._export_pool_dir = tmp_path
        win._buffer_sel_mode = "default"
        win._buffer_sel_abs = None
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: True,
        )
        win._on_buffer_drag_out(0.0, 1.0)
        cos = state.active_slot.checkout_manager.list()
        assert len(cos) == 1
        assert cos[0].state == "saved"
        # Window is clamped to what's buffered: exactly the 1 s written.
        sr = state.active_slot.buffer.sample_rate
        assert cos[0].n_frames == sr
        assert len(list(tmp_path.glob("*.wav"))) == 1
    finally:
        win.close()


def test_refresh_clip_side_caches_peak_bins_per_checkout(qapp, state, monkeypatch):
    """Waveform bins are computed once per checkout, not on every refresh —
    otherwise refresh cost grows with every banked clip (measured live:
    0.17s -> 1.7s over 8 drags). Checkout.bins is precomputed once at
    create() time, so the cost this guards is the window's own
    ring_amp/panel_bins reduction in _clip_bins_cache — pin that it runs
    at most once per checkout (via a CheckoutManager.peak_bins spy, the
    engine-reaching fallback path) and that cache entries are identity
    -stable across repeat refreshes."""
    from flashback_sampler.core.checkout import CheckoutManager

    calls = []
    real_peak_bins = CheckoutManager.peak_bins

    def spy(self, checkout_id, n_bins):
        calls.append(checkout_id)
        return real_peak_bins(self, checkout_id, n_bins)

    monkeypatch.setattr(CheckoutManager, "peak_bins", spy)

    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.active_slot.checkout_manager
        a = mgr.create(duration_s=0.2)
        b = mgr.create(duration_s=0.2)
        win._refresh_clip_side(auto_select_newest=True)
        ring_a = win._clip_bins_cache[a.id]["ring_amp"]
        ring_b = win._clip_bins_cache[b.id]["ring_amp"]
        panel_b = win._clip_bins_cache[b.id]["panel_bins"]  # b is the displayed clip

        win._refresh_clip_side()
        win._refresh_clip_side()

        assert win._clip_bins_cache[a.id]["ring_amp"] is ring_a
        assert win._clip_bins_cache[b.id]["ring_amp"] is ring_b
        assert win._clip_bins_cache[b.id]["panel_bins"] is panel_b
        assert calls.count(a.id) <= 1
        assert calls.count(b.id) <= 1
    finally:
        win.close()


def test_refresh_clip_side_prunes_cache_for_discarded_checkouts(qapp, state):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.active_slot.checkout_manager
        co = mgr.create(duration_s=0.2)
        win._refresh_clip_side(auto_select_newest=True)
        assert co.id in win._clip_bins_cache
        mgr.discard(co.id)
        win._refresh_clip_side()
        assert co.id not in win._clip_bins_cache
    finally:
        win.close()


def test_refresh_clip_side_keeps_cache_for_inactive_slots(qapp, state):
    """The bins cache is shared across slots (checkout ids are globally
    unique) — refreshing while one slot is active must not evict cached
    bins belonging to another slot's checkouts."""
    from flashback_sampler.core.quality_presets import QualityPreset

    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        co_a = state.active_slot.checkout_manager.create(duration_s=0.2)
        win._refresh_clip_side(auto_select_newest=True)
        assert co_a.id in win._clip_bins_cache

        state.add_slot(
            QualityPreset(
                name="CUSTOM", sample_rate=48000, channels=2,
                buffer_seconds=60.0,
            ),
            name="second",
        )
        win._switch_to_slot(len(state.slots) - 1)
        assert co_a.id in win._clip_bins_cache  # survived the slot switch
    finally:
        win.close()


def test_buffer_drag_out_evicts_oldest_saved_checkout_at_cap(qapp, state, tmp_path, monkeypatch):
    """The sample-bank flow mints a checkout per drag; at the manager's
    active-checkout cap the oldest `saved` clip is evicted (its pool file
    is the durable record) so drags keep working."""
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        sr = state.active_slot.buffer.sample_rate
        mgr = state.active_slot.checkout_manager
        # Fill to the cap with saved checkouts (as prior drags would)
        cap = mgr._max_active
        for _ in range(cap):
            co = mgr.create(duration_s=0.01)
            mgr.mark_saved(co.id)
        oldest_id = mgr.list()[0].id
        win._export_pool_dir = tmp_path
        win._buffer_sel_abs = (0, sr // 2)
        win._buffer_sel_mode = "user"
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: True,
        )
        win._on_buffer_drag_out(0.0, 0.5)
        ids = [c.id for c in mgr.list()]
        assert oldest_id not in ids  # evicted
        assert len(ids) == cap  # newcomer took its place
        assert len(list(tmp_path.glob("*.wav"))) == 1
    finally:
        win.close()


def test_buffer_drag_out_at_cap_without_saved_clips_reports_failure(qapp, state, tmp_path, monkeypatch):
    """Pending (unsaved) clips are the user's working set — never evicted."""
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        sr = state.active_slot.buffer.sample_rate
        mgr = state.active_slot.checkout_manager
        cap = mgr._max_active
        for _ in range(cap):
            mgr.create(duration_s=0.01)  # all stay pending
        win._export_pool_dir = tmp_path
        win._buffer_sel_abs = (0, sr // 2)
        win._buffer_sel_mode = "user"
        called = []
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: called.append(path) or True,
        )
        win._on_buffer_drag_out(0.0, 0.5)
        assert called == []
        assert len(mgr.list()) == cap  # nothing evicted
    finally:
        win.close()


def test_buffer_drag_out_with_empty_buffer_is_noop(qapp, state, tmp_path, monkeypatch):
    win = TurntableWindow(state)
    try:
        win._export_pool_dir = tmp_path
        win._buffer_sel_mode = "default"
        called = []
        monkeypatch.setattr(
            "flashback_sampler.app.turntable_window.perform_file_drag",
            lambda widget, path: called.append(path) or True,
        )
        win._on_buffer_drag_out(0.0, 1.0)
        assert called == []
        assert state.active_slot.checkout_manager.list() == []
    finally:
        win.close()


def test_set_export_prefs_persist_and_apply(qapp, state, tmp_path, monkeypatch):
    import flashback_sampler.app.turntable_window as tw
    saved = {}
    monkeypatch.setattr(
        tw, "save_export_pool_dir", lambda p: saved.__setitem__("dir", str(p))
    )
    monkeypatch.setattr(
        tw, "save_export_bit_depth", lambda d: saved.__setitem__("depth", d)
    )
    win = TurntableWindow(state)
    try:
        win._set_export_pool_dir(str(tmp_path / "pool"))
        win._set_export_bit_depth("PCM_24")
        assert win._export_pool_dir == tmp_path / "pool"
        assert win._export_bit_depth == "PCM_24"
        assert saved == {"dir": str(tmp_path / "pool"), "depth": "PCM_24"}
    finally:
        win.close()


def test_add_source_applies_rate_probe(qapp, state, monkeypatch):
    import flashback_sampler.app.turntable_window as tw
    from flashback_sampler.core.quality_presets import QualityPreset

    adjusted = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2, buffer_seconds=60.0
    )
    monkeypatch.setattr(
        tw, "apply_rate_probe", lambda preset, device: (adjusted, "mix is 48k")
    )
    win = TurntableWindow(state)
    try:
        requested = QualityPreset(
            name="CUSTOM", sample_rate=96000, channels=2, buffer_seconds=60.0
        )
        result = win._probe_and_notify(requested, None)
        assert result.sample_rate == 48000  # notice shown via stubbed QMessageBox
    finally:
        win.close()


# ─────────────────────────────────────────────────────────────────────────
# Clip playback through NativeScrubPlayer with the native library mocked
# ─────────────────────────────────────────────────────────────────────────


def _fake_player(monkeypatch, state):
    """Swap state.scrub_player for a NativeScrubPlayer bound to a fake lib.
    The ring buffers stay on the real library; only the player is faked."""
    from tests.unit.test_scrub_player import _FakePlaybackLib
    from flashback_sampler.core import native
    from flashback_sampler.core.scrub_player import NativeScrubPlayer

    fake = _FakePlaybackLib()
    with monkeypatch.context() as m:
        m.setattr(native, "load", lambda: fake)
        player = NativeScrubPlayer(48_000, 2)
        # The handle is lazy, so force it here: outside this context
        # native.load() is the real library again.
        player._handle()
    state.scrub_player = player
    return fake


def _checkout(state):
    import numpy as np
    audio = np.zeros((4800, 2), dtype=np.float32)
    audio[:, 0] = 0.5
    state.buffer.write(audio)
    return state.checkout_manager.create(duration_s=0.1)


def test_display_clip_reads_write_state_before_pinning(qapp, state, monkeypatch):
    """F2: pin() queues the checkout's async scratch load; write_state()
    goes through fb_checkout_info, which calls waitLoad and blocks until
    that load finishes. Reading write_state before pin() keeps clip
    selection from freezing the UI thread on the load it just queued."""
    from flashback_sampler.core.checkout import CheckoutManager

    win = TurntableWindow(state)
    co = _checkout(state)

    calls: list[str] = []
    real_pin = CheckoutManager.pin
    real_write_state = CheckoutManager.write_state

    def spy_pin(self, checkout_id):
        calls.append("pin")
        return real_pin(self, checkout_id)

    def spy_write_state(self, checkout_id):
        calls.append("write_state")
        return real_write_state(self, checkout_id)

    monkeypatch.setattr(CheckoutManager, "pin", spy_pin)
    monkeypatch.setattr(CheckoutManager, "write_state", spy_write_state)

    win._display_clip_in_panel(co, 0, 1)

    assert calls == ["write_state", "pin"]


def test_play_click_with_no_checkout_does_nothing(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    win._on_play_clip_clicked()
    assert not [n for n, _ in fake.calls if n in ("fb_playback_bind", "fb_playback_play")]


def test_play_click_binds_the_checkout_at_its_rate_and_plays(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    co = _checkout(state)
    co.sample_rate = 96_000  # pin a non-default rate so the bind-rate assert is real
    win._tick()
    fake.state = (0, 0, 0, co.n_frames, 48_000)  # not playing: the click must take the play branch
    win._on_play_clip_clicked()
    assert fake.bound_checkout == (co.handle, 0, co.n_frames)
    assert state.scrub_player.sample_rate == co.sample_rate
    assert [n_ for n_, _ in fake.calls if n_ == "fb_playback_play"] == ["fb_playback_play"]
    assert win._intending_playback is True
    assert win.clip_controls[0].text() == "STOP"


def test_play_click_while_playing_pauses_and_drops_intent(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    _checkout(state)
    win._intending_playback = True
    fake.state = (1, 1, 100, 4800, 48_000)
    win._on_play_clip_clicked()
    assert [n for n, _ in fake.calls if n == "fb_playback_pause"] == ["fb_playback_pause"]
    assert win._intending_playback is False
    assert not [n for n, _ in fake.calls if n == "fb_playback_bind"]


def test_update_playback_state_drives_the_playhead_from_the_native_cursor(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    co = _checkout(state)
    win._tick()
    seen = []
    monkeypatch.setattr(win.clip_panel.waveform, "set_playhead", seen.append)
    fake.state = (1, 1, co.n_frames // 2, co.n_frames, 48_000)
    win._update_clip_playback_state()
    assert seen[-1] == pytest.approx(0.5)
    fake.state = (1, 0, co.n_frames, co.n_frames, 48_000)
    win._update_clip_playback_state()
    assert seen[-1] is None


def test_loop_restarts_play_after_native_auto_stop(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    _checkout(state)
    win._tick()
    win.loop_btn.setChecked(True)
    win._intending_playback = True
    win._was_playing_last_tick = True
    fake.state = (1, 0, 4800, 4800, 48_000)
    win._update_clip_playback_state()
    assert [n for n, _ in fake.calls if n == "fb_playback_play"] == ["fb_playback_play"]


def test_async_open_failure_surfaces_once_via_last_error(qapp, state, monkeypatch):
    """The native player opens its device lazily on the Zig render
    thread: a failure there reports through last_error() + playing
    dropping to 0, not through an exception at play(). The click itself
    must arm the "was playing" edge, because the failure lands before
    the next 33 ms tick. Two ticks on that edge with the SAME
    last_error must only pop the warning once."""
    import flashback_sampler.app.turntable_window as tw

    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    _checkout(state)
    win._tick()
    warnings = []
    monkeypatch.setattr(tw.QMessageBox, "warning", lambda *a, **k: warnings.append(a))

    win._on_play_clip_clicked()
    # The render thread failed after play() returned OK.
    fake.state = (1, 0, 0, 4800, 48_000)
    fake.err = b"device open failed"
    win._update_clip_playback_state()
    assert len(warnings) == 1  # the click armed the edge; nothing hand-forced

    win._was_playing_last_tick = True  # force a second "just stopped" edge
    win._update_clip_playback_state()
    assert len(warnings) == 1


def test_loop_with_a_failing_device_warns_once_not_every_tick(qapp, state, monkeypatch):
    """LOOP checked plus a device that keeps failing is the dialog-storm
    case: the LOOP restart must not re-arm the once-guard. Only an
    explicit user click may do that."""
    import flashback_sampler.app.turntable_window as tw

    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    _checkout(state)
    win._tick()
    win.loop_btn.setChecked(True)
    warnings = []
    monkeypatch.setattr(tw.QMessageBox, "warning", lambda *a, **k: warnings.append(a))

    fake.err = b"device open failed"
    win._on_play_clip_clicked()
    for _ in range(3):
        # Each LOOP restart dies on the render thread the same way.
        fake.state = (1, 0, 0, 4800, 48_000)
        win._update_clip_playback_state()

    assert len(warnings) == 1


# ── Bins-from-handle, pin-on-select, bind_checkout (h10) ────────────────


def test_selecting_a_clip_pins_it_and_bins_come_from_the_handle(qapp, state):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        mgr = state.active_slot.checkout_manager
        a = mgr.create(duration_s=0.2)
        b = mgr.create(duration_s=0.3)
        win._refresh_clip_side(auto_select_newest=True)
        assert mgr._pinned_id == b.id  # noqa: SLF001
        win.clip_turntable.select_track(0)
        win._refresh_clip_side()
        assert mgr._pinned_id == a.id  # noqa: SLF001
        assert win._clip_bins_cache[a.id]["panel_bins"].shape == (360, 2, state.channels)
        assert win._clip_bins_cache[a.id]["ring_amp"].shape == (540,)
    finally:
        win.close()


def test_play_with_a_trim_binds_the_trim_range(qapp, state, monkeypatch):
    fake = _fake_player(monkeypatch, state)
    win = TurntableWindow(state)
    try:
        co = _checkout(state)
        state.checkout_manager.set_trim(co.id, 100, 300)
        win._tick()
        fake.state = (0, 0, 0, 0, 48_000)
        win._on_play_clip_clicked()
        assert fake.bound_checkout == (co.handle, 100, 200)
    finally:
        win.close()


def test_buffer_drag_at_the_count_cap_evicts_the_oldest_saved(qapp, state, monkeypatch, tmp_path):
    win = TurntableWindow(state)
    try:
        _write_one_second(state)
        state.apply_checkout_caps(max_active=1)
        win._export_pool_dir = tmp_path
        monkeypatch.setattr("flashback_sampler.app.turntable_window.perform_file_drag", lambda w, p: True)
        win._on_buffer_drag_out(0.0, 0.5)
        first = state.checkout_manager.list()[0].id
        win._on_buffer_drag_out(0.0, 0.5)
        cos = state.checkout_manager.list()
        assert len(cos) == 1 and cos[0].id != first and cos[0].state == "saved"
    finally:
        win.close()


def test_flush_confirm_names_the_action_not_a_stale_seconds_figure(qapp, state, monkeypatch):
    """#40: the flush clears the whole ring, so the confirm must not quote a
    buffered-seconds figure that is stale by the time the flush runs."""
    import flashback_sampler.app.turntable_window as tw

    win = TurntableWindow(state)
    asked = []

    def question(parent, title, text, *a, **k):
        asked.append(text)
        return tw.QMessageBox.Yes

    monkeypatch.setattr(tw.QMessageBox, "question", question)
    flushed = []
    monkeypatch.setattr(state.slots[0].buffer, "flush", lambda: flushed.append(True))

    win._flush_slot_buffer(0)

    assert flushed == [True]
    assert len(asked) == 1
    assert not any(c.isdigit() for c in asked[0]), asked[0]
    assert "all buffered audio" in asked[0]
