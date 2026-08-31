"""
CaptureSource Protocol conformance tests.

Verifies that NativeCaptureSource structurally satisfies the Protocol,
and that the fake capture sources under tests/fixtures/fake_capture.py
also conform.
"""

from __future__ import annotations

import pytest

from flashback_sampler.core.native import NativeAudioCircularBuffer
from flashback_sampler.core.capture_source import CaptureSource


def _minimal_buffer() -> NativeAudioCircularBuffer:
    return NativeAudioCircularBuffer(duration_seconds=0.5, sample_rate=1000, channels=1)


# ─────────────────────────────────────────────────────────────────────────
# Fake sources (the main fixtures for M10.2+ tests)
# ─────────────────────────────────────────────────────────────────────────


def test_fake_no_thread_source_conforms_to_capture_source():
    from tests.fixtures.fake_capture import FakeCaptureSourceNoThread
    src = FakeCaptureSourceNoThread(_minimal_buffer(), sample_rate=1000, channels=1)
    assert isinstance(src, CaptureSource)


def test_silence_source_conforms_to_capture_source():
    from tests.fixtures.fake_capture import SilenceCaptureSource
    src = SilenceCaptureSource(_minimal_buffer(), sample_rate=1000, channels=1)
    assert isinstance(src, CaptureSource)


def test_fake_no_thread_start_stop_updates_is_running():
    from tests.fixtures.fake_capture import FakeCaptureSourceNoThread
    src = FakeCaptureSourceNoThread(_minimal_buffer(), sample_rate=1000, channels=1)
    assert src.is_running() is False
    src.start()
    assert src.is_running() is True
    src.stop()
    assert src.is_running() is False


def test_fake_no_thread_fill_writes_to_buffer():
    from tests.fixtures.fake_capture import FakeCaptureSourceNoThread
    buf = NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    src = FakeCaptureSourceNoThread(buf, sample_rate=1000, channels=1)
    src.start()
    src.fill(0.5)  # 500 samples
    assert buf.total_written == 500
    src.stop()


def test_fake_no_thread_xrun_counter_starts_at_zero_and_increments():
    from tests.fixtures.fake_capture import FakeCaptureSourceNoThread
    src = FakeCaptureSourceNoThread(_minimal_buffer(), sample_rate=1000, channels=1)
    assert src.xrun_count() == 0
    src.bump_xrun()
    src.bump_xrun()
    assert src.xrun_count() == 2


# ─────────────────────────────────────────────────────────────────────────
# Concrete backends — structural check only (don't start a real stream)
# ─────────────────────────────────────────────────────────────────────────


def test_native_capture_source_conforms_without_starting(monkeypatch):
    from flashback_sampler.core import native
    from flashback_sampler.core.native_capture import NativeCaptureSource

    class _Lib:
        def __getattr__(self, name):
            return lambda *a: 1 if name == "fb_capture_create" else None

    monkeypatch.setattr(native, "_lib", _Lib())
    monkeypatch.setattr(native, "_lib_tried", True)

    class _Buf:
        _h = 1

    src = NativeCaptureSource(_Buf(), kind="loopback")
    assert isinstance(src, CaptureSource)
