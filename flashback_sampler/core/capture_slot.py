"""
CaptureSlot — one independent capture channel.

The foundation of the Shape B multi-source architecture. Each slot
owns its own ring buffer, its own CheckoutManager, its own transport
state (anchor offset + duration preset), and an optional active
capture source. AppState grows from holding ONE buffer+manager+source
triple to holding a LIST of slots, with one slot designated "active"
for the main transport cluster.

Slots are built from QualityPresets (see quality_presets.py). The
preset resolves the sample rate / channel count / buffer duration;
everything else (name, capture device binding, active state) is set
through the slot's API as the user interacts with it.

This module is pure core — no Qt, no audio backends. The slot
accepts any object that satisfies the CaptureSource Protocol via
`bind_capture()`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

from .buffer import AudioCircularBuffer
from .capture_source import CaptureSource
from .checkout import CheckoutManager
from .quality_presets import (
    QualityPreset,
    compute_ram_bytes,
    compute_ram_mb,
)


@dataclass
class CaptureSlot:
    """
    One capture channel. Holds:
      - a named identity (id, user-given label)
      - the resolved audio shape (sample_rate, channels, buffer_seconds,
        quality_preset name)
      - its own AudioCircularBuffer + CheckoutManager
      - an optional live CaptureSource (None until start_capture is
        called by the app layer)
      - per-slot transport state so each slot remembers its own anchor
        offset and duration preset across UI switches
    """

    id: str
    name: str
    sample_rate: int
    channels: int
    buffer_seconds: float
    quality_preset: str  # name of the QualityPreset used (for display / presets)
    buffer: AudioCircularBuffer
    checkout_manager: CheckoutManager
    capture_source: Optional[CaptureSource] = None
    anchor_offset_s: float = 0.0
    duration_preset_idx: int = 4  # default 3:00 on the 8-preset cluster

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_quality_preset(
        cls,
        preset: QualityPreset,
        name: str = "",
        max_active_checkouts: int = 16,
        max_total_ram_mb: float = 1024.0,
    ) -> "CaptureSlot":
        """
        Build a new slot from a QualityPreset. The ring buffer and
        checkout manager are constructed immediately; the capture
        source is left None (the app layer attaches one when the user
        picks a device).
        """
        buf = AudioCircularBuffer(
            duration_seconds=preset.buffer_seconds,
            sample_rate=preset.sample_rate,
            channels=preset.channels,
        )
        mgr = CheckoutManager(
            buffer=buf,
            max_active_checkouts=max_active_checkouts,
            max_total_ram_mb=max_total_ram_mb,
        )
        return cls(
            id=uuid.uuid4().hex[:12],
            name=name or preset.name,
            sample_rate=preset.sample_rate,
            channels=preset.channels,
            buffer_seconds=preset.buffer_seconds,
            quality_preset=preset.name,
            buffer=buf,
            checkout_manager=mgr,
        )

    # ------------------------------------------------------------------
    # Capture lifecycle
    # ------------------------------------------------------------------

    def bind_capture(self, source: CaptureSource) -> None:
        """
        Attach a concrete CaptureSource (e.g. WASAPI loopback, mic
        input, fake sine source). Stops any previously-bound source
        first. Does NOT start the new source — call start_capture()
        to begin writing frames.
        """
        if self.capture_source is not None and self.capture_source is not source:
            try:
                self.capture_source.stop()
            except Exception:  # pragma: no cover
                pass
        self.capture_source = source

    def start_capture(self) -> None:
        if self.capture_source is None:
            raise RuntimeError(
                f"CaptureSlot {self.name!r} has no capture source bound"
            )
        self.capture_source.start()

    def stop_capture(self) -> None:
        if self.capture_source is not None:
            try:
                self.capture_source.stop()
            except Exception:  # pragma: no cover
                pass

    def is_capturing(self) -> bool:
        return (
            self.capture_source is not None
            and self.capture_source.is_running()
        )

    def xrun_count(self) -> int:
        if self.capture_source is None:
            return 0
        return self.capture_source.xrun_count()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def ram_bytes(self) -> int:
        """
        RAM held by THIS slot's ring buffer (excluding checkouts —
        those are tracked separately by the CheckoutManager's cap).
        """
        return compute_ram_bytes(
            self.sample_rate, self.channels, self.buffer_seconds
        )

    def ram_mb(self) -> float:
        return compute_ram_mb(
            self.sample_rate, self.channels, self.buffer_seconds
        )

    def buffered_seconds(self) -> float:
        return self.buffer.buffered_seconds
