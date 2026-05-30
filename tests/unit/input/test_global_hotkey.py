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
