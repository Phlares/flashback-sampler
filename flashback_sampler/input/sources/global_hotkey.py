"""Global (system-wide) hotkeys via Win32 RegisterHotKey.

Lets bindings fire even when flashback-sampler is minimized or hidden to the
tray (the window-scoped KeyboardSource can't — Qt only delivers key events to
the focused window). This is a thin OS adapter that resolves a fired hotkey
through the SAME action registry as the keyboard source: a WM_HOTKEY message
maps back to an action id and `invoke()`s it.

Windows-only for now (gated by platform.capabilities.global_hotkeys_supported);
the parser is pure and the OS calls are injectable, so everything but the literal
RegisterHotKey round-trip is testable on any platform.
"""

from __future__ import annotations

import ctypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QObject
from PySide6.QtWidgets import QApplication

from flashback_sampler.input.core import invoke

try:  # wintypes is import-safe everywhere in modern Python; guard just in case
    from ctypes import wintypes as _wintypes
except Exception:  # pragma: no cover - non-Windows fallback
    _wintypes = None

# Win32 modifier flags (winuser.h) + WM_HOTKEY.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # one fire per physical press, no auto-repeat storm
WM_HOTKEY = 0x0312

_MODIFIERS = {
    "Ctrl": MOD_CONTROL, "Control": MOD_CONTROL,
    "Alt": MOD_ALT,
    "Shift": MOD_SHIFT,
    "Meta": MOD_WIN, "Win": MOD_WIN,
}


def _key_to_vk(key: str) -> int | None:
    """Map a single key token to a Win32 virtual-key code, or None."""
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    if key == "Space":
        return 0x20
    if len(key) >= 2 and key[0] == "F" and key[1:].isdigit():
        n = int(key[1:])
        if 1 <= n <= 24:
            return 0x70 + (n - 1)  # VK_F1 = 0x70
    return None


def parse_hotkey(chord: str) -> tuple[int, int] | None:
    """Parse a portable chord (e.g. ``"Ctrl+Alt+O"``) into
    ``(modifier_mask, virtual_key)`` for RegisterHotKey, or None if it can't be
    a global hotkey (bare key, modifier-only, or an unsupported key — Windows
    won't register a global hotkey without a modifier).
    """
    parts = chord.split("+")
    if len(parts) < 2:
        return None
    *mod_tokens, key = parts
    mask = 0
    for tok in mod_tokens:
        bit = _MODIFIERS.get(tok)
        if bit is None:
            return None
        mask |= bit
    if mask == 0:
        return None
    vk = _key_to_vk(key)
    if vk is None:
        return None
    return (mask | MOD_NOREPEAT, vk)


def _win_register(hotkey_id: int, mods: int, vk: int) -> bool:
    import ctypes
    return bool(ctypes.windll.user32.RegisterHotKey(None, hotkey_id, mods, vk))


def _win_unregister(hotkey_id: int) -> None:
    import ctypes
    ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)


class GlobalHotkeySource(QObject, QAbstractNativeEventFilter):
    """Registers global hotkeys for ``{chord: action_id}`` and routes WM_HOTKEY
    back through ``invoke``. OS calls are injectable for testing."""

    def __init__(
        self,
        bindings: dict[str, str],
        parent: QObject | None = None,
        *,
        register_fn: Callable[[int, int, int], bool] = _win_register,
        unregister_fn: Callable[[int], None] = _win_unregister,
    ) -> None:
        super().__init__(parent)
        self._register_fn = register_fn
        self._unregister_fn = unregister_fn
        self._registered: dict[int, str] = {}  # hotkey id -> action id
        next_id = 1
        for chord, action_id in bindings.items():
            parsed = parse_hotkey(chord)
            if parsed is None:
                continue
            mods, vk = parsed
            try:
                ok = self._register_fn(next_id, mods, vk)
            except Exception:
                ok = False
            if ok:
                self._registered[next_id] = action_id
                next_id += 1
        app = QApplication.instance()
        if app is not None and self._registered:
            app.installNativeEventFilter(self)

    def _dispatch(self, hotkey_id: int) -> bool:
        """Invoke the action bound to a fired hotkey id. Returns True if handled."""
        action_id = self._registered.get(hotkey_id)
        if action_id is None:
            return False
        invoke(action_id)
        return True

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        # Runs for every native message — keep it allocation-light and early-out.
        if not message or _wintypes is None or event_type != b"windows_generic_MSG":
            return False, 0
        try:
            msg = _wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        # _dispatch/invoke run outside the try so a real handler bug isn't swallowed.
        if msg.message == WM_HOTKEY and self._dispatch(int(msg.wParam)):
            return True, 0
        return False, 0

    def close(self) -> None:
        for hotkey_id in list(self._registered):
            try:
                self._unregister_fn(hotkey_id)
            except Exception:
                pass
        self._registered.clear()
        app = QApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self)
