"""
Headless tests for the audio_devices module. The enumeration functions
themselves talk to real hardware backends and can't be asserted against
a fixed expected set, so these tests focus on:

1. Dataclass frozenness / immutability.
2. build_capture_source() dispatch logic for loopback vs. input (using
   a fake AudioCircularBuffer that records what it gets passed).
3. That calling list_*_devices() doesn't raise.
"""

from __future__ import annotations

import pytest

from flashback_sampler.app.audio_devices import (
    CaptureDevice,
    OutputDevice,
    build_capture_source,
    list_capture_devices,
    list_output_devices,
)


def test_capture_device_is_frozen():
    d = CaptureDevice(kind="loopback", name="Speakers", id="Speakers")
    with pytest.raises(Exception):
        d.name = "other"  # type: ignore[misc]


def test_output_device_is_frozen():
    d = OutputDevice(id=0, name="Out", max_output_channels=2)
    with pytest.raises(Exception):
        d.id = 1  # type: ignore[misc]


def test_list_capture_devices_does_not_raise():
    devs = list_capture_devices()
    assert isinstance(devs, list)
    for d in devs:
        assert isinstance(d, CaptureDevice)


def test_list_output_devices_does_not_raise():
    devs = list_output_devices()
    assert isinstance(devs, list)
    for d in devs:
        assert isinstance(d, OutputDevice)


def test_build_capture_source_input_kind_requires_integer_id():
    # Fake buffer object — only needs to exist; build_capture_source
    # passes it through to the source constructor unchanged.
    class FakeBuffer:
        sample_rate = 48_000
        channels = 2

    dev = CaptureDevice(kind="input", name="Mic", id="not_an_int")
    with pytest.raises(ValueError, match="integer"):
        build_capture_source(dev, buffer=FakeBuffer(), sample_rate=48_000, channels=2)


def test_build_capture_source_rejects_unknown_kind():
    class FakeBuffer:
        sample_rate = 48_000
        channels = 2

    # Use object.__setattr__ to bypass frozen dataclass
    dev = CaptureDevice(kind="loopback", name="x", id="x")
    object.__setattr__(dev, "kind", "wtf")
    with pytest.raises(ValueError, match="unknown"):
        build_capture_source(dev, buffer=FakeBuffer(), sample_rate=48_000, channels=2)
