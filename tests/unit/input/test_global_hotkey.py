"""Pure parsing for Win32 global hotkeys (no ctypes / OS calls here)."""

from __future__ import annotations

import pytest

from flashback_sampler.input.sources.global_hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    parse_hotkey,
)


def test_modifier_plus_letter():
    mods, vk = parse_hotkey("Ctrl+Alt+O")
    assert mods == (MOD_CONTROL | MOD_ALT | MOD_NOREPEAT)
    assert vk == ord("O")


def test_single_modifier():
    mods, vk = parse_hotkey("Alt+R")
    assert mods == (MOD_ALT | MOD_NOREPEAT)
    assert vk == ord("R")


def test_shift_combo():
    mods, vk = parse_hotkey("Ctrl+Shift+C")
    assert mods == (MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT)
    assert vk == ord("C")


def test_function_key():
    mods, vk = parse_hotkey("Alt+F5")
    assert mods == (MOD_ALT | MOD_NOREPEAT)
    assert vk == 0x74  # VK_F5


def test_digit():
    mods, vk = parse_hotkey("Ctrl+Alt+1")
    assert vk == ord("1")


def test_modified_space_is_ok():
    mods, vk = parse_hotkey("Ctrl+Space")
    assert mods == (MOD_CONTROL | MOD_NOREPEAT)
    assert vk == 0x20


@pytest.mark.parametrize("chord", ["Space", "F5", "A", "", "Ctrl", "Ctrl+Shift"])
def test_unregisterable_returns_none(chord):
    # Bare keys (no modifier) and modifier-only chords can't be global hotkeys.
    assert parse_hotkey(chord) is None


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# --- build_global_bindings: derive global hotkeys from the live BindingTable -

def test_build_global_bindings_tracks_current_bindings():
    from flashback_sampler.input.core import Action, BindingTable, register
    from flashback_sampler.input.core.actions import clear_registry
    from flashback_sampler.input.sources.global_hotkey import build_global_bindings

    clear_registry()
    register(Action(id="rec", name="Rec", category="T", callable=lambda: None,
                    default_binding="Ctrl+Alt+R", is_global=True))
    register(Action(id="co", name="Checkout", category="T", callable=lambda: None,
                    default_binding="Ctrl+Alt+O", is_global=True))
    # A non-global action with a modifier chord must NOT be globalized.
    register(Action(id="local", name="Local", category="T", callable=lambda: None,
                    default_binding="Ctrl+Alt+L", is_global=False))
    table = BindingTable()

    assert build_global_bindings(table) == {
        "Ctrl+Alt+R": "rec", "Ctrl+Alt+O": "co"}

    # Rebind the record action — the global map must follow.
    table.bind("Ctrl+Alt+G", "rec")
    out = build_global_bindings(table)
    assert out["Ctrl+Alt+G"] == "rec"
    assert "Ctrl+Alt+R" not in out
    clear_registry()


def test_build_global_bindings_skips_bare_keys():
    # A global action rebound to a modifier-less key can't be a Win32 global
    # hotkey; it must be dropped rather than crash.
    from flashback_sampler.input.core import Action, BindingTable, register
    from flashback_sampler.input.core.actions import clear_registry
    from flashback_sampler.input.sources.global_hotkey import build_global_bindings

    clear_registry()
    register(Action(id="rec", name="Rec", category="T", callable=lambda: None,
                    default_binding="F13", is_global=True))
    table = BindingTable()
    assert build_global_bindings(table) == {}
    clear_registry()


def test_source_registers_only_parseable_bindings(qapp):
    from flashback_sampler.input.core import Action, register
    from flashback_sampler.input.sources.global_hotkey import GlobalHotkeySource
    register(Action(id="clip.checkout", name="C", category="Clip", callable=lambda: None))
    calls = []
    src = GlobalHotkeySource(
        {"Ctrl+Alt+O": "clip.checkout", "Space": "x"},  # Space can't be global
        hwnd=4242,
        register_fn=lambda hwnd, hid, mods, vk: calls.append((hwnd, mods, vk)) or True,
        unregister_fn=lambda hwnd, hid: None,
    )
    assert len(calls) == 1
    assert calls[0] == (4242, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("O"))
    assert len(src._registered) == 1


def test_source_dispatch_invokes_bound_action(qapp):
    from flashback_sampler.input.core import Action, register
    from flashback_sampler.input.sources.global_hotkey import GlobalHotkeySource
    fired = []
    register(Action(id="clip.checkout", name="C", category="Clip",
                    callable=lambda: fired.append(1)))
    src = GlobalHotkeySource(
        {"Ctrl+Alt+O": "clip.checkout"}, hwnd=1,
        register_fn=lambda *a: True, unregister_fn=lambda *a: None,
    )
    hid = next(iter(src._registered))
    assert src._dispatch(hid) is True
    assert fired == [1]
    assert src._dispatch(99999) is False  # unknown id → no-op


def test_source_close_unregisters_all(qapp):
    from flashback_sampler.input.sources.global_hotkey import GlobalHotkeySource
    from flashback_sampler.input.core import Action, register
    register(Action(id="clip.checkout", name="C", category="Clip", callable=lambda: None))
    removed = []
    src = GlobalHotkeySource(
        {"Ctrl+Alt+O": "clip.checkout"}, hwnd=7,
        register_fn=lambda *a: True, unregister_fn=lambda hwnd, hid: removed.append((hwnd, hid)),
    )
    n = len(src._registered)
    src.close()
    assert len(removed) == n and not src._registered
    assert removed[0][0] == 7  # unregistered against the same hwnd


def test_source_is_single_inheritance_native_filter(qapp):
    # REGRESSION: PySide6 silently fails to wire up the nativeEventFilter
    # virtual override when the class also inherits QObject (multiple
    # inheritance with QObject primary). The C++ event loop then never calls
    # back into Python, so registered hotkeys fire but nothing happens — and
    # unit tests that call nativeEventFilter() directly still pass, masking it.
    # The filter object MUST be a plain QAbstractNativeEventFilter subclass.
    from PySide6.QtCore import QAbstractNativeEventFilter, QObject

    from flashback_sampler.input.sources.global_hotkey import GlobalHotkeySource

    mro = GlobalHotkeySource.__mro__
    assert QAbstractNativeEventFilter in mro
    assert QObject not in mro  # QObject breaks the native dispatch — keep it out


def test_native_event_filter_dispatches_despite_falsy_pointer(qapp):
    # REGRESSION: PySide6 hands nativeEventFilter a sip.voidptr that is *falsy*
    # even when it wraps a valid non-null pointer. A `if not message:` guard
    # therefore drops every WM_HOTKEY. Reproduce with a falsy stand-in that
    # still yields a valid MSG address, and assert the bound action still fires.
    import ctypes
    from ctypes import wintypes

    from flashback_sampler.input.core import Action, register
    from flashback_sampler.input.sources.global_hotkey import (
        WM_HOTKEY,
        GlobalHotkeySource,
    )

    fired = []
    register(Action(id="clip.checkout", name="C", category="Clip",
                    callable=lambda: fired.append(1)))
    src = GlobalHotkeySource(
        {"Ctrl+Alt+O": "clip.checkout"}, hwnd=1,
        register_fn=lambda *a: True, unregister_fn=lambda *a: None,
    )
    hid = next(iter(src._registered))
    msg = wintypes.MSG()
    msg.message = WM_HOTKEY
    msg.wParam = hid

    class FalsyPtr:  # mimics sip.voidptr: falsy, but a valid address
        def __bool__(self):
            return False

        def __int__(self):
            return ctypes.addressof(msg)

    handled, _ = src.nativeEventFilter(b"windows_generic_MSG", FalsyPtr())
    assert handled is True
    assert fired == [1]


def test_failed_registration_is_skipped(qapp):
    from flashback_sampler.input.sources.global_hotkey import GlobalHotkeySource
    from flashback_sampler.input.core import Action, register
    register(Action(id="clip.checkout", name="C", category="Clip", callable=lambda: None))
    src = GlobalHotkeySource(
        {"Ctrl+Alt+O": "clip.checkout"}, hwnd=0,
        register_fn=lambda *a: False,  # combo already taken by another app
        unregister_fn=lambda *a: None,
    )
    assert src._registered == {}  # nothing registered, no crash
