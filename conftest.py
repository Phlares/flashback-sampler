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
    from PySide6.QtWidgets import QMenu

    QMenu.exec = lambda self, *a, **kw: None  # type: ignore[assignment]
    QMenu.popup = lambda self, *a, **kw: None  # type: ignore[assignment]
except ImportError:
    pass
