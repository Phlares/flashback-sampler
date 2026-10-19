"""The root conftest stubs every modal Qt dialog for the whole session,
so a production path that opens one cannot hang a test."""

from __future__ import annotations

from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox


def test_message_box_helpers_return_without_a_dialog(qapp):
    ok, yes = QMessageBox.StandardButton.Ok, QMessageBox.StandardButton.Yes
    assert QMessageBox.warning(None, "t", "m") == ok
    assert QMessageBox.critical(None, "t", "m") == ok
    assert QMessageBox.information(None, "t", "m") == ok
    assert QMessageBox.question(None, "t", "m") == yes
    assert QMessageBox.about(None, "t", "m") is None
    assert QMessageBox().exec() == ok


def test_menus_do_not_open(qapp):
    menu = QMenu()
    assert menu.exec() is None
    assert menu.popup(menu.pos()) is None


def test_file_dialogs_report_cancel(qapp):
    assert QFileDialog.getSaveFileName() == ("", "")
    assert QFileDialog.getOpenFileName() == ("", "")
    assert QFileDialog.getExistingDirectory() == ""
