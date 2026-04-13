"""
SourceStrip — chip reconciliation and signal wiring.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.state import AppState
from flashback_sampler.app.widgets.slot_chip import SlotChip
from flashback_sampler.app.widgets.source_strip import SourceStrip
from flashback_sampler.core.quality_presets import preset_by_name


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_source_strip_starts_empty(qapp):
    strip = SourceStrip()
    assert strip._chips == []


def test_set_slots_grows_chip_list_to_match(qapp):
    strip = SourceStrip()
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    strip.set_slots(st.slots, st.active_slot_index)
    assert len(strip._chips) == 1

    st.add_slot(preset_by_name("SCRATCH"), name="B")
    st.add_slot(preset_by_name("CHAT"), name="C")
    strip.set_slots(st.slots, st.active_slot_index)
    assert len(strip._chips) == 3


def test_set_slots_shrinks_chip_list_when_slots_removed(qapp):
    strip = SourceStrip()
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.add_slot(preset_by_name("SCRATCH"))
    st.add_slot(preset_by_name("CHAT"))
    strip.set_slots(st.slots, st.active_slot_index)
    assert len(strip._chips) == 3

    st.remove_slot(2)
    strip.set_slots(st.slots, st.active_slot_index)
    assert len(strip._chips) == 2


def test_chip_reflects_active_slot(qapp):
    strip = SourceStrip()
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.add_slot(preset_by_name("SCRATCH"))
    st.set_active_slot_index(1)
    strip.set_slots(st.slots, st.active_slot_index)

    assert strip._chips[0]._is_active is False
    assert strip._chips[1]._is_active is True


def test_chip_receives_slot_name_and_ram(qapp):
    strip = SourceStrip()
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.slots[0].name = "Main"
    new_slot = st.add_slot(preset_by_name("CHAT"), name="Discord")
    strip.set_slots(st.slots, st.active_slot_index)

    assert strip._chips[0]._name == "Main"
    assert strip._chips[1]._name == "Discord"
    # RAM matches the preset
    assert strip._chips[1]._ram_mb == pytest.approx(new_slot.ram_mb())


def test_active_changed_emits_on_chip_click(qapp):
    strip = SourceStrip()
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.add_slot(preset_by_name("SCRATCH"))
    strip.set_slots(st.slots, st.active_slot_index)

    fired: list[int] = []
    strip.activeChanged.connect(lambda i: fired.append(i))
    strip._chips[1].clicked.emit()
    assert fired == [1]


def test_add_source_requested_emits_on_button_click(qapp):
    strip = SourceStrip()
    fired: list[int] = []
    strip.addSourceRequested.connect(lambda: fired.append(1))
    strip._add_btn.click()
    assert fired == [1]


def test_slot_chip_state_update_triggers_repaint(qapp):
    chip = SlotChip()
    chip.set_state(
        name="Main",
        fill_percent=10.0,
        is_active=True,
        is_capturing=True,
        xrun_count=5,
        ram_mb=123.0,
    )
    assert chip._name == "Main"
    assert chip._fill_percent == 10.0
    assert chip._is_active is True
    assert chip._is_capturing is True
    assert chip._xrun_count == 5
    assert chip._ram_mb == 123.0
