"""
Quality presets + RAM math for CaptureSlots.

Pure Python — no audio backends, no Qt. The preset table is the
canonical source of truth; the "Add Source" dialog (M10.4) renders
these as a vertical cluster and a new CaptureSlot is built from the
user's pick.

RAM math is straightforward (sample_rate × channels × bytes_per_sample
× duration_seconds) but centralizing it here keeps every caller
reporting the same numbers to the user. Dtype is locked to float32
for v1 — see plan addendum for the deferred int16 optimization.
"""

from __future__ import annotations

from dataclasses import dataclass


BYTES_PER_SAMPLE = 4  # float32 — deferred: per-slot dtype config
MB = 1024 * 1024


@dataclass(frozen=True)
class QualityPreset:
    """
    Named bundle of (sample_rate, channels, default_buffer_seconds).

    The user picks one of these when they add a new source; the
    CaptureSlot holds the resolved values, not the preset name — so
    selecting FULL and then editing the buffer duration to 12 min
    produces a slot with quality_preset="FULL" but buffer_seconds=720.
    """
    name: str
    sample_rate: int
    channels: int
    buffer_seconds: float
    description: str = ""

    def ram_bytes(self) -> int:
        return compute_ram_bytes(
            self.sample_rate, self.channels, self.buffer_seconds
        )

    def ram_mb(self) -> float:
        return compute_ram_mb(
            self.sample_rate, self.channels, self.buffer_seconds
        )


# Ordered as they appear in the UI cluster: heaviest at the top,
# lightest at the bottom. Matches the spec in the plan addendum.
PRESETS: tuple[QualityPreset, ...] = (
    QualityPreset(
        name="FULL",
        sample_rate=48_000,
        channels=2,
        buffer_seconds=900.0,
        description="48k stereo, 15 min — uncompromised",
    ),
    QualityPreset(
        name="MUSIC",
        sample_rate=48_000,
        channels=2,
        buffer_seconds=300.0,
        description="48k stereo, 5 min — music stems",
    ),
    QualityPreset(
        name="VOICE",
        sample_rate=22_050,
        channels=1,
        buffer_seconds=900.0,
        description="22.05k mono, 15 min — podcast / call capture",
    ),
    QualityPreset(
        name="CHAT",
        sample_rate=16_000,
        channels=1,
        buffer_seconds=600.0,
        description="16k mono, 10 min — Discord / comms",
    ),
    QualityPreset(
        name="SCRATCH",
        sample_rate=16_000,
        channels=1,
        buffer_seconds=180.0,
        description="16k mono, 3 min — disposable scratchpad",
    ),
)

DEFAULT_PRESET_NAME = "MUSIC"


def preset_by_name(name: str) -> QualityPreset | None:
    """Lookup by exact name. Returns None if not found."""
    for p in PRESETS:
        if p.name == name:
            return p
    return None


def default_preset() -> QualityPreset:
    p = preset_by_name(DEFAULT_PRESET_NAME)
    assert p is not None  # DEFAULT_PRESET_NAME must exist in PRESETS
    return p


def compute_ram_bytes(
    sample_rate: int,
    channels: int,
    buffer_seconds: float,
) -> int:
    """
    Total bytes held by a single ring buffer. Equals
    `sample_rate * channels * buffer_seconds * bytes_per_sample`,
    clamped to 0 for invalid inputs.
    """
    if sample_rate <= 0 or channels <= 0 or buffer_seconds <= 0:
        return 0
    return int(
        float(buffer_seconds)
        * int(sample_rate)
        * int(channels)
        * BYTES_PER_SAMPLE
    )


def compute_ram_mb(
    sample_rate: int,
    channels: int,
    buffer_seconds: float,
) -> float:
    return compute_ram_bytes(sample_rate, channels, buffer_seconds) / MB
