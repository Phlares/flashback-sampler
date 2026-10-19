"""Tests for CenterBridge widget."""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout

from flashback_sampler.app.widgets.center_bridge import CenterBridge


def test_center_bridge_instantiates(qapp):
    bridge = CenterBridge()
    assert bridge is not None


def test_center_bridge_has_stop_and_start(qapp):
    bridge = CenterBridge()
    assert bridge.stop_btn.text() == "STOP"
    assert bridge.start_btn.text() == "START"


def test_center_bridge_fixed_width(qapp):
    bridge = CenterBridge()
    assert bridge.maximumWidth() <= 80


def test_buttons_sit_in_lower_portion(qapp):
    """Top stretch should be heavier than bottom stretch so buttons sit low."""
    bridge = CenterBridge()
    layout = bridge.layout()
    assert isinstance(layout, QVBoxLayout)
    # Layout items: [top_stretch, stop_btn, start_btn, bottom_stretch]
    assert layout.count() == 4
    top_item = layout.itemAt(0)
    bottom_item = layout.itemAt(layout.count() - 1)
    assert top_item is not None and top_item.spacerItem() is not None
    assert bottom_item is not None and bottom_item.spacerItem() is not None
    # Compare via the layout's stretch factors stored on the items
    assert layout.stretch(0) > layout.stretch(layout.count() - 1)
