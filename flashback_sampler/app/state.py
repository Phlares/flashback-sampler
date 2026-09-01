"""
AppState — the root object graph for the Qt application layer.

Holds a LIST of CaptureSlots with one active index. Each slot owns its
own buffer / checkout manager / capture source. Convenience properties
(buffer, checkout_manager, capture) delegate to the ACTIVE slot, and the
UI switches which slot is active.

Nothing in this file imports PySide6 — it's a plain Python container
so unit tests can drive it headless.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from flashback_sampler.app import config as app_config
from flashback_sampler.app.audio_devices import (
    CaptureDevice,
    OutputDevice,
    build_capture_source,
    build_mixed_capture_source,
    default_capture_device,
    default_output_device,
)
from flashback_sampler.core.capture_slot import CaptureSlot
from flashback_sampler.core.checkout import Checkout, CheckoutManager
from flashback_sampler.core.manifest import resolve_audio, scan
from flashback_sampler.core import native
from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch
from flashback_sampler.core.quality_presets import QualityPreset
from flashback_sampler.core.scrub_player import NativeScrubPlayer


# Default capture target: 5-minute rolling buffer at 48 kHz stereo.
# Size: 5 * 60 * 48_000 * 2 * 4 bytes ≈ 110 MB. main.py's --buffer-minutes
# argparse default derives from this constant, so this is the one place
# that sets the launch default.
DEFAULT_BUFFER_SECONDS = 5 * 60
DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_CHANNELS = 2


class AppState:
    def __init__(
        self,
        buffer_seconds: float = DEFAULT_BUFFER_SECONDS,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        scratch_dir: Path | None = None,
        checkout_cache_mb: float | None = None,
    ):
        # ── Scratch: the process-wide writer thread + RAM cache ─────────
        # One per AppState; every slot's CheckoutManager writes through
        # it. Checkouts scratch to <scratch_dir>/<id>.wav on creation
        # (epic #53). Budget in bytes; 0 = only pinned/in-flight stay.
        requested_scratch_dir = Path(scratch_dir) if scratch_dir is not None else app_config.load_scratch_dir()
        # F1: an uncreatable configured scratch dir (bad drive letter, no
        # permission, a stale removable-media path, ...) must not brick
        # every launch. Fall back to the app-owned cache dir, which is
        # always creatable, and record what happened so the window can
        # tell the user instead of silently redirecting their scratch.
        self.scratch_dir_error: Optional[str] = None
        try:
            requested_scratch_dir.mkdir(parents=True, exist_ok=True)
            self.scratch_dir = requested_scratch_dir
        except OSError as e:
            self.scratch_dir = app_config.default_scratch_dir()
            self.scratch_dir.mkdir(parents=True, exist_ok=True)
            self.scratch_dir_error = (
                f"Could not use scratch folder {requested_scratch_dir}: {e}. "
                f"Using {self.scratch_dir} instead."
            )
        cache_mb = app_config.load_checkout_cache_mb() if checkout_cache_mb is None else float(checkout_cache_mb)
        self.scratch = NativeScratch(budget_bytes=int(cache_mb * 1024 * 1024))
        self.scratch.start()

        # ── Slot list ─────────────────────────────────────────────────
        # Build one initial slot from the constructor args. It's
        # conceptually a "CUSTOM" quality preset since the values come
        # from CLI flags / settings rather than one of the named
        # presets. The Add Source dialog uses the named presets for
        # subsequent slots.
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
                scratch=self.scratch,
                scratch_dir=self.scratch_dir,
            )
        ]
        self.active_slot_index: int = 0
        # Max footprint (#41): a safety line for the whole session's
        # resident bytes, not a reservation. Default = 25 % of physical
        # RAM at launch; the stored preference overrides it; 0 = no cap.
        total_physical, _free = native.mem_info()
        self.max_footprint_mb: float = app_config.load_max_footprint_mb(
            default=app_config.default_max_footprint_mb(total_physical)
        )

        # Master transport state. `rolling` is the global START/STOP
        # CAPTURE toggle. While rolling, every armed slot has a live
        # capture source writing into its ring; stopping rolls every
        # slot down but preserves each slot's `armed` flag so the next
        # start picks up the same set of sources.
        self.rolling: bool = False

        # ── Shared, non-per-slot state ───────────────────────────────
        self.scrub_player = NativeScrubPlayer(
            sample_rate=int(sample_rate),
            channels=int(channels),
        )

        # Device selections. Start with the system defaults and let
        # the main window override them from config.json on startup.
        self.capture_spec: Optional[CaptureDevice] = default_capture_device()
        self.output_spec: Optional[OutputDevice] = default_output_device()
        if self.output_spec is not None:
            self.scrub_player.set_device(self.output_spec.id)

        # Restore whatever the scratch dir already holds -- a normal
        # relaunch after clean shutdown, or the same path a crash left
        # behind (crash and quit take this same recovery path).
        self.adopt_scratch()

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
        capture_spec: Optional[CaptureDevice] = None,
        armed: bool = True,
    ) -> CaptureSlot:
        """
        Append a new CaptureSlot built from `preset`. Does NOT change
        the active slot index — the caller decides whether to switch.

        `capture_spec` is an optional per-slot device override. If None
        (the default), the slot inherits whichever device is currently
        set at the AppState level when it goes to build a source.

        `armed` sets the new slot's initial armed state (default True,
        the normal "Add Source" path); adoption passes False for a
        foreign-rate slot it recreates just to hold checkouts.

        Raises RuntimeError when the new ring would push the session's
        resident bytes past `max_footprint_mb` (when a cap is set), or
        when the ring alone exceeds the free physical memory the engine
        reports (skipped where the platform cannot say). Either way the
        refusal happens BEFORE the ring is created; `fb_ring_create`'s
        own out_of_memory status stays as the backstop (#41).
        `total_project_ram_bytes()` includes `self.scratch.resident_bytes`,
        so the cap clause also depends on what the scratch cache holds.
        """
        from flashback_sampler.core.quality_presets import MB

        new_ring_bytes = preset.ram_bytes()
        current_bytes = self.total_project_ram_bytes()
        cap_bytes = int(self.max_footprint_mb * MB)
        if cap_bytes and current_bytes + new_ring_bytes > cap_bytes:
            raise RuntimeError(
                f"Max footprint exceeded: adding {preset.name} "
                f"({new_ring_bytes / MB:.0f} MB) would bring the session to "
                f"{(current_bytes + new_ring_bytes) / MB:.0f} MB, over the "
                f"{self.max_footprint_mb:.0f} MB max footprint. "
                f"Raise it (or set 0 for no cap) in Preferences, or pick a lighter preset."
            )
        _total, free_bytes = native.mem_info()
        if free_bytes and new_ring_bytes > free_bytes:
            raise RuntimeError(
                f"Not enough free memory: {preset.name} needs "
                f"{new_ring_bytes / MB:.0f} MB and only {free_bytes / MB:.0f} MB "
                f"of physical memory is free. Close other programs or pick a lighter preset."
            )

        slot = CaptureSlot.from_quality_preset(
            preset,
            name=name or f"Source {len(self.slots) + 1}",
            max_active_checkouts=max_active_checkouts,
            scratch=self.scratch,
            scratch_dir=self.scratch_dir,
        )
        slot.capture_spec = capture_spec
        slot.armed = armed
        self.slots.append(slot)
        return slot

    def adopt_scratch(self) -> list[Checkout]:
        """Adopt every manifest in the scratch dir: a root goes to the
        first slot with its rate and channels, else to a new unarmed slot
        named from the manifest (60 s ring — an arbitrary small default;
        a foreign-rate slot only exists to hold adopted checkouts, not to
        capture into); a slice goes where its parent went. Anything
        unreadable, without audio, or that fails anywhere in adoption
        (a corrupt-but-parseable manifest, a locked `.part` rename, a
        RAM-budget refusal for a new slot, ...) is skipped and left on
        disk -- no on-disk artefact may abort a launch. Crash and quit
        take this same path."""
        adopted: list[Checkout] = []
        where: dict[str, CaptureSlot] = {}
        for m in scan(self.scratch_dir):
            try:
                if m.parent is None:
                    found = resolve_audio(self.scratch_dir, m)  # renames a lone .wav.part into place
                    if found is None:
                        continue
                    audio, partial = found
                    slot = next((s for s in self.slots if s.sample_rate == m.rate and s.channels == m.channels), None)
                    if slot is None:
                        slot = self.add_slot(
                            QualityPreset(name="ADOPTED", sample_rate=int(m.rate), channels=int(m.channels),
                                          buffer_seconds=60.0, description="Slot recreated for adopted checkouts"),
                            name=m.slot or "Adopted", armed=False,
                        )
                    co = slot.checkout_manager.adopt_root(m, audio, partial)
                else:
                    slot = where.get(m.parent)
                    if slot is None:
                        continue
                    co = slot.checkout_manager.adopt_slice(m, slot.checkout_manager.get(m.parent))
            except Exception:
                # No on-disk artefact may abort a launch -- skip and continue.
                continue
            where[co.id] = slot
            adopted.append(co)
        return adopted

    def effective_capture_spec_for_slot(self, slot: CaptureSlot) -> Optional[CaptureDevice]:
        """
        Return the capture device that should actually be used to open
        a source for `slot`. Slot-level override wins; falls back to
        the AppState global.
        """
        return slot.capture_spec if slot.capture_spec is not None else self.capture_spec

    # ------------------------------------------------------------------
    # Project-wide RAM accounting
    # ------------------------------------------------------------------

    def total_project_ram_bytes(self) -> int:
        """
        Sum of every slot's ring buffer bytes PLUS the scratch cache's
        resident bytes -- every checkout's RAM copy, counted once per
        process. Reflects the actual resident-in-RAM footprint of the
        whole session.
        """
        total = 0
        for slot in self.slots:
            # capacity_bytes, not slot.buffer.buffer.nbytes -- the latter
            # reads the raw storage array, which is larger than the
            # readable window by a guard band (see native.py's
            # capacity_bytes docstring).
            total += slot.buffer.capacity_bytes
        total += self.scratch.resident_bytes
        return total

    def total_project_ram_mb(self) -> float:
        return self.total_project_ram_bytes() / (1024.0 * 1024.0)

    def set_max_footprint_mb(self, mb: float) -> None:
        """0 = no cap; negatives floor to 0."""
        self.max_footprint_mb = max(0.0, float(mb))

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
        # A removed slot's checkouts go with it: manifests and files
        # deleted.
        slot.checkout_manager.discard_all()
        try:
            slot.buffer.close()
        except Exception:  # pragma: no cover
            pass
        if self.active_slot_index >= len(self.slots):
            self.active_slot_index = len(self.slots) - 1
        elif self.active_slot_index > index:
            self.active_slot_index -= 1

    # ------------------------------------------------------------------
    # Convenience properties — delegate to the active slot so callers can
    # use state.buffer / state.checkout_manager / state.capture /
    # state.sample_rate / state.channels without reaching into the slot.
    # ------------------------------------------------------------------

    @property
    def buffer(self) -> NativeAudioCircularBuffer:
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
        return self.build_capture_for_slot(self.active_slot)

    # ------------------------------------------------------------------
    # Master transport — START / STOP CAPTURE for every armed slot
    # ------------------------------------------------------------------

    def start_rolling(self) -> tuple[int, Optional[Exception]]:
        """
        Enter the rolling state: for every armed slot that isn't
        already capturing, build + bind + start a capture source.
        Returns (started_count, first_error) so the caller can surface
        partial failures without aborting the whole operation.
        """
        started = 0
        first_error: Optional[Exception] = None
        for slot in self.slots:
            if not slot.armed:
                continue
            if slot.is_capturing():
                continue
            try:
                source = self.build_capture_for_slot(slot)
                slot.bind_capture(source)
                slot.start_capture()
                started += 1
            except Exception as e:  # pragma: no cover — hardware path
                first_error = first_error or e
        self.rolling = True
        return started, first_error

    def stop_rolling(self) -> int:
        """
        Leave the rolling state: stop every slot whose capture source
        is currently running. Preserves each slot's `armed` flag so
        the next `start_rolling()` picks up the same set. Returns the
        number of slots actually stopped.
        """
        stopped = 0
        for slot in self.slots:
            if slot.is_capturing():
                try:
                    slot.stop_capture()
                    stopped += 1
                except Exception:  # pragma: no cover
                    pass
        self.rolling = False
        return stopped

    def armed_count(self) -> int:
        return sum(1 for s in self.slots if s.armed)

    def build_capture_for_slot(self, slot: CaptureSlot):
        """
        Instantiate a capture source wired to `slot`'s buffer.

        Per-slot device routing: if `slot.capture_specs` has any
        entries they take precedence — exactly one entry becomes a
        standard single-source route, two or more become a muxed
        NativeMixedSource that sums all inputs into the same buffer.
        An empty list falls back to AppState's global capture_spec.
        """
        specs = list(slot.capture_specs) if slot.capture_specs else []
        if not specs:
            device = self.effective_capture_spec_for_slot(slot)
            if device is None:
                raise RuntimeError(
                    "No capture device selected. Pick one from the Audio menu "
                    "or from the slot's right-click menu."
                )
            specs = [device]

        if len(specs) == 1:
            return build_capture_source(
                device=specs[0],
                buffer=slot.buffer,
                sample_rate=slot.sample_rate,
                channels=slot.channels,
            )
        # Two or more: one Zig mixer owns a capture and a staging ring per
        # device and sums them into slot.buffer. Python passes the devices.
        return build_mixed_capture_source(
            devices=specs,
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
    ) -> None:
        """
        Update the active slot's CheckoutManager caps in place.
        Other slots' caps are intentionally left alone so a future
        per-slot settings dialog can tune each independently.
        """
        mgr = self.active_slot.checkout_manager
        if max_active is not None:
            mgr._max_active = int(max_active)  # noqa: SLF001

    def rebuild_buffer(self, new_seconds: float) -> None:
        """
        Rebuild the ACTIVE slot's ring buffer with a new duration.
        Stops capture if it was running; the caller is responsible for
        restarting capture after the rebuild.

        Existing Checkouts are preserved — they're file-backed scratch
        handles, independent of the ring they were cut from. The active
        slot's CheckoutManager._buffer reference is updated so new
        checkouts pull from the fresh ring.
        """
        slot = self.active_slot
        was_running = slot.is_capturing()
        if was_running:
            try:
                slot.stop_capture()
            except Exception:  # pragma: no cover
                pass

        old_buf = slot.buffer
        new_buf = NativeAudioCircularBuffer(
            duration_seconds=float(new_seconds),
            sample_rate=slot.sample_rate,
            channels=slot.channels,
        )
        new_buf.gain = old_buf.gain  # preserve the source's record gain
        slot.buffer = new_buf
        slot.buffer_seconds = float(new_seconds)
        slot.checkout_manager._buffer = new_buf  # noqa: SLF001
        # Capture source, if present, was writing into the old buffer.
        # Drop it so the caller rebuilds from state.build_capture().
        slot.capture_source = None
        # Release the old buffer's resources now rather than waiting on
        # GC/__del__ -- deterministic release of the Zig-owned handle.
        # Safe here specifically: the writer was already stopped above
        # (stop_capture() joins/closes its stream before returning), and
        # rebuild_buffer runs entirely on the GUI thread, so nothing else
        # can be mid-read on old_buf when we reach this line.
        try:
            old_buf.close()
        except Exception:  # pragma: no cover
            pass

    def shutdown(self) -> None:
        """Called on window close — stop all slots + playback cleanly."""
        for slot in self.slots:
            try:
                slot.stop_capture()
            except Exception:  # pragma: no cover
                pass
        for slot in self.slots:
            try:
                slot.checkout_manager.close()
            except Exception:  # pragma: no cover
                pass
        try:
            self.scratch.close()  # drains the writer
        except Exception:  # pragma: no cover
            pass
        try:
            self.scrub_player.close()
        except Exception:  # pragma: no cover
            pass
