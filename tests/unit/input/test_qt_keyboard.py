import pytest
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QWidget

from flashback_sampler.input.core import (
    Action, BindingTable, clear_registry, register,
)
from flashback_sampler.input.sources.qt_keyboard import KeyboardSource


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _press(key: int, modifiers: Qt.KeyboardModifier = Qt.NoModifier, auto_repeat: bool = False) -> QKeyEvent:
    return QKeyEvent(QEvent.KeyPress, key, modifiers, 0, 0, 0, "", auto_repeat, 1)


def test_press_event_invokes_bound_action(qapp):
    fired = []
    register(Action(id="t.play", name="Play", category="T",
                    callable=lambda: fired.append(1),
                    default_binding="Space"))
    table = BindingTable()
    widget = QWidget()
    source = KeyboardSource(table, widget)

    source.eventFilter(widget, _press(Qt.Key_Space))
    assert fired == [1]


def test_release_event_ignored(qapp):
    fired = []
    register(Action(id="t.play", name="Play", category="T",
                    callable=lambda: fired.append(1),
                    default_binding="Space"))
    table = BindingTable()
    widget = QWidget()
    source = KeyboardSource(table, widget)
    release = QKeyEvent(QEvent.KeyRelease, Qt.Key_Space, Qt.NoModifier, 0, 0, 0, "", False, 1)
    source.eventFilter(widget, release)
    assert fired == []


def test_modifier_chord_resolves(qapp):
    fired = []
    register(Action(id="t.record", name="Record", category="T",
                    callable=lambda: fired.append(1),
                    default_binding="Ctrl+Shift+R"))
    table = BindingTable()
    widget = QWidget()
    source = KeyboardSource(table, widget)
    event = _press(Qt.Key_R, Qt.ControlModifier | Qt.ShiftModifier)
    source.eventFilter(widget, event)
    assert fired == [1]


def test_auto_repeat_flag_respects_ignore_repeat_policy(qapp):
    fired = []
    register(Action(id="t.record", name="Record", category="T",
                    callable=lambda: fired.append(1),
                    default_binding="R",
                    repeat_policy="ignore_repeat"))
    table = BindingTable()
    widget = QWidget()
    source = KeyboardSource(table, widget)
    source.eventFilter(widget, _press(Qt.Key_R, auto_repeat=False))
    source.eventFilter(widget, _press(Qt.Key_R, auto_repeat=True))
    source.eventFilter(widget, _press(Qt.Key_R, auto_repeat=True))
    assert fired == [1]
