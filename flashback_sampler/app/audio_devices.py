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

from dataclasses import dataclass, field
from typing import Literal

from flashback_sampler.platform.capabilities import loopback_supported


CaptureKind = Literal["loopback", "input", "process_loopback"]


@dataclass(frozen=True)
class CaptureDevice:
    """
    A source that feeds audio into the ring buffer.

    `kind`:
        "loopback" — Windows WASAPI loopback on a speaker (captures
            what that speaker is playing). `id` is the soundcard
            speaker name.
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
    for d in list_capture_devices():
        if d.is_default:
            return d
    devices = list_capture_devices()
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
            speaker_name=device.id,
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
