"""Tests for NavBar widget."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.widgets.nav_bar import NavBar


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_nav_bar_instantiates(qapp):
    bar = NavBar()
    assert bar is not None
    assert bar.minimumHeight() >= 26


def test_nav_bar_has_arm_all_button(qapp):
    bar = NavBar()
    assert bar.arm_all_btn is not None
    assert bar.arm_all_btn.text() == "ARM ALL"


def test_nav_bar_has_source_slots(qapp):
    bar = NavBar()
    assert len(bar.source_slots) == 3


def test_nav_bar_has_add_source_button(qapp):
    bar = NavBar()
    assert bar.add_source_btn is not None


def test_nav_bar_has_config_labels(qapp):
    bar = NavBar()
    assert bar.clip_length_label.text() == "3:00"
    assert bar.buffer_length_label.text() == "15:00"
    assert bar.project_size_label.text() == "~4.31 GB"


def test_set_source_names_updates_chips(qapp):
    bar = NavBar()
    bar.set_source_names(["Main", "Game"])
    assert bar.source_slots[0]._name == "MAIN"
    assert bar.source_slots[1]._name == "GAME"
    # Chip 2 falls back to default label
    assert bar.source_slots[2]._name == "SOURCE 3"


def test_set_source_names_empty_uses_defaults(qapp):
    bar = NavBar()
    bar.set_source_names([])
    assert bar.source_slots[0]._name == "SOURCE 1"
    assert bar.source_slots[1]._name == "SOURCE 2"
    assert bar.source_slots[2]._name == "SOURCE 3"
