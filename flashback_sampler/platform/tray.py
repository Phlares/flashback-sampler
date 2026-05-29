"""System-tray (notification area) integration — Option B menu.

Cross-platform via ``QSystemTrayIcon``; behaviour is tuned for the Windows
notification area (the only platform with loopback capture today, but the
tray itself works wherever Qt reports one available).

Menu (Option B — explicit Start/Stop verbs that swap by state):

    Open flashback-sampler        (default — also opened by left-click)
    ──────────────────────────
    Start Recording (All Sources) ⇄ Stop Recording
    Check Out Clip
    ──────────────────────────
    Settings…
    Quit flashback-sampler

The controller is intentionally decoupled from the window: it takes small
callbacks for state and lifecycle, and routes Start/Stop/Checkout through
the keybinding engine's action registry (``invoke``) so the tray and the
in-window buttons drive exactly the same actions.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QSystemTrayIcon, QMenu

from flashback_sampler.input.core import invoke

APP_NAME = "flashback-sampler"


def record_action_label(is_recording: bool) -> str:
    """Option B: the verb swaps with state."""
    return "Stop Recording" if is_recording else "Start Recording (All Sources)"


def tooltip_text(is_recording: bool, source_count: int) -> str:
    if not is_recording:
        return f"{APP_NAME} — Idle"
    plural = "s" if source_count != 1 else ""
    return f"{APP_NAME} — Recording ({source_count} source{plural})"


def _disc_icon(recording: bool) -> QIcon:
    """Paint a small turntable-disc tray icon; add a red rec-dot when live."""
    px = QPixmap(32, 32)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    # disc body + cream rim
    p.setPen(QColor("#e8e6df"))
    p.setBrush(QColor("#15151a"))
    p.drawEllipse(3, 3, 26, 26)
    # ember spindle
    p.setPen(QColor(0, 0, 0, 0))
    p.setBrush(QColor("#e2632a"))
    p.drawEllipse(14, 14, 4, 4)
    if recording:
        p.setBrush(QColor("#ff3b30"))
        p.setPen(QColor("#15151a"))
        p.drawEllipse(20, 20, 10, 10)
    p.end()
    return QIcon(px)


class SystemTray(QObject):
    """Owns the QSystemTrayIcon and its Option-B menu.

    Callbacks:
      ``is_recording()`` -> bool        — is any source currently capturing?
      ``source_count()`` -> int         — number of capturing sources (for tooltip)
      ``on_open()``                     — restore/raise the main window
      ``on_quit()``                     — tear down and exit the app
      ``on_settings()`` (optional)      — open the settings/keybindings dialog
    """

    def __init__(
        self,
        *,
        is_recording: Callable[[], bool],
        source_count: Callable[[], int],
        on_open: Callable[[], None],
        on_quit: Callable[[], None],
        on_settings: Callable[[], None] | None = None,
        show_toasts: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._is_recording = is_recording
        self._source_count = source_count
        self._on_open = on_open
        self._on_quit = on_quit
        self._on_settings = on_settings
        self._show_toasts = show_toasts

        self._tray = QSystemTrayIcon(parent)
        self._menu = QMenu()
        self._build_menu()
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self.refresh()

    # -- lifecycle --------------------------------------------------------

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def notify(self, title: str, message: str) -> None:
        if self._show_toasts and self._tray.supportsMessages():
            self._tray.showMessage(title, message, _disc_icon(self._is_recording()))

    # -- menu -------------------------------------------------------------

    def _build_menu(self) -> None:
        self._act_open = QAction("Open flashback-sampler", self)
        f = QFont(self._act_open.font())
        f.setBold(True)  # the default command, per notification-area convention
        self._act_open.setFont(f)
        self._act_open.triggered.connect(lambda: self._on_open())
        self._menu.addAction(self._act_open)
        self._menu.setDefaultAction(self._act_open)

        self._menu.addSeparator()

        self._act_record = QAction("", self)
        self._act_record.triggered.connect(self._toggle_record)
        self._menu.addAction(self._act_record)

        self._act_checkout = QAction("Check Out Clip", self)
        self._act_checkout.triggered.connect(self._checkout)
        self._menu.addAction(self._act_checkout)

        self._menu.addSeparator()

        if self._on_settings is not None:
            act_settings = QAction("Settings…", self)
            act_settings.triggered.connect(lambda: self._on_settings())
            self._menu.addAction(act_settings)

        act_quit = QAction("Quit flashback-sampler", self)
        act_quit.triggered.connect(lambda: self._on_quit())
        self._menu.addAction(act_quit)

    # -- state sync -------------------------------------------------------

    def refresh(self) -> None:
        """Re-read state and update the record label, icon, and tooltip."""
        recording = self._is_recording()
        self._act_record.setText(record_action_label(recording))
        self._tray.setIcon(_disc_icon(recording))
        self._tray.setToolTip(tooltip_text(recording, self._source_count()))

    # -- handlers ---------------------------------------------------------

    def _toggle_record(self) -> None:
        was_recording = self._is_recording()
        invoke("transport.stop_recording" if was_recording else "transport.start_recording")
        self.refresh()
        if not was_recording and self._is_recording():
            self.notify("Recording started", "Capturing all sources into the ring buffer.")
        elif was_recording and not self._is_recording():
            self.notify("Recording stopped", "Ring buffer paused. Checkouts are kept.")

    def _checkout(self) -> None:
        invoke("clip.checkout")
        self._on_open()  # surface the new clip in the window

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Left-click (Trigger) and double-click both open the window.
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_open()
