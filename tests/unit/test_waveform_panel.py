"""Tests for WaveformPanel widget."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.widgets.waveform_panel import WaveformPanel


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_buffer_panel_instantiates(qapp):
    panel = WaveformPanel(side="buffer")
    assert panel is not None


def test_clip_panel_instantiates(qapp):
    panel = WaveformPanel(side="clip")
    assert panel is not None


def test_buffer_panel_has_labels(qapp):
    panel = WaveformPanel(side="buffer")
    assert panel.source_label.text() == "SOURCE 1"
    assert panel.title_label.text() == "BUFFER"


def test_clip_panel_has_labels(qapp):
    panel = WaveformPanel(side="clip")
    assert panel.source_label.text() == "CLIP"
    assert panel.title_label.text() == ""


def test_panel_has_time_readouts(qapp):
    panel = WaveformPanel(side="buffer")
    assert panel.time_left_label is not None
    assert panel.time_right_label is not None


def test_set_source_name(qapp):
    panel = WaveformPanel(side="buffer")
    panel.set_source_name("SOURCE 3")
    assert panel.source_label.text() == "SOURCE 3"


def test_set_duration_text(qapp):
    panel = WaveformPanel(side="buffer")
    panel.set_duration_text("3:00")
    assert panel.duration_label.text() == "3:00"


def test_panel_has_container(qapp):
    panel = WaveformPanel(side="buffer")
    assert panel.container is not None
    assert panel.container.parent() is panel


def test_container_holds_waveform_view(qapp):
    panel = WaveformPanel(side="buffer")
    # waveform should be a child (descendant) of container, not of the panel directly
    assert panel.waveform.parent() is panel.container


def test_set_demo_waveform_runs(qapp):
    panel = WaveformPanel(side="buffer")
    panel.set_demo_waveform()
    assert panel.waveform._bins is not None
