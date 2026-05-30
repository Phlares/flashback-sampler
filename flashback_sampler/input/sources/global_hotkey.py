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

from PySide6.QtCore import QAbstractNativeEventFilter
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


def build_global_bindings(table) -> dict[str, str]:
    """``{chord: action_id}`` for every global-capable action whose CURRENT
    binding (user override or default) is a modifier-qualified chord.

    Deriving this from the live ``BindingTable`` — instead of a static map —
    means a record/checkout key the user rebinds in the Keybindings dialog
    moves its global hotkey to match, so the same key behaves identically
    whether the window is focused or minimized. Bare-key bindings are skipped
    (Win32 can't register a global hotkey without a modifier).
    """
    from flashback_sampler.input.core import all_actions  # local: avoid cycle

    out: dict[str, str] = {}
    for a in all_actions():
        if not a.is_global:
            continue
        code = table.binding_for(a.id)
        if code and parse_hotkey(code) is not None:
            out[code] = a.id
    return out


def _win_register(hwnd: int, hotkey_id: int, mods: int, vk: int) -> bool:
    # Register to a real window HWND (not NULL): WM_HOTKEY is then a window
    # message Qt delivers reliably via the native filter, even while the window
    # is minimized/hidden. argtypes are mandatory — without HWND typing the
    # 64-bit handle gets truncated to 32 bits and RegisterHotKey targets nothing.
    user32 = ctypes.windll.user32
    user32.RegisterHotKey.argtypes = [
        _wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
    ]
    user32.RegisterHotKey.restype = _wintypes.BOOL
    return bool(user32.RegisterHotKey(_wintypes.HWND(hwnd), hotkey_id, mods, vk))


def _win_unregister(hwnd: int, hotkey_id: int) -> None:
    user32 = ctypes.windll.user32
    user32.UnregisterHotKey.argtypes = [_wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey(_wintypes.HWND(hwnd), hotkey_id)


class GlobalHotkeySource(QAbstractNativeEventFilter):
    """Registers global hotkeys for ``{chord: action_id}`` and routes WM_HOTKEY
    back through ``invoke``. OS calls are injectable for testing.

    NOTE: single inheritance is load-bearing. PySide6 silently drops the
    ``nativeEventFilter`` override if this also inherits ``QObject`` (multiple
    inheritance with QObject primary) — the C++ loop then never calls back into
    Python and hotkeys register but never fire. Lifetime is managed explicitly
    via ``close()`` (removes the filter + releases OS registrations), so no
    QObject parenting is needed.
    """

    def __init__(
        self,
        bindings: dict[str, str],
        hwnd: int,
        *,
        register_fn: Callable[[int, int, int, int], bool] = _win_register,
        unregister_fn: Callable[[int, int], None] = _win_unregister,
    ) -> None:
        super().__init__()
        self._hwnd = hwnd
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
                ok = self._register_fn(hwnd, next_id, mods, vk)
            except Exception:
                ok = False
            if ok:
                self._registered[next_id] = action_id
                next_id += 1
        app = QApplication.instance()
        if app is not None and self._registered:
            app.installNativeEventFilter(self)

    @property
    def registered_count(self) -> int:
        """Number of hotkeys successfully registered with the OS. A named
        property rather than ``__len__`` so the object's truthiness stays a
        plain 'does it exist' check (callers use ``is not None``)."""
        return len(self._registered)

    def _dispatch(self, hotkey_id: int) -> bool:
        """Invoke the action bound to a fired hotkey id. Returns True if handled."""
        action_id = self._registered.get(hotkey_id)
        if action_id is None:
            return False
        invoke(action_id)
        return True

    # Registering to a real window HWND means WM_HOTKEY arrives as a window
    # message, which Qt's event loop filters as "windows_generic_MSG"
    # (dispatcher_MSG is kept too, harmlessly, for the NULL-hwnd path).
    _MSG_TYPES = (b"windows_generic_MSG", b"windows_dispatcher_MSG")

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        # Runs for every native message — keep it allocation-light and early-out.
        # NOTE: do NOT guard on `not message` — PySide6 hands us a sip.voidptr
        # that is falsy even when it wraps a valid non-null pointer, so that
        # check would drop every event. A bad/null pointer is caught by the
        # from_address try/except below instead.
        if _wintypes is None or event_type not in self._MSG_TYPES:
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
                self._unregister_fn(self._hwnd, hotkey_id)
            except Exception:
                pass
        self._registered.clear()
        app = QApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self)
