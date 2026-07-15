"""
Unit tests for SelectableWaveform's edge-detection + edge-drag math.
Focuses on the _edge_at helper and the direct manipulation of the
selection state — mouse event plumbing is covered by the main-window
smoke test.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.widgets.selectable_waveform import (
    EDGE_GRAB_PX,
    SelectableWaveform,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ─────────────────────────────────────────────────────────────────────────
# _edge_at
# ─────────────────────────────────────────────────────────────────────────


def _new_wave(qapp, width: int = 1012) -> SelectableWaveform:
    w = SelectableWaveform()
    w.resize(width, 200)
    return w


def test_edge_at_returns_none_with_no_selection(qapp):
    w = _new_wave(qapp)
    assert w._edge_at(100) is None
    assert w._edge_at(500) is None


def test_edge_at_detects_start_edge(qapp):
    w = _new_wave(qapp, width=1012)
    # Place a selection at frac 0.25 .. 0.75
    w.set_manual_selection(0.25, 0.75)
    inner_x, inner_w = w._inner_bounds()
    assert inner_w == 1000  # 1012 - 6 - 6
    start_x = inner_x + 0.25 * inner_w  # 256
    end_x = inner_x + 0.75 * inner_w  # 756

    # Exactly on the start edge
    assert w._edge_at(start_x) == "start"
    # Within grab distance of start
    assert w._edge_at(start_x + EDGE_GRAB_PX) == "start"
    assert w._edge_at(start_x - EDGE_GRAB_PX) == "start"
    # Just outside grab distance
    assert w._edge_at(start_x + EDGE_GRAB_PX + 1) is None


def test_edge_at_detects_end_edge(qapp):
    w = _new_wave(qapp)
    w.set_manual_selection(0.25, 0.75)
    inner_x, inner_w = w._inner_bounds()
    end_x = inner_x + 0.75 * inner_w

    assert w._edge_at(end_x) == "end"
    assert w._edge_at(end_x - EDGE_GRAB_PX) == "end"
    assert w._edge_at(end_x + EDGE_GRAB_PX) == "end"


def test_edge_at_prefers_closer_edge_when_both_in_range(qapp):
    """Two edges within 2 px of each other; whichever is nearer wins."""
    w = _new_wave(qapp, width=1012)
    # Make a tiny selection so start and end are close
    inner_x, inner_w = w._inner_bounds()
    # 0.5 .. 0.505 with inner_w=1000 → start_x=506, end_x=511
    w.set_manual_selection(0.5, 0.505)
    start_x = inner_x + 0.5 * inner_w  # 506
    end_x = inner_x + 0.505 * inner_w  # 511

    # Exactly at start → start
    assert w._edge_at(start_x) == "start"
    # Exactly at end → end
    assert w._edge_at(end_x) == "end"
    # Midpoint favours whichever is nearer (tied → start wins)
    assert w._edge_at((start_x + end_x) / 2) == "start"


def test_edge_at_ignores_click_far_from_edges(qapp):
    w = _new_wave(qapp)
    w.set_manual_selection(0.25, 0.75)
    inner_x, inner_w = w._inner_bounds()
    middle_x = inner_x + 0.5 * inner_w  # middle of the band
    assert w._edge_at(middle_x) is None


# ─────────────────────────────────────────────────────────────────────────
# Edge drag math — verify the clamp to [0, other_edge - epsilon]
# ─────────────────────────────────────────────────────────────────────────


def test_drag_start_edge_leftward_extends_selection(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.5, 0.75)
    w._dragging_edge = "start"

    # Directly manipulate as mouseMoveEvent would
    new_frac = 0.25
    inner_x, inner_w = w._inner_bounds()
    epsilon = 1.0 / max(1, inner_w)
    w._manual_start = max(0.0, min(float(w._manual_end) - epsilon, new_frac))
    assert w._manual_start == pytest.approx(0.25)
    assert w._manual_end == pytest.approx(0.75)


def test_drag_start_edge_cannot_cross_end(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.3, 0.7)
    w._dragging_edge = "start"

    new_frac = 0.9  # past end
    inner_x, inner_w = w._inner_bounds()
    epsilon = 1.0 / max(1, inner_w)
    w._manual_start = max(0.0, min(float(w._manual_end) - epsilon, new_frac))

    # Should be clamped to just before end
    assert w._manual_start == pytest.approx(0.7 - epsilon)
    assert w._manual_start < w._manual_end


def test_drag_end_edge_rightward_extends_selection(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.2, 0.5)
    w._dragging_edge = "end"

    new_frac = 0.8
    inner_x, inner_w = w._inner_bounds()
    epsilon = 1.0 / max(1, inner_w)
    w._manual_end = min(1.0, max(float(w._manual_start) + epsilon, new_frac))
    assert w._manual_end == pytest.approx(0.8)
    assert w._manual_start == pytest.approx(0.2)


def test_drag_end_edge_cannot_cross_start(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.3, 0.7)
    w._dragging_edge = "end"

    new_frac = 0.1  # past start
    inner_x, inner_w = w._inner_bounds()
    epsilon = 1.0 / max(1, inner_w)
    w._manual_end = min(1.0, max(float(w._manual_start) + epsilon, new_frac))

    assert w._manual_end == pytest.approx(0.3 + epsilon)
    assert w._manual_start < w._manual_end


# ─────────────────────────────────────────────────────────────────────────
# Drag-out gesture
# ─────────────────────────────────────────────────────────────────────────

from PySide6.QtCore import QEvent, QPointF
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication as _QApp


def _mouse_ev(kind, x, y=100.0, button=Qt.LeftButton,
              buttons=Qt.LeftButton, mods=Qt.NoModifier):
    return QMouseEvent(
        kind, QPointF(x, y), QPointF(x, y), button, buttons, mods
    )


def _press(w, x, mods=Qt.NoModifier):
    w.mousePressEvent(_mouse_ev(QEvent.MouseButtonPress, x, mods=mods))


def _move(w, x, mods=Qt.NoModifier):
    w.mouseMoveEvent(_mouse_ev(
        QEvent.MouseMove, x, button=Qt.NoButton, mods=mods
    ))


def _release(w, x):
    w.mouseReleaseEvent(_mouse_ev(
        QEvent.MouseButtonRelease, x, buttons=Qt.NoButton
    ))


def test_press_inside_selection_and_drag_emits_drag_out(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.25, 0.75)
    got = []
    w.dragOutRequested.connect(lambda s, e: got.append((s, e)))
    _press(w, 500)  # mid-selection, far from both edges
    _move(w, 500 + _QApp.startDragDistance() + 1)
    assert got == [(0.25, 0.75)]
    # selection untouched — the gesture must not repaint the band
    assert w.manual_selection() == (0.25, 0.75)


def test_press_inside_selection_without_move_is_a_noop_click(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.25, 0.75)
    got = []
    w.dragOutRequested.connect(lambda s, e: got.append((s, e)))
    _press(w, 500)
    _release(w, 500)
    assert got == []
    assert w.manual_selection() == (0.25, 0.75)


def test_drag_out_is_one_shot(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.25, 0.75)
    got = []
    w.dragOutRequested.connect(lambda s, e: got.append((s, e)))
    _press(w, 500)
    far = 500 + _QApp.startDragDistance() + 1
    _move(w, far)
    _move(w, far + 50)
    assert len(got) == 1


def test_ctrl_press_and_drag_emits_full_clip(qapp):
    w = _new_wave(qapp, width=1012)
    got = []
    w.dragFullClipRequested.connect(lambda: got.append(True))
    _press(w, 300, mods=Qt.ControlModifier)
    _move(w, 300 + _QApp.startDragDistance() + 1, mods=Qt.ControlModifier)
    assert got == [True]


def test_press_outside_selection_still_paints_new_selection(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.6, 0.8)
    _press(w, 100)  # well outside, not near an edge
    _move(w, 200)
    _release(w, 200)
    sel = w.manual_selection()
    assert sel is not None
    assert sel[0] < 0.25 and sel[1] < 0.25


def test_edge_grab_still_beats_drag_out(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.25, 0.75)
    inner_x, inner_w = w._inner_bounds()
    got = []
    w.dragOutRequested.connect(lambda s, e: got.append((s, e)))
    _press(w, inner_x + 0.25 * inner_w)  # exactly on the start edge
    assert w._dragging_edge == "start"
    assert got == []


def test_is_user_interacting_while_drag_out_armed(qapp):
    w = _new_wave(qapp, width=1012)
    w.set_manual_selection(0.25, 0.75)
    _press(w, 500)
    assert w.is_user_interacting() is True
    _release(w, 500)
    assert w.is_user_interacting() is False
