import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.preferences_dialog import PreferencesDialog


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_checkbox_reflects_initial_state(qapp):
    dlg = PreferencesDialog(show_notifications=False, on_notifications_changed=lambda v: None)
    assert dlg.notify_check.isChecked() is False
    dlg2 = PreferencesDialog(show_notifications=True, on_notifications_changed=lambda v: None)
    assert dlg2.notify_check.isChecked() is True


def test_toggling_checkbox_applies_live(qapp):
    changes = []
    dlg = PreferencesDialog(
        show_notifications=True,
        on_notifications_changed=lambda v: changes.append(v),
    )
    dlg.notify_check.setChecked(False)
    dlg.notify_check.setChecked(True)
    assert changes == [False, True]
