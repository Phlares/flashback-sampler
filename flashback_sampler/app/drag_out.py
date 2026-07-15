"""
OS file drag-out — offer an already-rendered file to any drop target
(DAW track, Explorer, ...) as a standard CF_HDROP-style file drag.

Kept as a tiny seam so the blocking QDrag.exec loop is injectable in
tests; everything above this (what to render, what "accepted" means for
checkout state) lives in the window controller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QMimeData, QUrl, Qt
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QWidget


def build_file_drag_mime(file_path: Path | str) -> QMimeData:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(Path(file_path).resolve()))])
    return mime


def perform_file_drag(
    source_widget: QWidget,
    file_path: Path | str,
    exec_fn: Optional[Callable[[QDrag], Qt.DropAction]] = None,
) -> bool:
    """
    Run a blocking OS drag offering `file_path`. Returns True when the
    drop target accepted the file (any action except IgnoreAction —
    some targets report Move/Link even though the file stays put).
    """
    drag = QDrag(source_widget)
    drag.setMimeData(build_file_drag_mime(file_path))
    action = drag.exec(Qt.CopyAction) if exec_fn is None else exec_fn(drag)
    return action != Qt.IgnoreAction
