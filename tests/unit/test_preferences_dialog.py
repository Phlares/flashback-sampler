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


def test_global_hotkeys_checkbox_reflects_and_fires(qapp):
    changes = []
    dlg = PreferencesDialog(
        show_notifications=True, on_notifications_changed=lambda v: None,
        global_hotkeys_enabled=True, on_global_hotkeys_changed=lambda v: changes.append(v),
        global_hotkeys_supported=True,
    )
    assert dlg.global_hotkeys_check.isChecked() is True
    dlg.global_hotkeys_check.setChecked(False)
    assert changes == [False]


def test_global_hotkeys_checkbox_disabled_when_unsupported(qapp):
    dlg = PreferencesDialog(
        show_notifications=True, on_notifications_changed=lambda v: None,
        global_hotkeys_enabled=True, on_global_hotkeys_changed=lambda v: None,
        global_hotkeys_supported=False,
    )
    assert dlg.global_hotkeys_check.isEnabled() is False
    assert dlg.global_hotkeys_check.isChecked() is False  # forced off when unsupported


def test_export_section_reflects_initial_values(qapp):
    dlg = PreferencesDialog(
        show_notifications=True,
        on_notifications_changed=lambda v: None,
        export_pool_dir="D:/pool",
        export_bit_depth="PCM_24",
    )
    assert dlg.export_dir_edit.text() == "D:/pool"
    assert dlg.export_dir_edit.isReadOnly()
    assert dlg.export_depth_combo.currentData() == "PCM_24"


def test_export_depth_change_fires_callback(qapp):
    got = []
    dlg = PreferencesDialog(
        show_notifications=True,
        on_notifications_changed=lambda v: None,
        export_bit_depth="FLOAT",
        on_export_bit_depth_changed=got.append,
    )
    idx = dlg.export_depth_combo.findData("PCM_16")
    dlg.export_depth_combo.setCurrentIndex(idx)
    assert got == ["PCM_16"]


def test_export_dir_browse_cancel_changes_nothing(qapp):
    # conftest stubs QFileDialog.getExistingDirectory to return "" (cancel)
    got = []
    dlg = PreferencesDialog(
        show_notifications=True,
        on_notifications_changed=lambda v: None,
        export_pool_dir="D:/pool",
        on_export_pool_dir_changed=got.append,
    )
    dlg.export_dir_btn.click()
    assert got == []
    assert dlg.export_dir_edit.text() == "D:/pool"
