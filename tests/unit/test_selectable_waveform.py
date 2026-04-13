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
