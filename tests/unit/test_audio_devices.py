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

import flashback_sampler.app.audio_devices as audio_devices
from flashback_sampler.app.audio_devices import (
    DEFAULT_LOOPBACK,
    CaptureDevice,
    OutputDevice,
    build_capture_source,
    default_capture_device,
    list_capture_devices,
    list_output_devices,
)


class _FakeBuffer:
    sample_rate = 48_000
    channels = 2


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


def test_build_capture_source_rejects_unknown_kind():
    # Use object.__setattr__ to bypass frozen dataclass
    dev = CaptureDevice(kind="loopback", name="x", id="x")
    object.__setattr__(dev, "kind", "wtf")
    with pytest.raises(ValueError, match="unknown"):
        build_capture_source(dev, buffer=_FakeBuffer(), sample_rate=48_000, channels=2)


# ─────────────────────────────────────────────────────────────────────────
# "Follow the OS default output" loopback (the silent-Realtek-endpoint fix)
# ─────────────────────────────────────────────────────────────────────────


def test_default_loopback_sentinel_follows_live_os_default(monkeypatch):
    """The default-output loopback (empty id) must build a NativeCaptureSource
    with device_id="" so the Zig side follows the live OS default endpoint —
    NOT a frozen device id that goes silent when the default changes."""
    import flashback_sampler.app.audio_devices as ad

    seen = {}

    class _Src:
        def __init__(self, buffer, kind, device_id="", pid=0, sample_rate=48_000, channels=2):
            seen.update(device_id=device_id)

    monkeypatch.setattr(ad, "NativeCaptureSource", _Src)
    build_capture_source(
        DEFAULT_LOOPBACK, buffer=_FakeBuffer(), sample_rate=48_000, channels=2
    )
    assert seen["device_id"] == ""


def test_named_loopback_still_pins_to_that_speaker(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    seen = {}

    class _Src:
        def __init__(self, buffer, kind, device_id="", pid=0, sample_rate=48_000, channels=2):
            seen.update(device_id=device_id)

    monkeypatch.setattr(ad, "NativeCaptureSource", _Src)
    dev = CaptureDevice(kind="loopback", name="Speakers (X)  [loopback]", id="{spk-x}")
    build_capture_source(
        dev, buffer=_FakeBuffer(), sample_rate=48_000, channels=2
    )
    assert seen["device_id"] == dev.id


def test_default_capture_device_matches_available_devices():
    # Precise (non-vacuous) on every platform: the choice depends on what's
    # actually enumerable, not just sys.platform.
    devices = list_capture_devices()
    dev = default_capture_device()
    if any(d.kind == "loopback" for d in devices):
        assert dev is DEFAULT_LOOPBACK and dev.follow_default
    elif devices:
        assert dev == devices[0]
    else:
        assert dev is None


def test_default_prefers_dynamic_loopback_when_present(monkeypatch):
    import flashback_sampler.app.audio_devices as ad
    lb = CaptureDevice(kind="loopback", name="Spk  [loopback]", id="Spk")
    mic = CaptureDevice(kind="input", name="Mic", id="0")
    monkeypatch.setattr(ad, "list_capture_devices", lambda: [lb, mic])
    assert ad.default_capture_device() is ad.DEFAULT_LOOPBACK


def test_default_falls_back_to_first_device_when_no_loopback(monkeypatch):
    # Windows-with-no-working-loopback (or any no-loopback host) must not hand
    # back an unopenable sentinel — fall back to the first real device.
    import flashback_sampler.app.audio_devices as ad
    mic = CaptureDevice(kind="input", name="Mic", id="0", is_default=True)
    monkeypatch.setattr(ad, "list_capture_devices", lambda: [mic])
    assert ad.default_capture_device() == mic


def test_default_returns_none_when_no_devices(monkeypatch):
    import flashback_sampler.app.audio_devices as ad
    monkeypatch.setattr(ad, "list_capture_devices", lambda: [])
    assert ad.default_capture_device() is None


def _fake_devices():
    return [
        {"kind": "loopback", "is_default": True, "mix_rate": 48_000, "mix_channels": 2, "id": "{spk}", "name": "Speakers"},
        {"kind": "loopback", "is_default": False, "mix_rate": 96_000, "mix_channels": 2, "id": "{hp}", "name": "Headphones"},
        {"kind": "input", "is_default": True, "mix_rate": 44_100, "mix_channels": 1, "id": "{mic}", "name": "Mic"},
    ]


def test_list_capture_devices_maps_native_list(monkeypatch):
    monkeypatch.setattr(audio_devices.native, "list_devices", _fake_devices)
    devs = audio_devices.list_capture_devices()
    kinds = [(d.kind, d.id, d.mix_rate, d.is_default) for d in devs]
    assert ("loopback", "{spk}", 48_000, True) in kinds
    assert ("input", "{mic}", 44_100, True) in kinds
    assert all(d.name.endswith("[loopback]") for d in devs if d.kind == "loopback")


# ─────────────────────────────────────────────────────────────────────────
# Rate probe
# ─────────────────────────────────────────────────────────────────────────


def test_probe_over_mix_rate_falls_back_for_loopback_and_input(monkeypatch):
    for kind in ("loopback", "input"):
        dev = audio_devices.CaptureDevice(kind=kind, name="X", id="{x}", mix_rate=48_000)
        r = audio_devices.probe_capture_rate(dev, 96_000, 2)
        assert not r.ok and r.effective_rate == 48_000 and "48000 Hz" in r.message


def test_probe_at_or_below_mix_rate_ok():
    dev = audio_devices.CaptureDevice(kind="loopback", name="X", id="{x}", mix_rate=48_000)
    assert audio_devices.probe_capture_rate(dev, 48_000, 2).ok
    assert audio_devices.probe_capture_rate(dev, 44_100, 2).ok


def test_probe_unknown_mix_rate_is_permissive():
    dev = audio_devices.CaptureDevice(kind="input", name="X", id="{x}")
    assert audio_devices.probe_capture_rate(dev, 192_000, 2).ok


def test_build_capture_source_loopback_and_input_use_native(monkeypatch):
    seen = {}

    class _Src:
        def __init__(self, buffer, kind, device_id="", pid=0, sample_rate=48_000, channels=2):
            seen.update(kind=kind, device_id=device_id, pid=pid, sample_rate=sample_rate, channels=channels)

    monkeypatch.setattr(audio_devices, "NativeCaptureSource", _Src)
    audio_devices.build_capture_source(audio_devices.DEFAULT_LOOPBACK, _FakeBuffer(), 48_000, 2)
    assert seen == dict(kind="loopback", device_id="", pid=0, sample_rate=48_000, channels=2)
    audio_devices.build_capture_source(audio_devices.CaptureDevice(kind="input", name="Mic", id="{mic}"), _FakeBuffer(), 44_100, 1)
    assert seen == dict(kind="input", device_id="{mic}", pid=0, sample_rate=44_100, channels=1)


def test_apply_rate_probe_rebuilds_preset():
    from flashback_sampler.core.quality_presets import QualityPreset

    dev = audio_devices.CaptureDevice(kind="loopback", name="X", id="{x}", mix_rate=48_000)
    preset = QualityPreset(
        name="CUSTOM", sample_rate=96000, channels=2, buffer_seconds=300.0
    )
    adjusted, notice = audio_devices.apply_rate_probe(preset, dev)
    assert adjusted.sample_rate == 48000
    assert adjusted.buffer_seconds == 300.0 and adjusted.channels == 2
    assert notice is not None

    ok_preset = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2, buffer_seconds=300.0
    )
    same, none_notice = audio_devices.apply_rate_probe(ok_preset, dev)
    assert same is ok_preset and none_notice is None
