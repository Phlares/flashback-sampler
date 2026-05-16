import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# Block QMenu.exec / popup process-wide for the duration of any pytest
# session. Some widgets emit contextMenuRequested when right-clicked,
# which production code connects to handlers that call QMenu.exec().
# Those calls escape pytest scope (queued via QTimer / event loop) so
# a function-scoped monkeypatch fixture is not enough — patch at import
# time so the stub is in effect for the entire process.
try:
    from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox

    QMenu.exec = lambda self, *a, **kw: None  # type: ignore[assignment]
    QMenu.popup = lambda self, *a, **kw: None  # type: ignore[assignment]

    # QMessageBox.warning/critical/information/question/about are static
    # helpers that internally call exec() on a modal dialog. On a CI
    # runner with no audio device, production paths (e.g. start-capture
    # failure) fire these and hang the test thread forever.
    QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.Ok)  # type: ignore[assignment]
    QMessageBox.critical = staticmethod(lambda *a, **kw: QMessageBox.Ok)  # type: ignore[assignment]
    QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.Ok)  # type: ignore[assignment]
    QMessageBox.question = staticmethod(lambda *a, **kw: QMessageBox.Yes)  # type: ignore[assignment]
    QMessageBox.about = staticmethod(lambda *a, **kw: None)  # type: ignore[assignment]
    QMessageBox.exec = lambda self, *a, **kw: QMessageBox.Ok  # type: ignore[assignment]
    QMessageBox.exec_ = lambda self, *a, **kw: QMessageBox.Ok  # type: ignore[assignment]

    # Save-file and directory pickers — return empty so callers treat
    # them as user-cancelled rather than hanging.
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **kw: ("", ""))  # type: ignore[assignment]
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **kw: ("", ""))  # type: ignore[assignment]
    QFileDialog.getExistingDirectory = staticmethod(lambda *a, **kw: "")  # type: ignore[assignment]
except ImportError:
    pass
