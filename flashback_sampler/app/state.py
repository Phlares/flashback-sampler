"""
AppState — the root object graph for the Qt application layer.

M10.4 refactors this from "one buffer, one checkout manager, one
optional capture source" to "a LIST of CaptureSlots with one active
index." Each slot owns its own buffer / checkout manager / capture
source. Backward-compat properties (buffer, checkout_manager, capture)
delegate to the ACTIVE slot so every existing widget keeps working
without changes — M10.5's source strip will then add a UI affordance
for switching which slot is active.

Nothing in this file imports PySide6 — it's a plain Python container
so unit tests can drive it headless.
"""

from __future__ import annotations

import sys
from typing import Optional

from flashback_sampler.app.audio_devices import (
    CaptureDevice,
    OutputDevice,
    build_capture_source,
    default_capture_device,
    default_output_device,
)
from flashback_sampler.core.buffer import AudioCircularBuffer
from flashback_sampler.core.capture_slot import CaptureSlot
from flashback_sampler.core.checkout import CheckoutManager
from flashback_sampler.core.quality_presets import QualityPreset
from flashback_sampler.core.scrub_player import ScrubPlayer


# Default capture target: 15-minute rolling buffer at 48 kHz stereo.
# Size: 15 * 60 * 48_000 * 2 * 4 bytes ≈ 330 MB. Matches the original
# prototype default and is what the UI will display initially.
DEFAULT_BUFFER_SECONDS = 15 * 60
DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_CHANNELS = 2


class AppState:
    def __init__(
        self,
        buffer_seconds: float = DEFAULT_BUFFER_SECONDS,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
    ):
        # ── Slot list ─────────────────────────────────────────────────
        # Build one initial slot from the constructor args. It's
        # conceptually a "CUSTOM" quality preset since the values come
        # from CLI flags / settings rather than one of the named
        # presets. M10.5's Add Source dialog will use the named
        # presets for subsequent slots.
        initial_preset = QualityPreset(
            name="CUSTOM",
            sample_rate=int(sample_rate),
            channels=int(channels),
            buffer_seconds=float(buffer_seconds),
            description="Initial slot built from CLI args",
        )
        self.slots: list[CaptureSlot] = [
            CaptureSlot.from_quality_preset(
                initial_preset,
                name="Main",
                max_active_checkouts=16,
                max_total_ram_mb=1024,
            )
        ]
        self.active_slot_index: int = 0

        # ── Shared, non-per-slot state ───────────────────────────────
        self.scrub_player = ScrubPlayer(
            sample_rate=int(sample_rate),
            channels=int(channels),
        )

        # Device selections. Start with the system defaults and let
        # the main window override them from config.json on startup.
        self.capture_spec: Optional[CaptureDevice] = default_capture_device()
        self.output_spec: Optional[OutputDevice] = default_output_device()
        if self.output_spec is not None:
            self.scrub_player.set_device(self.output_spec.id)

    # ------------------------------------------------------------------
    # Slot access
    # ------------------------------------------------------------------

    @property
    def active_slot(self) -> CaptureSlot:
        return self.slots[self.active_slot_index]

    def set_active_slot_index(self, index: int) -> None:
        if not (0 <= index < len(self.slots)):
            raise IndexError(
                f"active_slot_index {index} out of range [0, {len(self.slots)})"
            )
        self.active_slot_index = int(index)

    def add_slot(
        self,
        preset: QualityPreset,
        name: str = "",
        max_active_checkouts: int = 16,
        max_total_ram_mb: float = 1024.0,
    ) -> CaptureSlot:
        """
        Append a new CaptureSlot built from `preset`. Does NOT change
        the active slot index — the caller decides whether to switch.
        """
        slot = CaptureSlot.from_quality_preset(
            preset,
            name=name or f"Source {len(self.slots) + 1}",
            max_active_checkouts=max_active_checkouts,
            max_total_ram_mb=max_total_ram_mb,
        )
        self.slots.append(slot)
        return slot

    def remove_slot(self, index: int) -> None:
        """
        Remove and stop the slot at `index`. The active_slot_index is
        adjusted to stay in range. Raises if it would leave zero slots —
        the app always has at least one slot.
        """
        if len(self.slots) <= 1:
            raise RuntimeError("cannot remove the last slot")
        if not (0 <= index < len(self.slots)):
            raise IndexError(f"slot index {index} out of range")
        slot = self.slots.pop(index)
        try:
            slot.stop_capture()
        except Exception:  # pragma: no cover
            pass
        if self.active_slot_index >= len(self.slots):
            self.active_slot_index = len(self.slots) - 1
        elif self.active_slot_index > index:
            self.active_slot_index -= 1

    # ------------------------------------------------------------------
    # Backward-compat properties — delegate to the active slot so every
    # existing widget continues to work against state.buffer /
    # state.checkout_manager / state.capture / state.sample_rate /
    # state.channels as before M10.4.
    # ------------------------------------------------------------------

    @property
    def buffer(self) -> AudioCircularBuffer:
        return self.active_slot.buffer

    @property
    def checkout_manager(self) -> CheckoutManager:
        return self.active_slot.checkout_manager

    @property
    def capture(self):
        return self.active_slot.capture_source

    def set_capture(self, capture) -> None:
        self.active_slot.capture_source = capture

    @property
    def sample_rate(self) -> int:
        return self.active_slot.sample_rate

    @property
    def channels(self) -> int:
        return self.active_slot.channels

    def is_capturing(self) -> bool:
        return self.active_slot.is_capturing()

    def set_capture_spec(self, spec: CaptureDevice) -> None:
        self.capture_spec = spec

    def set_output_spec(self, spec: OutputDevice) -> None:
        self.output_spec = spec
        self.scrub_player.set_device(spec.id)

    def build_capture(self):
        """
        Instantiate a capture source for the ACTIVE slot from the
        current capture_spec. Raises if no spec is selected.
        """
        if self.capture_spec is None:
            raise RuntimeError(
                "No capture device selected. Pick one from the Audio menu."
            )
        slot = self.active_slot
        return build_capture_source(
            device=self.capture_spec,
            buffer=slot.buffer,
            sample_rate=slot.sample_rate,
            channels=slot.channels,
        )

    # ------------------------------------------------------------------
    # Runtime settings application — now scoped to the active slot
    # ------------------------------------------------------------------

    def apply_checkout_caps(
        self,
        max_active: int | None = None,
        max_ram_mb: float | None = None,
    ) -> None:
        """
        Update the active slot's CheckoutManager caps in place.
        Other slots' caps are intentionally left alone so a future
        per-slot settings dialog can tune each independently.
        """
        mgr = self.active_slot.checkout_manager
        if max_active is not None:
            mgr._max_active = int(max_active)  # noqa: SLF001
        if max_ram_mb is not None:
            mgr._max_ram_bytes = int(  # noqa: SLF001
                float(max_ram_mb) * 1024 * 1024
            )

    def rebuild_buffer(self, new_seconds: float) -> None:
        """
        Rebuild the ACTIVE slot's ring buffer with a new duration.
        Stops capture if it was running; the caller is responsible for
        restarting capture after the rebuild.

        Existing Checkouts are preserved — they're immutable in-RAM
        snapshots. The active slot's CheckoutManager._buffer reference
        is updated so new checkouts pull from the fresh ring.
        """
        slot = self.active_slot
        was_running = slot.is_capturing()
        if was_running:
            try:
                slot.stop_capture()
            except Exception:  # pragma: no cover
                pass

        new_buf = AudioCircularBuffer(
            duration_seconds=float(new_seconds),
            sample_rate=slot.sample_rate,
            channels=slot.channels,
        )
        slot.buffer = new_buf
        slot.buffer_seconds = float(new_seconds)
        slot.checkout_manager._buffer = new_buf  # noqa: SLF001
        # Capture source, if present, was writing into the old buffer.
        # Drop it so the caller rebuilds from state.build_capture().
        slot.capture_source = None

    def shutdown(self) -> None:
        """Called on window close — stop all slots + playback cleanly."""
        for slot in self.slots:
            try:
                slot.stop_capture()
            except Exception:  # pragma: no cover
                pass
        try:
            self.scrub_player.close()
        except Exception:  # pragma: no cover
            pass


def make_loopback_capture(state: AppState):
    """
    DEPRECATED: use `state.build_capture()` instead. Kept for any leftover
    callers from before M7.
    """
    if sys.platform != "win32":
        raise RuntimeError(
            "Loopback capture is Windows-only for now. "
            "Use a mic/line-in CaptureSource on this platform."
        )
    return state.build_capture()
