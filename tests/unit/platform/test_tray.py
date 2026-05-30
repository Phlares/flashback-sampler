import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.core.source_status import Severity
from flashback_sampler.input.core import Action, register
from flashback_sampler.platform.tray import (
    SystemTray,
    record_action_label,
    tooltip_text,
)


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


# -- pure helpers (no Qt) -------------------------------------------------

def test_record_action_label_swaps_with_state():
    assert record_action_label(False) == "Start Recording (All Sources)"
    assert record_action_label(True) == "Stop Recording"


def test_tooltip_text():
    assert tooltip_text(False, 3) == "flashback-sampler — Idle"
    assert tooltip_text(True, 1) == "flashback-sampler — Recording (1 source)"
    assert tooltip_text(True, 3) == "flashback-sampler — Recording (3 sources)"


def test_tooltip_includes_memory_when_provided():
    assert tooltip_text(False, 0, 0) == "flashback-sampler — Idle"  # 0 → no suffix
    assert tooltip_text(False, 0, 150 * 1024 * 1024) == "flashback-sampler — Idle · 150 MB"
    assert (
        tooltip_text(True, 2, 142 * 1024 * 1024)
        == "flashback-sampler — Recording (2 sources) · 142 MB"
    )


# -- controller behaviour -------------------------------------------------

def test_record_action_reflects_and_toggles_state(qapp):
    state = {"rec": False}
    register(Action(id="transport.start_recording", name="Start", category="T",
                    callable=lambda: state.update(rec=True)))
    register(Action(id="transport.stop_recording", name="Stop", category="T",
                    callable=lambda: state.update(rec=False)))
    tray = SystemTray(
        is_recording=lambda: state["rec"], source_count=lambda: 1,
        on_open=lambda: None, on_quit=lambda: None, show_toasts=False,
    )
    assert tray._act_record.text() == "Start Recording (All Sources)"
    tray._toggle_record()
    assert state["rec"] is True
    assert tray._act_record.text() == "Stop Recording"
    tray._toggle_record()
    assert state["rec"] is False
    assert tray._act_record.text() == "Start Recording (All Sources)"


def test_checkout_invokes_action_and_opens_window(qapp):
    fired = {"checkout": 0, "open": 0}
    register(Action(id="clip.checkout", name="Checkout", category="Clip",
                    callable=lambda: fired.__setitem__("checkout", fired["checkout"] + 1)))
    tray = SystemTray(
        is_recording=lambda: False, source_count=lambda: 0,
        on_open=lambda: fired.__setitem__("open", fired["open"] + 1),
        on_quit=lambda: None, show_toasts=False,
    )
    tray._checkout()
    assert fired["checkout"] == 1
    assert fired["open"] == 1  # checkout surfaces the new clip in the window


def test_notifications_toggle_invokes_callback_and_updates_state(qapp):
    changes = []
    tray = SystemTray(
        is_recording=lambda: False, source_count=lambda: 0,
        on_open=lambda: None, on_quit=lambda: None,
        on_toggle_notifications=lambda v: changes.append(v),
        show_toasts=True,
    )
    assert tray._act_notify.isChecked() is True
    tray._act_notify.trigger()  # checkable → toggles to False
    assert changes == [False]
    assert tray._show_toasts is False


def test_set_notifications_enabled_syncs_menu_without_loop(qapp):
    changes = []
    tray = SystemTray(
        is_recording=lambda: False, source_count=lambda: 0,
        on_open=lambda: None, on_quit=lambda: None,
        on_toggle_notifications=lambda v: changes.append(v),
        show_toasts=True,
    )
    tray.set_notifications_enabled(False)
    assert tray._act_notify.isChecked() is False
    assert tray._show_toasts is False
    # setting it programmatically should NOT re-fire the user-toggle callback
    assert changes == []


def test_memory_callback_appears_in_tooltip(qapp):
    tray = SystemTray(
        is_recording=lambda: False, source_count=lambda: 0,
        on_open=lambda: None, on_quit=lambda: None,
        memory_bytes=lambda: 64 * 1024 * 1024, show_toasts=False,
    )
    tray.update_tooltip()
    assert "64 MB" in tray._tray.toolTip()


def test_icon_rebuilds_only_when_state_changes(qapp):
    sev = {"v": Severity.OK}
    tray = SystemTray(
        is_recording=lambda: True, source_count=lambda: 1,
        on_open=lambda: None, on_quit=lambda: None,
        worst_severity=lambda: sev["v"], show_toasts=False,
    )
    assert tray._icon_key == (True, Severity.OK)
    sev["v"] = Severity.WARN
    tray.refresh()
    assert tray._icon_key == (True, Severity.WARN)  # ring follows severity


def test_severity_defaults_ok_without_callback(qapp):
    tray = SystemTray(
        is_recording=lambda: False, source_count=lambda: 0,
        on_open=lambda: None, on_quit=lambda: None, show_toasts=False,
    )
    assert tray._icon_key == (False, Severity.OK)


def test_quit_callback_fires(qapp):
    fired = {"quit": 0}
    tray = SystemTray(
        is_recording=lambda: False, source_count=lambda: 0,
        on_open=lambda: None,
        on_quit=lambda: fired.__setitem__("quit", fired["quit"] + 1),
        show_toasts=False,
    )
    # the Quit action is the last in the menu
    tray._menu.actions()[-1].trigger()
    assert fired["quit"] == 1
