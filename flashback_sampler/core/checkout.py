"""
Checkout workflow — pull immutable snapshots of the live ring buffer.

Mental model (user-provided): a DJ with one turntable still spinning,
pulling a record off the rack to audition. The ring keeps writing
throughout. Each Checkout is a frozen copy of a span of the ring.

Where the audio lives (epic #53): in Zig. `create` copies the span out
of the ring into a Zig-owned buffer and queues its scratch write; the
scratch file `<scratch_dir>/<id>.wav` is the checkout from then on, and
the RAM copy is a cache the engine manages under a byte budget. Python
never holds samples: this module holds ids, states, trims, per-file
refcounts and the JSON manifests that adoption reads at launch.

A slice is a reference into its parent's file — `(path, start_frame,
n_frames)` with `parent_id` set. A file lives while any checkout in
this manager references it (`_file_refs`); the last discard deletes it.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import numpy as np

from flashback_sampler.core import native
from flashback_sampler.core.manifest import (
    Manifest, audio_path, bins_from_json, bins_to_json, manifest_path, write_manifest,
)
from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch


CheckoutState = Literal["pending", "ready", "saved", "discarded"]
CheckoutFormat = Literal["WAV"]
CheckoutSubtype = Literal["FLOAT", "PCM_24", "PCM_16"]

# FLOAT keeps the float32 scratch bit-perfect on disk.
_DEFAULT_SUBTYPE = "FLOAT"
_VALID_SUBTYPES: tuple[str, ...] = ("FLOAT", "PCM_24", "PCM_16")
# The deck draws two bin resolutions per checkout: the radial ring (540)
# and the clip panel (360). Computed once at create (from the RAM copy)
# and stored in the manifest so adoption never reads audio for them.
BIN_COUNTS: tuple[int, ...] = (540, 360)


class ScratchWriteFailed(RuntimeError):
    """The checkout's scratch write failed, so nothing can reference its
    file. The RAM copy still exports."""


@dataclass
class Checkout:
    """A frozen span of ring audio. `handle` is the Zig `*Checkout`;
    `path`/`start_frame`/`n_frames` say where the same audio lives on
    disk. `abs_sample_*` are the ring's absolute sample positions at
    creation time (display metadata).

    `start_frame` is ALWAYS absolute into `path` (a root is a slice at
    `(0, all)`), and so is the manifest field of the same name. The Zig
    `checkout_slice` call is the one place that differs: its `start` is
    PARENT-RELATIVE and Zig adds the parent's own `start_frame` itself,
    so every caller converts (`slice` adds, `adopt_slice` subtracts)."""

    id: str
    handle: int
    path: Path
    sample_rate: int
    channels: int
    n_frames: int
    start_frame: int
    abs_sample_start: int
    abs_sample_end: int
    parent_id: Optional[str] = None
    trim_in_samples: int = 0
    trim_out_samples: int = 0
    state: CheckoutState = "pending"
    partial: bool = False
    bins: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return self.n_frames / self.sample_rate

    def has_trim(self) -> bool:
        return self.trim_in_samples > 0 or (0 < self.trim_out_samples < self.n_frames)

    def trim_range(self) -> tuple[int, int]:
        """(start, n) within the checkout: the trim, or the whole clip."""
        n = self.n_frames
        start = max(0, min(self.trim_in_samples, n))
        end = n if self.trim_out_samples <= 0 else max(start, min(self.trim_out_samples, n))
        return start, end - start


def _destroy_quietly(scratch: NativeScratch, handle: Optional[int], path: Optional[Path] = None) -> None:
    """Best-effort cleanup after a failed checkout create/adopt. Swallows
    every error -- cleanup must not mask the original failure the caller
    is about to raise. `path` is passed only by a caller that minted the
    file itself (create_from_abs_range's freshly uuid4'd .wav); adopt_root
    / adopt_slice pass None because the file is a pre-existing one they
    do not own -- only the handle is theirs to release."""
    if handle is not None:
        try:
            scratch.checkout_destroy(handle)
        except Exception:
            pass
    if path is not None:
        try:
            path.unlink(missing_ok=True)
            Path(f"{path}.part").unlink(missing_ok=True)
        except Exception:
            pass


class CheckoutManager:
    """
    Creates, tracks, saves and discards Checkouts for one slot. A single
    CheckoutManager per CaptureSlot; all share one NativeScratch (the
    process-wide writer + cache).

    Thread-safety guarantee (R-h8l): every method that touches a
    `Checkout.handle` looks the checkout up AND uses its handle inside
    the SAME `self._lock` acquisition, so a concurrent `discard()`
    cannot free a handle out from under `write_state` / `resident_bytes`
    / `peak_bins` / `export_range` (a fetch-then-use gap there is a
    Zig-side use-after-free -- a crash, not a catchable exception).
    `export_range` holds the lock across its own file I/O too; today's
    only caller is the UI thread, so this trades a theoretical
    multi-exporter stall for correctness. Lock scope on the four
    creation paths is uneven, unchanged from before this fix:
    `create`/`create_from_abs_range` hold `self._lock` for the WHOLE
    body (`checkout_create`, the bins computation, and `_register`);
    `adopt_root`/`adopt_slice` hold it only around `_register` --
    `checkout_open`/`checkout_slice`/`checkout_info`/`checkout_peak_bins`
    run unlocked there, so two concurrent adoptions can run those calls
    in parallel (a deliberate cost/latency tradeoff at launch, not
    touched by R-h8l -- that ruling is about handle-freeing races on an
    ALREADY-registered checkout, not about serializing adoption itself).

    Every method that reaches the engine (creates, opens, slices, or
    exports through a Zig handle, including the manifest write that
    follows) converts `RuntimeError`/`OSError` from the engine into a
    single `RuntimeError` (R-h8k) -- callers see one exception type
    regardless of whether the underlying failure was a ring race, a
    corrupt scratch file, or an unwritable disk.
    """

    _VALID_FORMATS: tuple[str, ...] = ("WAV",)

    def __init__(
        self,
        buffer: NativeAudioCircularBuffer,
        scratch: NativeScratch,
        scratch_dir: Path | str,
        slot_name: str = "",
        max_active_checkouts: int = 16,
    ):
        self._buffer = buffer
        self._scratch = scratch
        self._scratch_dir = Path(scratch_dir)
        self._slot_name = slot_name
        self._max_active = int(max_active_checkouts)
        self._lock = threading.Lock()
        self._checkouts: dict[str, Checkout] = {}
        self._file_refs: dict[Path, int] = {}
        self._pinned_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create(self, duration_s: float, anchor_offset_s: float = 0.0) -> Checkout:
        """Snapshot `duration_s` seconds ending `anchor_offset_s` ago
        (0 = now). Both clamp to what the ring holds: the offset to
        `buffered - 1 sample`, the start to the readable window."""
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if anchor_offset_s < 0:
            raise ValueError("anchor_offset_s must be non-negative")
        buf = self._buffer
        sr = buf.sample_rate
        buffered_s = buf.buffered_seconds
        effective_offset_s = min(float(anchor_offset_s), max(0.0, buffered_s - 1.0 / sr))
        total = buf.total_written
        abs_end = total - int(effective_offset_s * sr)
        oldest = max(0, total - buf.buffer_size)
        abs_start = max(oldest, abs_end - int(duration_s * sr))
        if abs_end <= abs_start:
            raise RuntimeError("nothing buffered yet")
        return self.create_from_abs_range(abs_start, abs_end)

    def create_from_abs_range(self, abs_start: int, abs_end: int) -> Checkout:
        """Commit the exact absolute span `[abs_start, abs_end)` (the
        drag-select path). Raises RuntimeError when the span is past the
        head, already overwritten, or the count cap is hit."""
        if abs_end <= abs_start:
            raise ValueError(f"abs_end must be greater than abs_start ({abs_end} <= {abs_start})")
        buf = self._buffer
        total = buf.total_written
        if abs_end > total:
            raise RuntimeError(f"requested range extends past current head (abs_end={abs_end}, total_written={total})")
        if total - abs_start > buf.buffer_size:
            raise RuntimeError("requested range has already been overwritten")
        with self._lock:
            if len(self._checkouts) >= self._max_active:
                raise RuntimeError(f"Maximum active checkouts reached ({self._max_active})")
            cid = uuid.uuid4().hex[:12]
            path = audio_path(self._scratch_dir, cid)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = None
            try:
                # overwritten / out_of_range / io_error from the engine, or
                # an OSError from the manifest write below (an unwritable
                # scratch dir): every failure past this point surfaces as
                # ONE RuntimeError (R-h8k) -- the UI handler widens this
                # further in h10.
                handle = self._scratch.checkout_create(buf, int(abs_start), int(abs_end), path)
                co = Checkout(
                    id=cid, handle=handle, path=path,
                    sample_rate=buf.sample_rate, channels=buf.channels,
                    n_frames=int(abs_end - abs_start), start_frame=0,
                    abs_sample_start=int(abs_start), abs_sample_end=int(abs_end),
                )
                co.bins = {str(n): self._scratch.checkout_peak_bins(handle, n) for n in BIN_COUNTS}
                self._register(co)
            except (RuntimeError, OSError) as e:
                # R-h8m: this cid's path is freshly minted (uuid4) and was
                # never returned to a caller -- nothing else can reference
                # it, so cleanup on any failure past checkout_create is
                # unconditionally safe: destroy the handle (if one was
                # obtained) and delete the orphan .wav. Without this, a
                # bins/manifest failure leaves a manifest-less .wav on
                # disk forever -- adoption only scans *.json, so it would
                # never be found or cleaned up.
                _destroy_quietly(self._scratch, handle, path)
                raise RuntimeError(f"could not create checkout; {e}") from e
        return co

    def slice(self, parent_id: str, start: int, n: int) -> Checkout:
        """A saved segment `(parent file, start, n)`. Waits for the
        parent's file: a slice has no RAM copy, so its bins and audio
        come from disk (plan P13). Raises ScratchWriteFailed when the
        parent's write failed, and KeyError when the parent was discarded while
        this call waited for that write."""
        parent = self.get(parent_id)
        if start < 0 or n <= 0 or start + n > parent.n_frames:
            raise ValueError(f"slice {start}+{n} is outside the parent's {parent.n_frames} frames")
        # P13, outside self._lock: write_state takes the lock itself and
        # threading.Lock is not reentrant, and a wait under the lock
        # would stall every other manager call for up to 30 s.
        deadline = time.monotonic() + 30.0
        while True:
            ws = self.write_state(parent_id)
            if ws in ("written", "adopted"):
                break
            if ws == "failed":
                raise ScratchWriteFailed("scratch write failed for the parent; cannot slice")
            if time.monotonic() > deadline:
                raise RuntimeError("timed out waiting for the parent's scratch write")
            time.sleep(0.005)
        with self._lock:
            # R-h8l: re-look the parent up under the lock before touching
            # its handle. The wait above holds no lock for up to 30 s, so
            # a concurrent discard() can have freed the handle the `get`
            # before it returned -- using that is a Zig-side
            # use-after-free (a crash, not a catchable exception).
            if parent_id not in self._checkouts:
                raise KeyError(parent_id)
            parent = self._checkouts[parent_id]
            if len(self._checkouts) >= self._max_active:
                raise RuntimeError(f"Maximum active checkouts reached ({self._max_active})")
            handle = None
            try:
                handle = self._scratch.checkout_slice(parent.handle, int(start), int(n))
                co = Checkout(
                    id=uuid.uuid4().hex[:12], handle=handle, path=parent.path,
                    sample_rate=parent.sample_rate, channels=parent.channels,
                    n_frames=int(n), start_frame=int(parent.start_frame + start),
                    abs_sample_start=parent.abs_sample_start + int(start),
                    abs_sample_end=parent.abs_sample_start + int(start + n),
                    parent_id=parent.id, state="saved",
                )
                co.bins = {str(b): self._scratch.checkout_peak_bins(handle, b) for b in BIN_COUNTS}
                self._register(co)
            except (RuntimeError, OSError) as e:
                # Same rule as adopt_slice: `parent.path` is shared, owned
                # by the parent checkout's own refcount -- only this
                # slice's handle is ours to release.
                _destroy_quietly(self._scratch, handle, None)
                raise RuntimeError(f"could not slice checkout {parent_id}; {e}") from e
        return co

    def _register(self, co: Checkout) -> None:
        """Lock held. Write the manifest FIRST, then commit to the
        tracking dicts -- if the manifest write raises (a disk error),
        this checkout must not appear half-registered: the callers that
        clean up on failure (create_from_abs_range / adopt_root /
        adopt_slice) rely on `list()` and `file_refcount()` being
        unaffected by a `_register` that never finished."""
        self._write_manifest(co)
        self._checkouts[co.id] = co
        self._file_refs[co.path] = self._file_refs.get(co.path, 0) + 1

    def _write_manifest(self, co: Checkout) -> None:
        write_manifest(self._scratch_dir, Manifest(
            id=co.id, slot=self._slot_name, rate=co.sample_rate, channels=co.channels,
            abs_start=co.abs_sample_start, abs_end=co.abs_sample_end, created_at=time.time(),
            parent=co.parent_id, file=co.path.stem, start_frame=co.start_frame, n_frames=co.n_frames,
            trim_in=co.trim_in_samples, trim_out=co.trim_out_samples, state=co.state,
            partial=co.partial, bins=bins_to_json(co.bins) if co.bins else None,
        ))

    # ------------------------------------------------------------------
    # Adoption (launch)
    # ------------------------------------------------------------------

    def adopt_root(self, m: Manifest, audio: Path, partial: bool) -> Checkout:
        """A checkout opened directly over a file that already exists, at
        `m.start_frame` into it: 0 for a root, the slice's own offset for
        an orphaned slice whose parent left the manifests. Frame count
        comes from the file (a partial file reports its true prefix);
        bins from the manifest when present, else computed once from the
        file. An orphan keeps the `parent_id` its manifest recorded, so
        every later launch takes this same path."""
        start_frame = int(m.start_frame)
        handle = None
        try:
            handle = self._scratch.checkout_open(audio, start_frame, max(1, int(m.n_frames)))
            info = self._scratch.checkout_info(handle)
            co = Checkout(
                id=m.id, handle=handle, path=Path(audio),
                sample_rate=int(info.rate), channels=int(info.channels),
                n_frames=int(info.n_frames), start_frame=start_frame,
                abs_sample_start=int(m.abs_start), abs_sample_end=int(m.abs_end),
                parent_id=m.parent,
                trim_in_samples=int(m.trim_in), trim_out_samples=int(m.trim_out),
                state=m.state if m.state in ("pending", "ready", "saved") else "pending",
                partial=bool(partial or m.partial),
            )
            co.bins = bins_from_json(m.bins, co.channels)
            if set(co.bins) != {str(n) for n in BIN_COUNTS}:
                co.bins = {str(n): self._scratch.checkout_peak_bins(handle, n) for n in BIN_COUNTS}
            with self._lock:
                self._register(co)
        except (RuntimeError, OSError) as e:
            # R-h8k/R-h8m: unlike create_from_abs_range, `audio` is a
            # PRE-EXISTING file this manager did not create -- only the
            # handle is ours to release on failure, never the file.
            _destroy_quietly(self._scratch, handle)
            raise RuntimeError(f"could not adopt root {audio}; {e}") from e
        return co

    def adopt_slice(self, m: Manifest, parent: Checkout) -> Checkout:
        """A slice of an adopted parent in THIS manager. `m.start_frame`
        is absolute into the file (see the `Checkout` docstring) and
        `checkout_slice` wants it parent-relative, so the parent's own
        start comes off first -- without that, a slice of a slice
        re-opens `parent.start_frame` too far in. A negative result means
        the manifest disagrees with its parent (corruption): raise so
        `adopt_scratch` skips it, like any other corrupt manifest."""
        start = int(m.start_frame) - int(parent.start_frame)
        if start < 0:
            raise ValueError(f"slice {m.id} starts before its parent ({m.start_frame} < {parent.start_frame})")
        handle = None
        try:
            handle = self._scratch.checkout_slice(parent.handle, start, int(m.n_frames))
            co = Checkout(
                id=m.id, handle=handle, path=parent.path,
                sample_rate=parent.sample_rate, channels=parent.channels,
                n_frames=int(m.n_frames), start_frame=int(m.start_frame),
                abs_sample_start=int(m.abs_start), abs_sample_end=int(m.abs_end),
                parent_id=parent.id, trim_in_samples=int(m.trim_in), trim_out_samples=int(m.trim_out),
                state=m.state if m.state in ("pending", "ready", "saved") else "saved",
            )
            co.bins = bins_from_json(m.bins, co.channels)
            if set(co.bins) != {str(n) for n in BIN_COUNTS}:
                co.bins = {str(n): self._scratch.checkout_peak_bins(handle, n) for n in BIN_COUNTS}
            with self._lock:
                self._register(co)
        except (RuntimeError, OSError) as e:
            # Same rule as adopt_root: `parent.path` is shared, owned by
            # the parent checkout's own refcount -- only this slice's
            # handle is ours to release.
            _destroy_quietly(self._scratch, handle)
            raise RuntimeError(f"could not adopt slice {m.id}; {e}") from e
        return co

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def list(self) -> list[Checkout]:
        with self._lock:
            return list(self._checkouts.values())

    def get(self, checkout_id: str) -> Checkout:
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            return self._checkouts[checkout_id]

    def write_state(self, checkout_id: str) -> str:
        # R-h8l: look up AND use the handle under one lock acquisition --
        # `self.get(checkout_id).handle` would fetch the handle, release
        # the lock, THEN use it, leaving a gap where a concurrent
        # discard() can free it out from under this call.
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            handle = self._checkouts[checkout_id].handle
            return native.WRITE_STATES[self._scratch.checkout_info(handle).write_state]

    def resident_bytes(self, checkout_id: str) -> int:
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            handle = self._checkouts[checkout_id].handle
            return int(self._scratch.checkout_info(handle).resident_bytes)

    def peak_bins(self, checkout_id: str, n_bins: int) -> np.ndarray:
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            handle = self._checkouts[checkout_id].handle
            return self._scratch.checkout_peak_bins(handle, n_bins)

    def file_refcount(self, path: Path | str) -> int:
        with self._lock:
            return self._file_refs.get(Path(path), 0)

    # ------------------------------------------------------------------
    # UI state
    # ------------------------------------------------------------------

    def set_trim(self, checkout_id: str, trim_in: int, trim_out: int) -> None:
        # R-h8l (round 2): look up AND mutate under one lock acquisition --
        # `self.get(checkout_id)` fetches `co` and releases the lock before
        # this method touches it, the same fetch-then-use gap closed
        # elsewhere for write_state/resident_bytes/peak_bins/export_range.
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            co = self._checkouts[checkout_id]
            co.trim_in_samples = max(0, int(trim_in))
            co.trim_out_samples = max(co.trim_in_samples, int(trim_out)) if trim_out > 0 else 0
            self._write_manifest(co)  # takes no lock of its own -- safe to call here

    def pin(self, checkout_id: Optional[str]) -> None:
        """The selected clip: pinned (never evicted) and preloaded. One
        at a time per manager; None unpins."""
        # R-h8l (round 2): the previous shape released the lock between
        # recording `_pinned_id` and looking up `prev`'s/`checkout_id`'s
        # handle via `self.get(...)` -- both fetch-then-use gaps, plus
        # `self.get()` itself takes `self._lock`, so it CANNOT be called
        # from inside this block (would deadlock; `threading.Lock` is not
        # reentrant). Index `self._checkouts` directly instead.
        with self._lock:
            prev = self._pinned_id
            self._pinned_id = checkout_id
            prev_handle = None
            if prev and prev != checkout_id and prev in self._checkouts:
                prev_handle = self._checkouts[prev].handle
            cur_handle = None
            if checkout_id is not None:
                if checkout_id not in self._checkouts:
                    raise KeyError(checkout_id)
                cur_handle = self._checkouts[checkout_id].handle
            # checkout_pin is a raw ctypes call -- no lock of its own.
            if prev_handle is not None:
                self._scratch.checkout_pin(prev_handle, False)
            if cur_handle is not None:
                self._scratch.checkout_pin(cur_handle, True)

    # ------------------------------------------------------------------
    # Save / discard
    # ------------------------------------------------------------------

    def export_range(self, checkout_id: str, target_path: Path | str, start: int, n: int, subtype: str = _DEFAULT_SUBTYPE,
                     markers: tuple[int, int] | None = None) -> Path:
        """Materialise `[start, start + n)` of the checkout into a WAV.
        Zig reads the scratch file (or the RAM copy while the write is
        still in flight) — no audio crosses into Python.

        `markers` is `(slice_start, slice_end)` in frames RELATIVE TO THE
        EXPORTED FILE, end exclusive; Zig appends the cue/smpl/adtl
        chunks. A marker export needs the audio on disk, so it waits for
        the scratch write and raises if that write failed."""
        if subtype not in _VALID_SUBTYPES:
            raise ValueError(f"Unsupported subtype {subtype!r}; must be one of {_VALID_SUBTYPES}")
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # R-h8l: hold the lock across the handle-using export call itself
        # (not just the lookup) -- a concurrent discard() must not be able
        # to free the handle while checkout_export is reading through it.
        # This means export_range holds the lock for its own file I/O;
        # today's only caller is the UI thread, so a slow export stalls
        # other manager calls rather than racing a freed handle.
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            handle = self._checkouts[checkout_id].handle
            try:
                self._scratch.checkout_export(handle, target, int(start), int(n), subtype, markers)
            except (RuntimeError, OSError) as e:
                # R-h8k: one exception type out of every engine-reaching call.
                raise RuntimeError(f"could not export checkout {checkout_id}; {e}") from e
        return target

    def mint_trim(self, checkout_id: str) -> Optional[Checkout]:
        """The clip's trim as a saved slice, or None when there is no
        trim or the clip's scratch write failed: a slice needs the file,
        while the RAM copy still exports on its own. Save and drag both
        mint through here, so the same trim leaves as the same slice
        whichever way it goes."""
        co = self.get(checkout_id)
        if not co.has_trim():
            return None
        try:
            return self.slice(checkout_id, *co.trim_range())
        except ScratchWriteFailed:
            return None

    def save(
        self,
        checkout_id: str,
        target_path: Path | str,
        fmt: CheckoutFormat = "WAV",
        trimmed: bool = True,
        subtype: CheckoutSubtype | None = None,
        mark_saved: bool = True,
    ) -> Checkout:
        """Write the clip, or its trim, to `target_path`. Returns the
        checkout the file came from: the slice a trimmed save mints, or
        the clip itself. A minted slice is already `saved` and the clip
        stays as it was; otherwise `mark_saved` flips the clip."""
        fmt = fmt.upper()  # type: ignore[assignment]
        if fmt not in self._VALID_FORMATS:
            raise ValueError(f"Unsupported format {fmt!r}; must be one of {self._VALID_FORMATS}")
        co = self.get(checkout_id)
        start, n = co.trim_range() if trimmed else (0, co.n_frames)
        minted = self.mint_trim(checkout_id) if trimmed else None
        try:
            self.export_range(checkout_id, target_path, start, n, subtype or _DEFAULT_SUBTYPE)
        except BaseException:
            # An export that fails must not strand the slice (a `saved`
            # manifest adoption would resurrect). Cleanup errors stay
            # quiet so the export error is the one the caller sees.
            if minted is not None:
                try:
                    self.discard(minted.id)
                except Exception:
                    pass
            raise
        if minted is not None:
            return minted
        if mark_saved:
            self.mark_saved(checkout_id)
        return co

    def mark_saved(self, checkout_id: str) -> None:
        # R-h8l (round 2): same fetch-then-use fix as set_trim.
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            co = self._checkouts[checkout_id]
            co.state = "saved"
            self._write_manifest(co)

    def discard(self, checkout_id: str) -> None:
        """Destroy the handle, delete the manifest, delete the WAV when
        no other checkout references it."""
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            co = self._checkouts.pop(checkout_id)
            co.state = "discarded"
            if self._pinned_id == checkout_id:
                self._pinned_id = None
            self._scratch.checkout_destroy(co.handle)  # waits for any job on it
            manifest_path(self._scratch_dir, co.id).unlink(missing_ok=True)
            refs = self._file_refs.get(co.path, 0) - 1
            if refs <= 0:
                self._file_refs.pop(co.path, None)
                co.path.unlink(missing_ok=True)
                Path(f"{co.path}.part").unlink(missing_ok=True)
            else:
                self._file_refs[co.path] = refs

    def discard_all(self) -> None:
        for co in self.list():
            self.discard(co.id)

    def close(self) -> None:
        """Release every handle and keep every file: the next launch
        adopts them. Shutdown path."""
        with self._lock:
            for co in self._checkouts.values():
                self._scratch.checkout_destroy(co.handle)
            self._checkouts.clear()
            self._file_refs.clear()
            self._pinned_id = None
