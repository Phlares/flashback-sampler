"""
Tests for the Windows per-process WASAPI loopback backend.

The actual native COM / ctypes calls can't be exercised on non-
Windows or without a real audio-producing target process. We verify:

1. GUID string parsing round-trips.
2. GUID.equals is true for same value and false for different.
3. AUDIOCLIENT_ACTIVATION_PARAMS struct packs to the expected size.
4. is_supported() returns False on non-Windows.
5. ProcessLoopbackCapture construction on non-Windows raises
   RuntimeError with a clean message.
6. build_capture_source dispatch routes kind="process_loopback" to
   ProcessLoopbackCapture (with a stub buffer).
7. enumerate_audio_processes returns [] on non-Windows.
"""

from __future__ import annotations

import sys

import pytest

from flashback_sampler.io.win32_process_loopback import (
    AUDIOCLIENT_ACTIVATION_PARAMS,
    GUID,
    IID_IActivateAudioInterfaceCompletionHandler,
    IID_IAudioClient,
    IID_IUnknown,
    ProcessLoopbackCapture,
    enumerate_audio_processes,
    is_supported,
)


# ─────────────────────────────────────────────────────────────────────────
# GUID parsing / equality
# ─────────────────────────────────────────────────────────────────────────


def test_guid_from_string_round_trips_iid_unknown():
    g = GUID.from_string("{00000000-0000-0000-C000-000000000046}")
    assert g.Data1 == 0x00000000
    assert g.Data2 == 0x0000
    assert g.Data3 == 0x0000
    # Data4 is a big-endian array of 8 bytes; the high word is 0xC000,
    # the low 6 bytes are 00 00 00 00 00 46
    expected = [0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46]
    for i in range(8):
        assert g.Data4[i] == expected[i]


def test_guid_from_string_accepts_unbraced():
    g = GUID.from_string("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
    assert g.Data1 == 0x1CB9AD4C
    assert g.Data2 == 0xDBFA
    assert g.Data3 == 0x4C32


def test_guid_from_string_rejects_garbage():
    with pytest.raises(ValueError):
        GUID.from_string("not-a-guid")


def test_guid_equals_self():
    g = GUID.from_string("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    g2 = GUID.from_string("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
    assert g.equals(g2)


def test_guid_not_equals_different():
    a = IID_IUnknown
    b = IID_IAudioClient
    assert not a.equals(b)
    assert not b.equals(a)


def test_guid_equals_none_or_wrong_type():
    g = IID_IUnknown
    assert g.equals(None) is False
    assert g.equals("not a GUID") is False


# ─────────────────────────────────────────────────────────────────────────
# AUDIOCLIENT_ACTIVATION_PARAMS struct
# ─────────────────────────────────────────────────────────────────────────


def test_activation_params_struct_layout_sane():
    """
    The struct contains a DWORD ActivationType and a union with
    ProcessLoopbackParams (two DWORDs). Minimum size is 12 bytes
    (could be larger due to alignment).
    """
    import ctypes
    size = ctypes.sizeof(AUDIOCLIENT_ACTIVATION_PARAMS)
    assert size >= 12
    assert size <= 32  # reasonable upper bound


def test_activation_params_can_be_populated():
    from flashback_sampler.io.win32_process_loopback import (
        AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
    )
    p = AUDIOCLIENT_ACTIVATION_PARAMS()
    p.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    p.ProcessLoopbackParams.TargetProcessId = 12345
    p.ProcessLoopbackParams.ProcessLoopbackMode = (
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
    )
    assert p.ActivationType == 1
    assert p.ProcessLoopbackParams.TargetProcessId == 12345
    assert p.ProcessLoopbackParams.ProcessLoopbackMode == 0


# ─────────────────────────────────────────────────────────────────────────
# Platform gate + enumeration
# ─────────────────────────────────────────────────────────────────────────


def test_is_supported_matches_platform():
    if sys.platform == "win32":
        # Can't know the build without running on a real machine,
        # but the function should return a bool.
        assert isinstance(is_supported(), bool)
    else:
        assert is_supported() is False


def test_enumerate_audio_processes_empty_on_non_windows():
    if sys.platform != "win32":
        assert enumerate_audio_processes() == []


def test_enumerate_audio_processes_returns_tuples_on_windows():
    if sys.platform != "win32":
        pytest.skip("Windows-only")
    procs = enumerate_audio_processes()
    assert isinstance(procs, list)
    # Should have at least a few processes running
    if procs:
        pid, name = procs[0]
        assert isinstance(pid, int)
        assert isinstance(name, str)


# ─────────────────────────────────────────────────────────────────────────
# ProcessLoopbackCapture construction + dispatch
# ─────────────────────────────────────────────────────────────────────────


def test_process_loopback_capture_raises_on_non_windows():
    if sys.platform == "win32":
        pytest.skip("Windows path — tested separately")

    class FakeBuffer:
        pass

    with pytest.raises(RuntimeError, match="Windows"):
        ProcessLoopbackCapture(
            buffer=FakeBuffer(), pid=1234, sample_rate=48_000, channels=2
        )


def test_build_capture_source_routes_process_loopback():
    """
    Even on non-Windows, build_capture_source should attempt to
    instantiate the ProcessLoopbackCapture — and the attempt raises
    RuntimeError with the Windows-only message. We verify the
    dispatch reaches that code path.
    """
    from flashback_sampler.app.audio_devices import (
        CaptureDevice,
        build_capture_source,
    )

    class FakeBuffer:
        pass

    dev = CaptureDevice(
        kind="process_loopback", name="notepad.exe (pid 1234)", id="1234"
    )
    if sys.platform == "win32":
        # On Windows we should actually get a capture source object.
        src = build_capture_source(dev, FakeBuffer(), 48_000, 2)
        assert isinstance(src, ProcessLoopbackCapture)
        assert src.pid == 1234
    else:
        with pytest.raises(RuntimeError, match="Windows"):
            build_capture_source(dev, FakeBuffer(), 48_000, 2)


def test_build_capture_source_rejects_non_integer_pid():
    from flashback_sampler.app.audio_devices import (
        CaptureDevice,
        build_capture_source,
    )

    class FakeBuffer:
        pass

    dev = CaptureDevice(
        kind="process_loopback", name="x", id="not-an-int"
    )
    with pytest.raises(ValueError, match="integer PID"):
        build_capture_source(dev, FakeBuffer(), 48_000, 2)
