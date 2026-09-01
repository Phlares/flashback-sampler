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


def test_build_file_drag_mime_carries_every_url_in_order(qapp, tmp_path):
    """The Ableton sidecar rides the same drag as the WAV; Live reads the
    .alc, Explorer copies both."""
    wav, alc = tmp_path / "slice.wav", tmp_path / "slice.alc"
    wav.write_bytes(b"")
    alc.write_bytes(b"")
    urls = build_file_drag_mime([wav, alc]).urls()
    assert [Path(u.toLocalFile()) for u in urls] == [wav.resolve(), alc.resolve()]


def test_perform_file_drag_forwards_a_list(qapp, tmp_path):
    wav, alc = tmp_path / "slice.wav", tmp_path / "slice.alc"
    wav.write_bytes(b"")
    alc.write_bytes(b"")
    seen = {}

    def fake_exec(drag):
        seen["urls"] = drag.mimeData().urls()
        return Qt.CopyAction

    assert perform_file_drag(QWidget(), [wav, alc], exec_fn=fake_exec) is True
    assert len(seen["urls"]) == 2


def test_build_file_drag_mime_accepts_any_path_like(qapp, tmp_path):
    """A scalar that is not a str or a Path (an os.PathLike wrapper) must
    be taken as ONE path, not iterated character by character."""
    f = tmp_path / "slice.wav"
    f.write_bytes(b"")

    class Wrapper:
        def __fspath__(self):
            return str(f)

    urls = build_file_drag_mime(Wrapper()).urls()
    assert [Path(u.toLocalFile()) for u in urls] == [f.resolve()]
