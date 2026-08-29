"""
Tests for per-process WASAPI loopback on the native (Zig) backend.

The GUID / struct-layout tests that used to live here tested the ctypes
COM port; the Zig side now owns those layouts and tests them directly.
"""

from __future__ import annotations

import pytest

import flashback_sampler.app.audio_devices as audio_devices
from flashback_sampler.core import native


class _FakeBuffer:  # mirrors tests/unit/test_audio_devices.py
    _h = 0xB0B
    channels = 2
    sample_rate = 48_000


def test_is_supported_matches_platform(monkeypatch):
    from flashback_sampler.core import native_capture as nc
    monkeypatch.setattr(nc.sys, "platform", "linux")
    assert nc.is_process_loopback_supported() is False


def test_list_processes_empty_without_library(monkeypatch):
    from flashback_sampler.core import native
    monkeypatch.setattr(native, "_lib", None)
    monkeypatch.setattr(native, "_lib_tried", True)
    assert native.list_processes() == []


def test_build_capture_source_routes_process_loopback(monkeypatch):
    seen = {}

    class _Src:
        def __init__(self, buffer, kind, device_id="", pid=0, sample_rate=48_000, channels=2):
            seen.update(kind=kind, pid=pid, sample_rate=sample_rate, channels=channels)

    monkeypatch.setattr(audio_devices, "NativeCaptureSource", _Src)
    dev = audio_devices.CaptureDevice(kind="process_loopback", name="game.exe", id="4242")
    audio_devices.build_capture_source(dev, _FakeBuffer(), 48_000, 2)
    assert seen == dict(kind="process", pid=4242, sample_rate=48_000, channels=2)


def test_build_capture_source_rejects_non_integer_pid():
    dev = audio_devices.CaptureDevice(kind="process_loopback", name="x", id="nope")
    with pytest.raises(ValueError):
        audio_devices.build_capture_source(dev, _FakeBuffer(), 48_000, 2)


def test_resolve_root_pid_walks_same_named_chain(monkeypatch):
    # spotify 300 -> 200 -> 100 (all same exe); 100's parent is explorer.
    entries = [(1, 0, "explorer.exe"), (100, 1, "spotify.exe"),
               (200, 100, "spotify.exe"), (300, 200, "spotify.exe")]
    monkeypatch.setattr(native, "_process_entries", lambda *a: entries)
    assert native.resolve_root_pid(300) == 100
    assert native.resolve_root_pid(100) == 100


def test_resolve_root_pid_unknown_or_broken_chain_is_identity(monkeypatch):
    entries = [(200, 999, "game.exe")]  # parent absent from snapshot
    monkeypatch.setattr(native, "_process_entries", lambda *a: entries)
    assert native.resolve_root_pid(200) == 200
    assert native.resolve_root_pid(555) == 555
