import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _clear_action_registry():
    """Reset the global action registry before every test so that
    tests that instantiate TurntableWindow (which registers transport
    actions) do not collide with each other."""
    from flashback_sampler.input.core.actions import clear_registry

    clear_registry()
    yield
    clear_registry()


# Block every modal Qt dialog for the whole pytest session. Some widgets
# emit contextMenuRequested when right-clicked, and production code
# connects those to handlers that call QMenu.exec(). The calls escape a
# test's scope (queued via QTimer / event loop), so a function-scoped
# patch is not enough: the stubs live as long as the session.
@pytest.fixture(scope="session", autouse=True)
def _no_modal_dialogs():
    from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox

    ok, yes = QMessageBox.StandardButton.Ok, QMessageBox.StandardButton.Yes
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(QMenu, "exec", lambda self, *a, **kw: None)
        mp.setattr(QMenu, "popup", lambda self, *a, **kw: None)
        # The static QMessageBox helpers call exec() on a modal dialog. On
        # a CI runner with no audio device, production paths (e.g. a
        # start-capture failure) fire them and hang the test thread.
        mp.setattr(QMessageBox, "warning", staticmethod(lambda *a, **kw: ok))
        mp.setattr(QMessageBox, "critical", staticmethod(lambda *a, **kw: ok))
        mp.setattr(QMessageBox, "information", staticmethod(lambda *a, **kw: ok))
        mp.setattr(QMessageBox, "question", staticmethod(lambda *a, **kw: yes))
        mp.setattr(QMessageBox, "about", staticmethod(lambda *a, **kw: None))
        mp.setattr(QMessageBox, "exec", lambda self, *a, **kw: ok)
        # Pickers return empty, which callers treat as a cancel.
        mp.setattr(
            QFileDialog, "getSaveFileName", staticmethod(lambda *a, **kw: ("", ""))
        )
        mp.setattr(
            QFileDialog, "getOpenFileName", staticmethod(lambda *a, **kw: ("", ""))
        )
        mp.setattr(
            QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **kw: "")
        )
        yield
