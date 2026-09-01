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


def test_scratch_dir_row_reports_a_pick(qapp, monkeypatch):
    seen = []
    dlg = PreferencesDialog(show_notifications=True, on_notifications_changed=lambda c: None,
                            scratch_dir="C:/old", on_scratch_dir_changed=seen.append)
    assert dlg.scratch_dir_edit.text() == "C:/old"
    monkeypatch.setattr("flashback_sampler.app.preferences_dialog.QFileDialog.getExistingDirectory",
                        staticmethod(lambda *a, **k: "C:/new"))
    dlg.scratch_dir_btn.click()
    assert seen == ["C:/new"] and dlg.scratch_dir_edit.text() == "C:/new"


def test_memory_row_shows_the_footprint_and_reports_edits(qapp):
    """#41: the max footprint is a live tunable; 0 means no cap. The hint
    names physical and free RAM so the number has a reference."""
    seen = []
    dlg = PreferencesDialog(show_notifications=True, on_notifications_changed=lambda c: None,
                            max_footprint_mb=16384.0, on_max_footprint_changed=seen.append,
                            mem_total_mb=65536.0, mem_free_mb=40000.0)
    assert dlg.footprint_spin.value() == 16384
    assert "65,536" in dlg.footprint_hint.text() and "40,000" in dlg.footprint_hint.text()
    dlg.footprint_spin.setValue(0)
    dlg.footprint_spin.editingFinished.emit()
    assert seen == [0.0]


def test_drag_handle_row_reports_edits(qapp):
    """The drag-out handle budget is a live tunable; 0 = slice only."""
    seen = []
    dlg = PreferencesDialog(show_notifications=True, on_notifications_changed=lambda c: None,
                            drag_handle_mb=200.0, on_drag_handle_mb_changed=seen.append)
    assert dlg.drag_cap_spin.value() == 200
    dlg.drag_cap_spin.setValue(50)
    assert seen == [50.0]
