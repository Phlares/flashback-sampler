"""Per-source health evaluation — pure Python, no Qt and no hardware.

The app layer gathers a :class:`SourceSnapshot` for each capture source on
every UI tick (recent peak from the ring buffer, how long it's been silent,
buffer fill, xrun rate, and the backend's last error) and calls
:func:`evaluate`. The result drives the in-app status badge; :func:`worst`
rolls the per-source results up into the single tray-icon severity.

Keeping this pure makes the whole defensive-heal policy trivially testable
and reusable from a future non-Qt host (VST/OBS).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "Severity", "SourceSnapshot", "SourceStatus", "evaluate", "worst",
    "SILENCE_DBFS", "SEVERITY_RING_COLOR", "SEVERITY_GLYPH",
]


class Severity(IntEnum):
    OK = 0
    INFO = 1
    WARN = 2
    ERROR = 3


# Detection thresholds (see PLATFORM/notification design notes).
SILENCE_DBFS = -60.0       # peak below this counts as "no signal"
LOW_DBFS = -40.0           # peak below this (but audible) is "low level"
CLIP_PEAK = 0.99           # sample magnitude at/above this is clipping
SILENCE_GRACE_S = 5.0      # must stay silent this long before warning
BUFFER_FULL_FRAC = 0.98    # ring buffer this full is "almost full"
XRUN_RATE_WARN = 1.0       # sustained xruns/sec worth surfacing


# Presentation per severity — single source of truth so the tray ring and the
# in-app chip badge can't drift. Plain data (no Qt), so it lives next to the
# enum it describes rather than in the Qt theme.
SEVERITY_RING_COLOR = {
    Severity.OK: "#9aa886",     # calm sage — "good", not alarmist
    Severity.INFO: "#9aa886",
    Severity.WARN: "#d29922",   # amber — no signal / clipping / buffer near cap
    Severity.ERROR: "#f85149",  # red — disconnected / permission / failure
}
SEVERITY_GLYPH = {
    Severity.WARN: "!",
    Severity.ERROR: "✕",
}


@dataclass(frozen=True)
class SourceSnapshot:
    """A point-in-time view of one capture source's runtime state."""
    capturing: bool
    peak: float = 0.0            # recent peak sample magnitude, 0..~1+
    silent_seconds: float = 0.0  # how long peak has been below the floor
    buffer_fill: float = 0.0     # ring-buffer fill fraction, 0..1
    xrun_rate: float = 0.0       # recent xruns per second
    error: str | None = None     # backend's last error (disconnect etc.), if any


@dataclass(frozen=True)
class SourceStatus:
    severity: Severity
    code: str       # ok | idle | silent | low | clip | buffer_high | dropouts | error
    message: str


def _dbfs(mag: float) -> float:
    return -math.inf if mag <= 0.0 else 20.0 * math.log10(mag)


def evaluate(snap: SourceSnapshot) -> SourceStatus:
    """Map a snapshot to a single status, highest-priority condition wins."""
    # Hard errors (disconnect, backend failure) outrank anything else.
    if snap.error:
        return SourceStatus(Severity.ERROR, "error", snap.error)
    if not snap.capturing:
        return SourceStatus(Severity.OK, "idle", "Idle — not capturing")
    if snap.peak >= CLIP_PEAK:
        return SourceStatus(Severity.WARN, "clip", "Clipping")
    if snap.buffer_fill >= BUFFER_FULL_FRAC:
        return SourceStatus(Severity.WARN, "buffer_high", "Buffer almost full")

    peak_db = _dbfs(snap.peak)
    if peak_db < SILENCE_DBFS:
        if snap.silent_seconds >= SILENCE_GRACE_S:
            return SourceStatus(Severity.WARN, "silent", "No signal")
        return SourceStatus(Severity.OK, "ok", "Capturing")  # within grace
    if snap.xrun_rate >= XRUN_RATE_WARN:
        return SourceStatus(Severity.INFO, "dropouts", "Dropouts")
    if peak_db < LOW_DBFS:
        return SourceStatus(Severity.INFO, "low", "Low level")
    return SourceStatus(Severity.OK, "ok", "Capturing")


def worst(statuses: list[SourceStatus]) -> SourceStatus:
    """The highest-severity status across sources (for the tray roll-up)."""
    if not statuses:
        return SourceStatus(Severity.OK, "ok", "Capturing")
    return max(statuses, key=lambda s: s.severity)
