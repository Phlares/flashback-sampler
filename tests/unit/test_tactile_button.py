"""
Sanity tests for TactileButton. Focuses on non-paint behaviour —
variant state, signal plumbing, enabled/disabled toggling — since
paintEvent correctness is verified by manual QA on Windows.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.widgets.tactile_button import TactileButton


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_default_variant_is_secondary(qapp):
    b = TactileButton("HELLO")
    assert b.variant() == "secondary"
    assert b.text() == "HELLO"


def test_primary_variant_via_constructor(qapp):
    b = TactileButton("CHECK OUT", variant="primary")
    assert b.variant() == "primary"


def test_set_variant_updates(qapp):
    b = TactileButton("X", variant="secondary")
    b.setVariant("primary")
    assert b.variant() == "primary"
    b.setVariant("secondary")
    assert b.variant() == "secondary"


def test_clicked_signal_fires(qapp):
    b = TactileButton("CLICK ME")
    fired = []
    b.clicked.connect(lambda: fired.append(1))
    b.click()
    assert fired == [1]


def test_disabled_reports_not_enabled(qapp):
    b = TactileButton("X")
    assert b.isEnabled() is True
    b.setEnabled(False)
    assert b.isEnabled() is False


def test_minimum_height_is_larger_for_primary(qapp):
    secondary = TactileButton("S", variant="secondary")
    primary = TactileButton("P", variant="primary")
    assert primary.minimumHeight() >= secondary.minimumHeight()
