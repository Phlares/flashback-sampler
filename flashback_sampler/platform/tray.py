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
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSystemTrayIcon, QMenu

from flashback_sampler.core.quality_presets import MB
from flashback_sampler.core.source_status import SEVERITY_RING_COLOR, Severity
from flashback_sampler.input.core import invoke

APP_NAME = "flashback-sampler"


def record_action_label(is_recording: bool) -> str:
    """Option B: the verb swaps with state."""
    return "Stop Recording" if is_recording else "Start Recording (All Sources)"


def _fmt_mem(memory_bytes: int | None) -> str:
    if not memory_bytes:
        return ""
    return f" · {memory_bytes / MB:.0f} MB"


def tooltip_text(
    is_recording: bool, source_count: int, memory_bytes: int | None = None
) -> str:
    mem = _fmt_mem(memory_bytes)
    if not is_recording:
        return f"{APP_NAME} — Idle{mem}"
    plural = "s" if source_count != 1 else ""
    return f"{APP_NAME} — Recording ({source_count} source{plural}){mem}"


def _disc_icon(recording: bool, severity: Severity = Severity.OK) -> QIcon:
    """Edge-to-edge two-ring tray icon.

    Outer ring carries the status colour; the centre shows a red dot while
    recording, or pause bars when idle.
    """
    px = QPixmap(32, 32)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    # Disc fill + status ring, run right up to the edge.
    p.setBrush(QColor("#15151a"))
    pen = QPen(QColor(SEVERITY_RING_COLOR.get(severity, SEVERITY_RING_COLOR[Severity.OK])))
    pen.setWidthF(2.7)
    p.setPen(pen)
    p.drawEllipse(2.0, 2.0, 28.0, 28.0)
    # Centre: red dot while recording, pause bars when idle.
    p.setPen(QColor(0, 0, 0, 0))
    if recording:
        p.setBrush(QColor("#ff3b30"))
        p.drawEllipse(11.0, 11.0, 10.0, 10.0)
    else:
        p.setBrush(QColor("#cfc9ba"))
        p.drawRoundedRect(11.4, 9.5, 3.4, 13.0, 1.3, 1.3)
        p.drawRoundedRect(17.2, 9.5, 3.4, 13.0, 1.3, 1.3)
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
        on_toggle_notifications: Callable[[bool], None] | None = None,
        memory_bytes: Callable[[], int] | None = None,
        worst_severity: Callable[[], Severity] | None = None,
        show_toasts: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._is_recording = is_recording
        self._source_count = source_count
        self._on_open = on_open
        self._on_quit = on_quit
        self._on_settings = on_settings
        self._on_toggle_notifications = on_toggle_notifications
        self._memory_bytes = memory_bytes
        self._worst_severity = worst_severity
        self._show_toasts = show_toasts
        self._icon_key: tuple[bool, Severity] | None = None  # cache to avoid rebuilds

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
            self._tray.showMessage(
                title, message, _disc_icon(self._is_recording(), self._severity())
            )

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

        self._act_notify = QAction("Show notifications", self)
        self._act_notify.setCheckable(True)
        self._act_notify.setChecked(self._show_toasts)
        self._act_notify.toggled.connect(self._on_notify_toggled)
        self._menu.addAction(self._act_notify)

        self._menu.addSeparator()

        if self._on_settings is not None:
            act_settings = QAction("Settings…", self)
            act_settings.triggered.connect(lambda: self._on_settings())
            self._menu.addAction(act_settings)

        act_quit = QAction("Quit flashback-sampler", self)
        act_quit.triggered.connect(lambda: self._on_quit())
        self._menu.addAction(act_quit)

    # -- state sync -------------------------------------------------------

    def _severity(self) -> Severity:
        return self._worst_severity() if self._worst_severity is not None else Severity.OK

    def refresh(self) -> None:
        """Re-read state and update the record label, icon, and tooltip.

        Safe to call at ~1 Hz: the icon is only repainted when the
        (recording, severity) pair actually changes."""
        recording = self._is_recording()
        self._act_record.setText(record_action_label(recording))
        key = (recording, self._severity())
        if key != self._icon_key:
            self._icon_key = key
            self._tray.setIcon(_disc_icon(recording, key[1]))
        self.update_tooltip()

    def update_tooltip(self) -> None:
        """Refresh just the tooltip (cheap — called periodically for live
        memory readout, without rebuilding the icon)."""
        mem = self._memory_bytes() if self._memory_bytes is not None else None
        self._tray.setToolTip(
            tooltip_text(self._is_recording(), self._source_count(), mem)
        )

    def set_notifications_enabled(self, enabled: bool) -> None:
        """Reflect a notifications-pref change from elsewhere (e.g. the
        Preferences page) onto the menu toggle and toast behaviour."""
        self._show_toasts = enabled
        if self._act_notify.isChecked() != enabled:
            # Programmatic sync — block signals so this doesn't re-fire the
            # user-toggle callback (which would double-persist / loop).
            self._act_notify.blockSignals(True)
            self._act_notify.setChecked(enabled)
            self._act_notify.blockSignals(False)

    def _on_notify_toggled(self, checked: bool) -> None:
        self._show_toasts = checked
        if self._on_toggle_notifications is not None:
            self._on_toggle_notifications(checked)

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
