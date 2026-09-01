"""
Render a checkout into the export pool for an OS drag-out.

Pure Python — no Qt; the write goes through CheckoutManager and the Zig
exporter. The app layer decides when to render (drag-start) and what to
do afterward (mark saved on drop, delete on cancel); this module owns
naming, the export span policy, and which checkout the drop commits.

Two renderers: `render_root_drag` writes the whole checkout (markers at
its trim on request, nothing minted); `render_slice_drag` mints a saved
slice of a trimmed clip and writes the slice plus handle audio around
it, markers at the slice.

`alc=True` adds an Ableton Live Clip sidecar naming the marked bounds --
only where there ARE markers, since a full-clip drag has no bounds to
write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .alc import write_alc
from .checkout import CheckoutManager, CheckoutSubtype

_MAX_COLLISION_SUFFIX = 999

BYTES_PER_SAMPLE = {"FLOAT": 4, "PCM_24": 3, "PCM_16": 2}


@dataclass(frozen=True)
class DragRender:
    path: Path
    checkout_id: str  # the checkout the drop commits (a minted slice, or the clip itself)
    minted: bool      # True when the render created a slice checkout
    sidecar: Path | None = None  # the .alc offered alongside `path`, when written


def sanitize_source_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "").strip("_").lower()
    return base or "source"


def drag_filename(source_name: str, when: datetime, duration_s: float) -> str:
    return (
        f"{sanitize_source_name(source_name)}"
        f"_{when:%Y%m%d-%H%M%S}_{duration_s:.1f}s.wav"
    )


def resolve_collision(target: Path) -> Path:
    """Return `target`, or the first free `<stem>_N<suffix>` sibling."""
    if not target.exists():
        return target
    for i in range(2, _MAX_COLLISION_SUFFIX + 1):
        candidate = target.with_name(f"{target.stem}_{i}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"no free collision suffix for {target}")


def export_span(
    parent_frames: int,
    slice_start: int,
    slice_end: int,
    channels: int,
    bytes_per_sample: int,
    handle_mb: float,
) -> tuple[int, int]:
    """The parent span to export around a slice: the WHOLE slice plus up
    to `handle_mb` of extra parent audio, half before and half after,
    clamped to the parent. The slice is never truncated. 0 = the slice
    alone; ∞ = the whole parent. A clamp at one edge does not move the
    other (the file just gets smaller)."""
    if handle_mb <= 0:
        return slice_start, slice_end
    half = (int(handle_mb * 2**20) // (channels * bytes_per_sample)) // 2
    return max(0, slice_start - half), min(parent_frames, slice_end + half)


def _export(
    manager: CheckoutManager,
    checkout_id: str,
    target: Path,
    lo: int,
    n: int,
    bit_depth: CheckoutSubtype,
    markers: tuple[int, int] | None,
    alc: bool,
    rate: int,
) -> Path | None:
    """Write the WAV, then the `.alc` sidecar when one was asked for and
    there are markers to put in it. Returns the sidecar path or None.

    Any failure unlinks both files before re-raising: the caller is told
    the render failed and never receives a DragRender, so anything left
    in the pool is a file nobody owns. `markers` are frames into the
    EXPORTED file; the sidecar wants them in seconds."""
    sidecar = target.with_suffix(".alc")
    try:
        manager.export_range(checkout_id, target, lo, n, bit_depth, markers=markers)
        if not (alc and markers):
            return None
        return write_alc(sidecar, target, markers[0] / rate, markers[1] / rate, frames=n, rate=rate)
    except BaseException:
        target.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise


def _target(pool_dir: Path | str, source_name: str, duration_s: float, now: datetime | None) -> Path:
    pool = Path(pool_dir)
    pool.mkdir(parents=True, exist_ok=True)
    return resolve_collision(pool / drag_filename(source_name, now or datetime.now(), duration_s))


def render_root_drag(
    manager: CheckoutManager,
    checkout_id: str,
    pool_dir: Path | str,
    source_name: str,
    *,
    bit_depth: CheckoutSubtype = "FLOAT",
    markers_at_trim: bool = False,
    alc: bool = False,
    now: datetime | None = None,
) -> DragRender:
    """The whole checkout, optionally with markers at its trim (the
    buffer-deck drag: the root IS the segment, its trim the slice).
    Does NOT mark the checkout saved — the caller commits that only once
    the drop target has accepted the file."""
    co = manager.get(checkout_id)
    markers = None
    if markers_at_trim and co.has_trim():
        start, n = co.trim_range()
        markers = (start, start + n)
    target = _target(pool_dir, source_name, co.duration_seconds, now)
    sidecar = _export(
        manager, checkout_id, target, 0, co.n_frames, bit_depth, markers, alc, co.sample_rate
    )
    return DragRender(target, checkout_id, False, sidecar)


def render_slice_drag(
    manager: CheckoutManager,
    checkout_id: str,
    pool_dir: Path | str,
    source_name: str,
    *,
    bit_depth: CheckoutSubtype = "FLOAT",
    handle_mb: float = 0.0,
    alc: bool = False,
    now: datetime | None = None,
) -> DragRender:
    """The clip-deck drag of a trimmed band: mint a saved slice, export
    the whole slice plus up to handle_mb of parent audio around it, with
    markers at the slice. An untrimmed clip has no slice to mint: the
    whole clip goes."""
    co = manager.get(checkout_id)
    if not co.has_trim():
        return render_root_drag(
            manager, checkout_id, pool_dir, source_name, bit_depth=bit_depth, alc=alc, now=now
        )
    start, n = co.trim_range()
    # Mint BEFORE the export: the plan names the file for the slice, so
    # the slice has to exist first. That leaves the export as the only
    # step that can strand it -- the caller never receives a DragRender,
    # so it has no id to discard, and adoption would resurrect the orphan
    # (state `saved`, a manifest on disk, a refcount on the parent file)
    # at the next launch. Undo the mint here, where the id is still known.
    s = manager.slice(checkout_id, start, n)
    try:
        lo, hi = export_span(co.n_frames, start, start + n, co.channels, BYTES_PER_SAMPLE[bit_depth], handle_mb)
        target = _target(pool_dir, source_name, n / co.sample_rate, now)
        # Markers are relative to the EXPORTED file, so rebase by lo.
        sidecar = _export(
            manager, checkout_id, target, lo, hi - lo, bit_depth,
            (start - lo, start + n - lo), alc, co.sample_rate,
        )
    except BaseException:
        # discard() drops the manifest and the refcount; the parent's file
        # survives on the parent's own reference. Swallowed like
        # _destroy_quietly: a cleanup that fails must not replace the
        # export error the caller actually needs to see.
        try:
            manager.discard(s.id)
        except Exception:
            pass
        raise
    return DragRender(target, s.id, True, sidecar)
