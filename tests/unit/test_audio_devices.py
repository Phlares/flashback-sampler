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


def test_build_capture_source_input_kind_requires_integer_id():
    dev = CaptureDevice(kind="input", name="Mic", id="not_an_int")
    with pytest.raises(ValueError, match="integer"):
        build_capture_source(dev, buffer=_FakeBuffer(), sample_rate=48_000, channels=2)


def test_build_capture_source_rejects_unknown_kind():
    # Use object.__setattr__ to bypass frozen dataclass
    dev = CaptureDevice(kind="loopback", name="x", id="x")
    object.__setattr__(dev, "kind", "wtf")
    with pytest.raises(ValueError, match="unknown"):
        build_capture_source(dev, buffer=_FakeBuffer(), sample_rate=48_000, channels=2)


# ─────────────────────────────────────────────────────────────────────────
# "Follow the OS default output" loopback (the silent-Realtek-endpoint fix)
# ─────────────────────────────────────────────────────────────────────────


def test_default_loopback_sentinel_follows_live_os_default():
    """The default-output loopback (empty id) must build a LoopbackCapture
    with speaker_name=None so it resolves sc.default_speaker() at start —
    NOT a frozen device name that goes silent when the default changes."""
    cap = build_capture_source(
        DEFAULT_LOOPBACK, buffer=_FakeBuffer(), sample_rate=48_000, channels=2
    )
    assert cap.speaker_name is None


def test_named_loopback_still_pins_to_that_speaker():
    dev = CaptureDevice(kind="loopback", name="Speakers (X)  [loopback]", id="Speakers (X)")
    cap = build_capture_source(
        dev, buffer=_FakeBuffer(), sample_rate=48_000, channels=2
    )
    assert cap.speaker_name == "Speakers (X)"


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


# ─────────────────────────────────────────────────────────────────────────
# Rate probe
# ─────────────────────────────────────────────────────────────────────────


def test_probe_input_ok(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    monkeypatch.setattr(
        ad.sd, "check_input_settings", lambda **kw: None, raising=False
    )
    dev = ad.CaptureDevice(kind="input", name="Mic", id="3")
    res = ad.probe_capture_rate(dev, 96000, 2)
    assert res.ok and res.effective_rate == 96000


def test_probe_input_falls_back_to_device_default(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    def boom(**kw):
        raise Exception("unsupported")

    monkeypatch.setattr(ad.sd, "check_input_settings", boom, raising=False)
    monkeypatch.setattr(
        ad.sd, "query_devices",
        lambda idx=None, kind=None: {"default_samplerate": 44100.0},
        raising=False,
    )
    dev = ad.CaptureDevice(kind="input", name="Mic", id="3")
    res = ad.probe_capture_rate(dev, 192000, 2)
    assert not res.ok
    assert res.effective_rate == 44100
    assert "192000" in res.message and "44100" in res.message


def test_probe_loopback_over_mix_rate_falls_back(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    monkeypatch.setattr(ad, "_wasapi_output_mix_rate", lambda hint: 48000)
    dev = ad.CaptureDevice(kind="loopback", name="Speakers", id="spk")
    res = ad.probe_capture_rate(dev, 96000, 2)
    assert not res.ok
    assert res.effective_rate == 48000
    assert "24000" in res.message  # honest Nyquist notice


def test_probe_loopback_at_or_below_mix_rate_ok(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    monkeypatch.setattr(ad, "_wasapi_output_mix_rate", lambda hint: 48000)
    dev = ad.CaptureDevice(kind="loopback", name="Speakers", id="spk")
    assert ad.probe_capture_rate(dev, 48000, 2).ok
    assert ad.probe_capture_rate(dev, 16000, 2).ok


def test_probe_loopback_unknown_mix_rate_is_permissive(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    monkeypatch.setattr(ad, "_wasapi_output_mix_rate", lambda hint: None)
    res = ad.probe_capture_rate(None, 96000, 2)
    assert res.ok and res.effective_rate == 96000


def test_probe_input_passes_integer_device_to_check_input_settings(monkeypatch):
    """sounddevice treats a *string* device arg as a name-substring query —
    a numeric-looking string like "3" matches nothing, so every real input
    device would misreport as unsupported. The probe must resolve
    CaptureDevice.id (a string index) to an int before calling in, exactly
    like build_capture_source's `int(device.id)` does."""
    import flashback_sampler.app.audio_devices as ad

    def fake_check_input_settings(*, device, samplerate, channels, dtype):
        if not isinstance(device, int):
            raise TypeError(f"device must be an int, got {device!r}")

    monkeypatch.setattr(
        ad.sd, "check_input_settings", fake_check_input_settings, raising=False
    )
    dev = ad.CaptureDevice(kind="input", name="Mic", id="3")
    res = ad.probe_capture_rate(dev, 96000, 2)
    assert res.ok and res.effective_rate == 96000


def test_probe_input_fallback_passes_integer_device_to_query_devices(monkeypatch):
    import flashback_sampler.app.audio_devices as ad

    def boom(**kw):
        raise Exception("unsupported")

    def fake_query_devices(idx=None, kind=None):
        assert isinstance(idx, int), f"expected int device index, got {idx!r}"
        return {"default_samplerate": 44100.0}

    monkeypatch.setattr(ad.sd, "check_input_settings", boom, raising=False)
    monkeypatch.setattr(ad.sd, "query_devices", fake_query_devices, raising=False)
    dev = ad.CaptureDevice(kind="input", name="Mic", id="3")
    res = ad.probe_capture_rate(dev, 192000, 2)
    assert not res.ok
    assert res.effective_rate == 44100


def test_probe_loopback_hint_strips_loopback_suffix_to_match_real_name(monkeypatch):
    """CaptureDevice.name for a loopback device is built as
    f"{spk.name}  [loopback]" — the raw name never matches a real WASAPI
    output device's name, so the "[loopback]" suffix must be stripped
    before the containment check in _wasapi_output_mix_rate."""
    import flashback_sampler.app.audio_devices as ad

    hostapis = [{"name": "Windows WASAPI", "default_output_device": 5}]
    devices = [
        {  # index 0: the real named device, NOT the hostapi default
            "hostapi": 0, "max_output_channels": 2,
            "name": "Speakers (Realtek(R) Audio)",
            "default_samplerate": 96000.0,
        },
        {"hostapi": 1, "max_output_channels": 0, "name": "Mic", "default_samplerate": 44100.0},
        {"hostapi": 0, "max_output_channels": 0, "name": "Unrelated In", "default_samplerate": 48000.0},
        {"hostapi": 0, "max_output_channels": 2, "name": "Other Out", "default_samplerate": 48000.0},
        {"hostapi": 0, "max_output_channels": 2, "name": "Yet Another Out", "default_samplerate": 48000.0},
        {  # index 5: the hostapi's default output — a DIFFERENT, lower rate
            "hostapi": 0, "max_output_channels": 2,
            "name": "Default Speakers", "default_samplerate": 48000.0,
        },
    ]
    monkeypatch.setattr(ad.sd, "query_hostapis", lambda: hostapis, raising=False)
    monkeypatch.setattr(ad.sd, "query_devices", lambda: devices, raising=False)

    dev = ad.CaptureDevice(
        kind="loopback", name="Speakers (Realtek(R) Audio)  [loopback]", id="spk"
    )
    res = ad.probe_capture_rate(dev, 96000, 2)
    assert res.ok and res.effective_rate == 96000


def test_apply_rate_probe_rebuilds_preset(monkeypatch):
    import flashback_sampler.app.audio_devices as ad
    from flashback_sampler.core.quality_presets import QualityPreset

    monkeypatch.setattr(ad, "_wasapi_output_mix_rate", lambda hint: 48000)
    preset = QualityPreset(
        name="CUSTOM", sample_rate=96000, channels=2, buffer_seconds=300.0
    )
    adjusted, notice = ad.apply_rate_probe(preset, None)
    assert adjusted.sample_rate == 48000
    assert adjusted.buffer_seconds == 300.0 and adjusted.channels == 2
    assert notice is not None

    ok_preset = QualityPreset(
        name="CUSTOM", sample_rate=48000, channels=2, buffer_seconds=300.0
    )
    same, none_notice = ad.apply_rate_probe(ok_preset, None)
    assert same is ok_preset and none_notice is None
