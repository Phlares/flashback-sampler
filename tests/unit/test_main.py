"""Tests for the app entry point's missing-native-core path."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from flashback_sampler.app import main as main_mod
from flashback_sampler.core import native


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_run_shows_dialog_and_returns_1_when_native_core_missing(qapp, monkeypatch):
    """AppState raises RuntimeError when the native audio core is not
    built. _run must catch it, tell the user with a QMessageBox instead
    of exiting silently, and return 1 -- the exit code main() forwards
    to sys.exit()."""
    monkeypatch.setattr(native, "_lib", None)
    monkeypatch.setattr(native, "_lib_tried", True)

    calls = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: calls.append((a, k)))

    args = main_mod._parse_args([])
    rc = main_mod._run(qapp, args)

    assert calls, "QMessageBox.critical was not called"
    assert rc == 1
