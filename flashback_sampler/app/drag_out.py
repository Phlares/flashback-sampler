"""
OS file drag-out — offer an already-rendered file to any drop target
(DAW track, Explorer, ...) as a standard CF_HDROP-style file drag.

Kept as a tiny seam so the blocking QDrag.exec loop is injectable in
tests; everything above this (what to render, what "accepted" means for
checkout state) lives in the window controller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QMimeData, QUrl, Qt
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QWidget


def _as_paths(file_path: Path | str | Iterable[Path | str]) -> list[Path]:
    """One path or several. A drag can carry more than one file (a WAV
    plus its Ableton sidecar) and every call site that offers one should
    not have to wrap it."""
    if isinstance(file_path, (str, Path)):
        return [Path(file_path)]
    return [Path(p) for p in file_path]


def build_file_drag_mime(file_path: Path | str | Iterable[Path | str]) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p.resolve())) for p in _as_paths(file_path)])
    return mime


def perform_file_drag(
    source_widget: QWidget,
    file_path: Path | str | Iterable[Path | str],
    exec_fn: Optional[Callable[[QDrag], Qt.DropAction]] = None,
) -> bool:
    """
    Run a blocking OS drag offering `file_path` (one path, or several).
    Returns True when the drop target accepted the files (any action
    except IgnoreAction — some targets report Move/Link even though the
    files stay put).
    """
    drag = QDrag(source_widget)
    drag.setMimeData(build_file_drag_mime(file_path))
    action = drag.exec(Qt.CopyAction) if exec_fn is None else exec_fn(drag)
    return action != Qt.IgnoreAction
