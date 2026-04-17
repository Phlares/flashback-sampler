"""Tests for CenterBridge widget."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.widgets.center_bridge import CenterBridge


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


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
