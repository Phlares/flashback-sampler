"""
Enumerate capture and preview-output audio devices.

Loopback, input, and render (output) devices are all enumerated
through the Zig core (`flashback_sampler/core/native.py`,
`native_capture.py`) via `native.list_devices()` / `NativeCaptureSource`.

The UI reads these lists into menu/dropdown widgets and passes the
chosen dataclass back through the AppState so controllers can build
the right concrete source object.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field, replace
from typing import Literal

from flashback_sampler.core import native
from flashback_sampler.core.native_capture import NativeCaptureSource, NativeMixedSource
from flashback_sampler.core.quality_presets import QualityPreset
from flashback_sampler.platform.capabilities import loopback_supported


CaptureKind = Literal["loopback", "input", "process_loopback"]


@dataclass(frozen=True)
class CaptureDevice:
    """
    A source that feeds audio into the ring buffer.

    `kind`:
        "loopback" — WASAPI loopback on a render endpoint (captures
            what that endpoint is playing). `id` is the WASAPI
            endpoint id string; `""` means follow the live OS default
            output. The DEFAULT_LOOPBACK sentinel sets
            `follow_default=True` to track the live OS default output
            instead of pinning a specific endpoint.
        "input" — a WASAPI capture endpoint (mic, line-in, virtual
            cable). `id` is the WASAPI endpoint id string; `""` means
            follow the live OS default input.
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
    # WASAPI shared-mode mix rate of the endpoint (Hz), when known. Drives
    # probe_capture_rate's honest-rate fallback. None = unknown/permissive.
    mix_rate: int | None = None


# Sentinel loopback device that follows the LIVE OS default output. Its empty
# `id` maps to NativeCaptureSource(device_id=""), which resolves the WASAPI
# default render endpoint at start — so capture follows whatever the user is
# actually hearing, even if the default output changes after launch. Pinning a
# specific named endpoint (the old default behaviour) silently records nothing
# when that endpoint isn't the one playing audio.
DEFAULT_LOOPBACK = CaptureDevice(
    kind="loopback", name="Default output  [loopback]", id="",
    is_default=True, follow_default=True,
)


@dataclass(frozen=True)
class OutputDevice:
    """A WASAPI render endpoint used for preview playback. `id` is the
    endpoint id string; `""` means the live OS default output."""
    id: str
    name: str
    max_output_channels: int
    is_default: bool = False


# ─────────────────────────────────────────────────────────────────────────
# Enumeration
# ─────────────────────────────────────────────────────────────────────────


def list_capture_devices() -> list[CaptureDevice]:
    """
    Return every available capture source: WASAPI loopback (render)
    endpoints, when supported, and input (capture) endpoints, both via
    the native core. The system default of each is marked.
    """
    return _list_native_devices()


def _list_native_devices() -> list[CaptureDevice]:
    out: list[CaptureDevice] = []
    for d in native.list_devices():
        if d["kind"] not in ("loopback", "input"):
            continue
        is_loop = d["kind"] == "loopback"
        if is_loop and not loopback_supported():
            continue
        out.append(CaptureDevice(
            kind=d["kind"],
            name=f'{d["name"]}  [loopback]' if is_loop else d["name"],
            id=d["id"],
            sample_rate=d["mix_rate"] or 48_000,
            channels=min(2, d["mix_channels"] or 2),
            is_default=d["is_default"],
            mix_rate=d["mix_rate"] or None,
        ))
    return out


def list_output_devices() -> list[OutputDevice]:
    """Every active render endpoint, from the same native list the
    capture side reads (render endpoints appear there twice: once as a
    loopback candidate, once as an output)."""
    return [
        OutputDevice(
            id=d["id"],
            name=d["name"],
            max_output_channels=d["mix_channels"] or 2,
            is_default=d["is_default"],
        )
        for d in native.list_devices()
        if d["kind"] == "render"
    ]


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
    devices = list_output_devices()
    for d in devices:
        if d.is_default:
            return d
    return devices[0] if devices else None


def _endpoints(device_id: str, is_default: bool) -> set[str]:
    """The keys a render endpoint answers to: its id, and "" when it is
    the OS default (picked as "follow the default" or by its own id).
    Two sides name the same endpoint when their key sets intersect."""
    return {device_id} | ({""} if is_default else set())


@functools.cache
def _own_root_pid() -> int:
    """This process's same-exe root, resolved once: the check runs from
    a 1 Hz poll and the answer never changes."""
    return native.resolve_root_pid(os.getpid())


def captures_preview(device: CaptureDevice, output: OutputDevice) -> bool:
    """True when `device` records what `output` plays, so a preview
    through `output` lands back in the ring. A loopback matches on the
    endpoint. A per-process source captures its target's process tree
    (include mode, `WasapiBackend.zig`), so it matches only when that
    tree is ours. An input never plays back what the preview renders."""
    if device.kind == "loopback":
        return bool(_endpoints(device.id, device.is_default or device.follow_default)
                    & _endpoints(output.id, output.is_default))
    if device.kind == "process_loopback":
        return native.resolve_root_pid(int(device.id)) == _own_root_pid()
    return False


# ─────────────────────────────────────────────────────────────────────────
# Spec → concrete source factory
# ─────────────────────────────────────────────────────────────────────────


def _spec_kwargs(device: CaptureDevice) -> dict:
    """CaptureDevice -> the keyword fields a native spec carries. Shared
    by the single and the mixed builder so a kind is mapped in one place."""
    if device.kind in ("loopback", "input"):
        return {
            "kind": device.kind,
            # follow_default → "" → the Zig side follows the live OS
            # default endpoint at start; otherwise pin to device.id.
            "device_id": "" if device.follow_default else device.id,
        }
    if device.kind == "process_loopback":
        try:
            pid = int(device.id)
        except ValueError as e:
            raise ValueError(
                f"process_loopback device id must be an integer PID; "
                f"got {device.id!r}"
            ) from e
        return {"kind": "process", "pid": native.resolve_root_pid(pid)}
    raise ValueError(f"unknown CaptureDevice.kind: {device.kind!r}")


def build_capture_source(device: CaptureDevice, buffer, sample_rate: int, channels: int):
    """Instantiate the capture source for ONE CaptureDevice.
    `buffer` is the NativeAudioCircularBuffer the source writes into."""
    return NativeCaptureSource(buffer=buffer, sample_rate=sample_rate, channels=channels, **_spec_kwargs(device))


def build_mixed_capture_source(devices, buffer, sample_rate: int, channels: int):
    """Instantiate the mixed source for two or more CaptureDevices: every
    device becomes a spec of the same Zig mixer, which sums them into
    `buffer`. Nothing per-source is created on the Python side."""
    return NativeMixedSource(
        buffer=buffer,
        specs=[_spec_kwargs(d) for d in devices],
        sample_rate=sample_rate,
        channels=channels,
    )


# ─────────────────────────────────────────────────────────────────────────
# Rate probe
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of asking whether a device can honestly deliver a rate."""
    ok: bool
    effective_rate: int
    message: str = ""


def probe_capture_rate(
    device: CaptureDevice | None,
    sample_rate: int,
    channels: int,
) -> ProbeResult:
    """
    Can this source honestly deliver `sample_rate`? Loopback and input
    rates above the endpoint's mix format add no information.
    """
    mix = device.mix_rate if device is not None else None
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
