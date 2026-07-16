"""
Enumerate capture and preview-output audio devices.

Unifies the two backends we use — `soundcard` for WASAPI loopback (the
only way to capture system audio on Windows) and `sounddevice` for mic,
line-in, and preview output — under a single pair of dataclass types.

The UI reads these lists into menu/dropdown widgets and passes the
chosen dataclass back through the AppState so controllers can build
the right concrete source object.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

# Optional dependency: every other consumer in this module imports
# sounddevice lazily inside a try/except so the module still loads
# where the backend is absent. The probe helpers below reference it as
# a module attribute (also what tests monkeypatch), so guard the import
# and let the probes' permissive except-blocks absorb `sd = None`.
try:
    import sounddevice as sd
except Exception:  # pragma: no cover - environment without sounddevice
    sd = None  # type: ignore[assignment]

from flashback_sampler.core.quality_presets import QualityPreset
from flashback_sampler.platform.capabilities import loopback_supported


CaptureKind = Literal["loopback", "input", "process_loopback"]


@dataclass(frozen=True)
class CaptureDevice:
    """
    A source that feeds audio into the ring buffer.

    `kind`:
        "loopback" — Windows WASAPI loopback on a speaker (captures
            what that speaker is playing). `id` is the soundcard
            speaker name. The DEFAULT_LOOPBACK sentinel sets
            `follow_default=True` to track the live OS default output
            instead of pinning a specific speaker.
        "input" — a normal sounddevice input (mic, line-in, virtual
            cable). `id` is the sounddevice device index as a string.
        "process_loopback" — Windows 10 2004+ per-process WASAPI
            loopback, captures only the target PID. `id` is the PID
            as a string.
    """
    kind: CaptureKind
    name: str
    id: str
    sample_rate: int = 48_000
    channels: int = 2
    is_default: bool = False
    # Loopback only: track the live OS default output rather than pinning
    # `id` to a specific speaker. Set on the DEFAULT_LOOPBACK sentinel.
    follow_default: bool = False


# Sentinel loopback device that follows the LIVE OS default output. Its empty
# `id` maps to LoopbackCapture(speaker_name=None), which resolves
# sc.default_speaker() at start — so capture follows whatever the user is
# actually hearing, even if the default output changes after launch. Pinning a
# specific named speaker (the old default behaviour) silently records nothing
# when that endpoint isn't the one playing audio.
DEFAULT_LOOPBACK = CaptureDevice(
    kind="loopback", name="Default output  [loopback]", id="",
    is_default=True, follow_default=True,
)


@dataclass(frozen=True)
class OutputDevice:
    """A sounddevice output device used for preview playback."""
    id: int
    name: str
    max_output_channels: int
    is_default: bool = False


# ─────────────────────────────────────────────────────────────────────────
# Enumeration
# ─────────────────────────────────────────────────────────────────────────


def list_capture_devices() -> list[CaptureDevice]:
    """
    Return every available capture source. Loopback devices (Windows
    only) come first, then input devices. The system default of each
    backend is marked.
    """
    devices: list[CaptureDevice] = []

    if loopback_supported():
        devices.extend(_list_loopback_devices())

    devices.extend(_list_input_devices())
    return devices


def _list_loopback_devices() -> list[CaptureDevice]:
    try:
        import soundcard as sc
    except Exception:  # pragma: no cover
        return []

    out: list[CaptureDevice] = []
    try:
        default_name = sc.default_speaker().name
    except Exception:  # pragma: no cover
        default_name = None

    try:
        speakers = sc.all_speakers()
    except Exception:  # pragma: no cover
        return []

    seen: set[str] = set()
    for spk in speakers:
        if spk.name in seen:
            continue
        seen.add(spk.name)
        out.append(
            CaptureDevice(
                kind="loopback",
                name=f"{spk.name}  [loopback]",
                id=spk.name,
                is_default=(spk.name == default_name),
            )
        )
    return out


def _list_input_devices() -> list[CaptureDevice]:
    try:
        import sounddevice as sd
    except Exception:  # pragma: no cover
        return []

    out: list[CaptureDevice] = []
    try:
        all_devs = sd.query_devices()
    except Exception:  # pragma: no cover
        return []

    default_in_name: str | None = None
    try:
        default_in = sd.query_devices(kind="input")
        if isinstance(default_in, dict):
            default_in_name = default_in.get("name")
    except Exception:  # pragma: no cover
        pass

    for i, dev in enumerate(all_devs):
        if not isinstance(dev, dict):
            continue
        if dev.get("max_input_channels", 0) <= 0:
            continue
        name = dev.get("name", f"Input {i}")
        out.append(
            CaptureDevice(
                kind="input",
                name=name,
                id=str(i),
                sample_rate=int(dev.get("default_samplerate", 48_000) or 48_000),
                channels=min(2, int(dev.get("max_input_channels", 2))),
                is_default=(name == default_in_name),
            )
        )
    return out


def list_output_devices() -> list[OutputDevice]:
    """Return every available sounddevice output device."""
    try:
        import sounddevice as sd
    except Exception:  # pragma: no cover
        return []

    out: list[OutputDevice] = []
    try:
        all_devs = sd.query_devices()
    except Exception:  # pragma: no cover
        return []

    default_out_name: str | None = None
    try:
        default_out = sd.query_devices(kind="output")
        if isinstance(default_out, dict):
            default_out_name = default_out.get("name")
    except Exception:  # pragma: no cover
        pass

    for i, dev in enumerate(all_devs):
        if not isinstance(dev, dict):
            continue
        ch = int(dev.get("max_output_channels", 0))
        if ch <= 0:
            continue
        name = dev.get("name", f"Output {i}")
        out.append(
            OutputDevice(
                id=i,
                name=name,
                max_output_channels=ch,
                is_default=(name == default_out_name),
            )
        )
    return out


def default_capture_device() -> CaptureDevice | None:
    # Prefer following the live OS default output (dynamic) over pinning a
    # specific speaker name that can go silent when the default changes — but
    # only when loopback is actually usable (a real loopback device exists).
    # Otherwise fall back to the first available device (e.g. a mic) so a
    # Windows box with no working loopback still gets a usable default.
    devices = list_capture_devices()
    if any(d.kind == "loopback" for d in devices):
        return DEFAULT_LOOPBACK
    return devices[0] if devices else None


def default_output_device() -> OutputDevice | None:
    for d in list_output_devices():
        if d.is_default:
            return d
    devices = list_output_devices()
    return devices[0] if devices else None


# ─────────────────────────────────────────────────────────────────────────
# Spec → concrete source factory
# ─────────────────────────────────────────────────────────────────────────


def build_capture_source(device: CaptureDevice, buffer, sample_rate: int, channels: int):
    """
    Instantiate the right capture-source class for a CaptureDevice.
    `buffer` is an AudioCircularBuffer that the source will write into.
    """
    if device.kind == "loopback":
        from flashback_sampler.core.loopback_capture import LoopbackCapture

        return LoopbackCapture(
            buffer=buffer,
            # follow_default → None → LoopbackCapture resolves the live OS
            # default speaker at start; otherwise pin to the named speaker.
            speaker_name=None if device.follow_default else device.id,
            sample_rate=sample_rate,
            channels=channels,
        )

    if device.kind == "input":
        from flashback_sampler.core.capture import AudioCapture

        try:
            idx = int(device.id)
        except ValueError as e:
            raise ValueError(
                f"input device id must be an integer sounddevice index; "
                f"got {device.id!r}"
            ) from e
        return AudioCapture(
            buffer=buffer,
            device=idx,
            sample_rate=sample_rate,
            channels=channels,
        )

    if device.kind == "process_loopback":
        from flashback_sampler.io.win32_process_loopback import (
            ProcessLoopbackCapture,
        )

        try:
            pid = int(device.id)
        except ValueError as e:
            raise ValueError(
                f"process_loopback device id must be an integer PID; "
                f"got {device.id!r}"
            ) from e
        return ProcessLoopbackCapture(
            buffer=buffer,
            pid=pid,
            sample_rate=sample_rate,
            channels=channels,
        )

    raise ValueError(f"unknown CaptureDevice.kind: {device.kind!r}")


# ─────────────────────────────────────────────────────────────────────────
# Rate probe
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of asking whether a device can honestly deliver a rate."""
    ok: bool
    effective_rate: int
    message: str = ""


def _wasapi_output_mix_rate(name_hint: str | None) -> int | None:
    """
    Shared-mode mix-format rate of the WASAPI output device matching
    `name_hint` (falling back to the default output). PortAudio reports
    a WASAPI output's `default_samplerate` from its mix format, which is
    exactly the rate Windows hands to loopback captures. None = unknown.
    """
    try:
        hostapis = sd.query_hostapis()
        was = next(
            (i for i, h in enumerate(hostapis) if "WASAPI" in h.get("name", "")),
            None,
        )
        if was is None:
            return None
        devices = sd.query_devices()
        outputs = [
            d for d in devices
            if d["hostapi"] == was and d["max_output_channels"] > 0
        ]
        if name_hint:
            # First containment match wins — a heuristic that can pick a
            # sibling device when names overlap (e.g. "Speakers" vs
            # "Speakers 2"). Acceptable: a wrong pick still yields a real
            # mix rate, and the default-output fallback below covers the
            # no-match case.
            hint = name_hint.casefold()
            for d in outputs:
                if hint in d["name"].casefold():
                    return int(d["default_samplerate"])
        didx = hostapis[was].get("default_output_device", -1)
        if didx is not None and didx >= 0:
            return int(devices[didx]["default_samplerate"])
    except Exception:
        # Probe must never raise — degrade permissively per spec (see
        # probe_capture_rate's docstring). Any WASAPI/query oddity here
        # just means "unknown mix rate", not a fatal error; don't narrow
        # this to specific exception types without re-checking that
        # mandate first.
        return None
    return None


def _strip_loopback_hint_suffix(name: str) -> str:
    """
    `CaptureDevice.name` for a loopback device is built as
    f"{spk.name}  [loopback]" (see `_list_loopback_devices`), so the raw
    name never appears verbatim in sounddevice's WASAPI output device
    list. Strip the trailing "[loopback]" marker (and the whitespace
    around it) so `_wasapi_output_mix_rate`'s containment match can find
    the real output device.
    """
    stripped = name.rstrip()
    suffix = "[loopback]"
    if stripped.casefold().endswith(suffix):
        stripped = stripped[: -len(suffix)].rstrip()
    return stripped


def probe_capture_rate(
    device: CaptureDevice | None,
    sample_rate: int,
    channels: int,
) -> ProbeResult:
    """
    Can this source honestly deliver `sample_rate`? Loopback rates above
    the output mix format add no information (Windows hands loopback
    audio at the mix rate), so we fall back with a notice instead of
    silently upsampling. Unknown capabilities are treated permissively —
    the capture backends already handle format conversion.
    """
    if sd is None:
        # No sounddevice backend to ask — can't probe, trust the request.
        return ProbeResult(True, sample_rate)
    kind = device.kind if device is not None else "loopback"
    if kind == "input":
        try:
            # CaptureDevice.id for kind="input" is a sounddevice index
            # stored as a string (see build_capture_source's int(device.id)
            # for the same pattern). Passing the raw string here would make
            # sounddevice treat it as a name-substring query instead of an
            # index, so every real device would misreport as unsupported.
            idx = int(device.id)
        except (TypeError, ValueError):
            # Id isn't a parseable index (shouldn't happen for real devices,
            # but defend against it) — we can't probe, so degrade
            # permissively per spec: trust the requested rate.
            return ProbeResult(True, sample_rate)
        try:
            # Rate-only probe: channels are deliberately left out so a
            # channel-count mismatch isn't misreported as a sample-rate
            # problem (the notice below suggests a rate fallback, which
            # would not fix a channel issue).
            sd.check_input_settings(
                device=idx, samplerate=sample_rate, dtype="float32",
            )
            return ProbeResult(True, sample_rate)
        except Exception:
            # Probe must never raise — degrade permissively per spec: any
            # failure here (unsupported rate, missing device, driver quirk)
            # falls back to the device's own reported default rate instead
            # of propagating. Don't narrow this without re-checking that
            # mandate first.
            try:
                info = sd.query_devices(idx)
                fallback = int(info["default_samplerate"])
            except Exception:
                fallback = 48_000
            return ProbeResult(
                False, fallback,
                f"'{device.name}' can't open at {sample_rate} Hz — "
                f"capturing at {fallback} Hz instead.",
            )
    # loopback / process_loopback: capped by the output mix format
    name_hint = device.name if device is not None else None
    if name_hint is not None:
        name_hint = _strip_loopback_hint_suffix(name_hint)
    mix = _wasapi_output_mix_rate(name_hint)
    if mix is None or sample_rate <= mix:
        return ProbeResult(True, sample_rate)
    return ProbeResult(
        False, mix,
        f"Output mix format is {mix} Hz — a {sample_rate} Hz capture "
        f"won't contain content above {mix // 2} Hz. "
        f"Capturing at {mix} Hz instead.",
    )


def apply_rate_probe(
    preset: QualityPreset,
    device: CaptureDevice | None,
) -> tuple[QualityPreset, str | None]:
    """Probe `device` for `preset.sample_rate`; return the (possibly
    rate-adjusted) preset plus a user-facing notice, or (preset, None)."""
    probe = probe_capture_rate(device, preset.sample_rate, preset.channels)
    if probe.ok:
        return preset, None
    adjusted = replace(preset, sample_rate=probe.effective_rate)
    return adjusted, probe.message
