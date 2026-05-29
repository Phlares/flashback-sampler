"""Platform detection and the cross-platform SEAM MAP.

This module is the single place to answer *"what is platform-specific in
flashback-sampler?"* — the porting checklist lives in ``PLATFORM.md``.

The seams a new platform must address:

  * **Capture / source listening** — ``flashback_sampler/app/audio_devices.py``
    (``list_capture_devices`` gates loopback to Windows; ``build_capture_source``
    selects the backend by ``device.kind``). Backends:
    ``core/loopback_capture.py`` (WASAPI loopback, soundcard),
    ``io/win32_process_loopback.py`` (per-process WASAPI, ctypes),
    ``core/capture.py`` (mic / line-in via sounddevice — already cross-platform).
  * **System tray** — ``flashback_sampler/platform/tray.py`` (QSystemTrayIcon).
  * **Config / data paths** — ``flashback_sampler/app/config.py`` (APPDATA / XDG).
  * **Packaging** — ``flashback_sampler.spec`` (PyInstaller, Windows onedir).

Internally this uses ``sys.platform`` rather than ``import platform`` so it
never shadows the stdlib module from inside this same-named package.
"""

from __future__ import annotations

import sys

WINDOWS = "windows"
MACOS = "macos"
LINUX = "linux"


def current_os() -> str:
    """Return one of ``WINDOWS`` / ``MACOS`` / ``LINUX`` for the host."""
    if sys.platform == "win32":
        return WINDOWS
    if sys.platform == "darwin":
        return MACOS
    return LINUX


def loopback_supported() -> bool:
    """True if system-audio (speaker) loopback capture is available.

    Today only Windows ships a loopback backend (WASAPI via ``soundcard``
    and per-process WASAPI via ctypes). Mic / line-in capture works on every
    platform regardless of this flag. macOS / Linux loopback are the open
    seam — see ``PLATFORM.md``.
    """
    return current_os() == WINDOWS


def tray_supported() -> bool:
    """True if a system tray / notification area is usable right now.

    Defers to Qt (``QSystemTrayIcon.isSystemTrayAvailable()``), so this is
    False under headless / offscreen Qt and on desktops without a tray.
    Returns False if no ``QApplication`` exists yet — the Qt static below
    requires a running application and crashes if called before one is
    constructed.
    """
    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    except Exception:
        return False
    if QApplication.instance() is None:
        return False
    return QSystemTrayIcon.isSystemTrayAvailable()
