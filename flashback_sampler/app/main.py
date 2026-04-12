"""
Entry point: python -m flashback_sampler.app.main

Boots the QApplication, applies the Erebus base stylesheet, builds the
AppState object graph, shows the main window, and runs the event loop.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.state import AppState
from flashback_sampler.app.main_window import MainWindow
from flashback_sampler.app.theme import EREBUS, base_stylesheet


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("flashback-sampler")
    app.setOrganizationName("flashback-sampler")

    # Force a dark palette so native dialogs inherit the chassis tone
    pal = QPalette()
    chassis = QColor(EREBUS["chassis"])
    plate = QColor(EREBUS["plate"])
    cream = QColor(EREBUS["cream"])
    bone = QColor(EREBUS["bone"])
    ember = QColor(EREBUS["ember"])
    pal.setColor(QPalette.Window, chassis)
    pal.setColor(QPalette.WindowText, cream)
    pal.setColor(QPalette.Base, plate)
    pal.setColor(QPalette.AlternateBase, chassis)
    pal.setColor(QPalette.Text, cream)
    pal.setColor(QPalette.Button, plate)
    pal.setColor(QPalette.ButtonText, cream)
    pal.setColor(QPalette.Highlight, ember)
    pal.setColor(QPalette.HighlightedText, QColor(EREBUS["void"]))
    pal.setColor(QPalette.PlaceholderText, bone)
    app.setPalette(pal)

    app.setStyleSheet(base_stylesheet())

    state = AppState()
    window = MainWindow(state)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
