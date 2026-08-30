"""
Entry point: python -m flashback_sampler.app.main

Boots the QApplication, applies the Erebus base stylesheet, builds the
AppState object graph, shows the main window, and runs the event loop.

CLI:
    --buffer-minutes N    ring buffer length in minutes (default 5)
    --sample-rate N       override the capture sample rate (default 48000)
    --channels N          1 = mono, 2 = stereo (default)
"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.state import AppState
from flashback_sampler.app.theme import EREBUS, base_stylesheet, load_fonts


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="flashback-sampler")
    p.add_argument(
        "--buffer-minutes",
        type=float,
        default=5.0,
        help="ring buffer length in minutes (default: 5). "
        "Use a small value like 0.5 to test rollover quickly.",
    )
    p.add_argument("--sample-rate", type=int, default=48_000)
    p.add_argument("--channels", type=int, default=2, choices=(1, 2))
    # Parse known args only — leave sys.argv[1:] extras untouched for Qt
    args, _ = p.parse_known_args(argv)
    return args


def main() -> int:
    args = _parse_args(sys.argv[1:])
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

    # Load bundled Monaspace fonts BEFORE building the stylesheet so
    # the font-family stacks resolve correctly for the first paint.
    load_fonts(app)
    app.setStyleSheet(base_stylesheet())

    state = AppState(
        buffer_seconds=args.buffer_minutes * 60.0,
        sample_rate=args.sample_rate,
        channels=args.channels,
    )
    from flashback_sampler.app.turntable_window import TurntableWindow
    window = TurntableWindow(state)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
