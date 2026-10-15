"""
Session-wide pytest configuration.

The suite requires the built flashback_core library: there is no Python
ring buffer, so a missing library exits the session with the build
command instead of skipping.
"""
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

# Module-level so garbage collection cannot free the app before the
# widgets that depend on it.
_qapp: QApplication | None = None


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    global _qapp
    _qapp = QApplication.instance() or QApplication([])
    return _qapp


@pytest.fixture(autouse=True)
def _delete_top_level_widgets():
    """Signal connections keep a dropped window alive until exit, and
    PySide6 6.12 crashes at exit when they are still alive (#93).
    deleteLater, not close: closeEvent runs app teardown such as
    TurntableWindow's state.shutdown()."""
    yield
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def pytest_sessionstart(session: pytest.Session) -> None:
    from flashback_sampler.core import native

    if native.load() is None:
        pytest.exit(
            "flashback_core native library not found (checked core/zig-out/bin, "
            "core/zig-out/lib, and flashback_sampler/core). There is no Python "
            "ring buffer any more, so nothing can run without it. Build it: "
            "`zig build --build-file core/build.zig -Doptimize=ReleaseSafe`.",
            returncode=1,
        )
