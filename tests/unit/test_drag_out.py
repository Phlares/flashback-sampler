"""Unit tests for the OS file-drag seam (exec injected, no real drag loop)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from flashback_sampler.app.drag_out import build_file_drag_mime, perform_file_drag


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_build_file_drag_mime_carries_local_file_url(qapp, tmp_path):
    f = tmp_path / "slice.wav"
    f.write_bytes(b"")
    mime = build_file_drag_mime(f)
    urls = mime.urls()
    assert len(urls) == 1
    assert urls[0].isLocalFile()
    assert Path(urls[0].toLocalFile()) == f.resolve()


def test_perform_file_drag_true_on_copy(qapp, tmp_path):
    f = tmp_path / "slice.wav"
    f.write_bytes(b"")
    w = QWidget()
    seen = {}

    def fake_exec(drag):
        seen["urls"] = drag.mimeData().urls()
        return Qt.CopyAction

    assert perform_file_drag(w, f, exec_fn=fake_exec) is True
    assert len(seen["urls"]) == 1


def test_perform_file_drag_false_on_ignore(qapp, tmp_path):
    f = tmp_path / "slice.wav"
    f.write_bytes(b"")
    assert perform_file_drag(QWidget(), f, exec_fn=lambda d: Qt.IgnoreAction) is False
