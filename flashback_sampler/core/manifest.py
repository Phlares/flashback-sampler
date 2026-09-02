"""Per-checkout manifest: the JSON sidecar next to a scratch WAV.

The manifest is what adoption reads at launch: identity, provenance
(slot, absolute ring range), the checkout that owns the file and the
range of it this checkout covers, its parent for a slice, trim, state,
and the deck's peak bins (so a launch with gigabytes of scratch draws
the deck without reading audio).

Pure Python, no Qt, no engine calls. Bins travel as flat float lists in
the numpy layout (n_bins, 2, channels); `bins_to_json` / `bins_from_json`
convert.

`created_at` here is a wall-clock stamp set once at manifest creation
and preserved by every later rewrite (trim, mark_saved, ...). The
preservation is mechanical, in `write_manifest` itself: it reads
whatever manifest already sits at the target path and keeps THAT
file's `created_at`, using the incoming value only when nothing is
there yet. A caller is free to re-stamp `created_at` on the `Manifest`
it passes in (h8's `_write_manifest` does, on every trim/mark_saved) —
`write_manifest` overrides it right back to the on-disk value.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Optional

import numpy as np

PART_SUFFIX = ".wav.part"


@dataclass
class Manifest:
    id: str
    slot: str
    rate: int
    channels: int
    abs_start: int
    abs_end: int
    created_at: float
    parent: Optional[str]
    file: str  # id of the checkout whose `<id>.wav` holds the audio; a root names itself
    start_frame: int
    n_frames: int
    trim_in: int
    trim_out: int
    state: str
    partial: bool
    bins: Optional[dict]  # {"540": [floats], "360": [floats]} or None


_FIELDS = {f.name for f in fields(Manifest)}
_REQUIRED = _FIELDS - {"file"}


def manifest_path(scratch_dir: Path | str, checkout_id: str) -> Path:
    return Path(scratch_dir) / f"{checkout_id}.json"


def audio_path(scratch_dir: Path | str, checkout_id: str) -> Path:
    return Path(scratch_dir) / f"{checkout_id}.wav"


def write_manifest(scratch_dir: Path | str, m: Manifest) -> Path:
    """Atomic: temp file + `Path.replace` (== `os.replace`), so a crash
    mid-write leaves the old manifest (or none), never a torn one. On
    Windows `os.replace` fails if the destination is open elsewhere;
    nothing in this codebase holds a manifest file open, so the replace
    is safe here, but a future reader that keeps a handle open would
    break this.

    `created_at` is written once and preserved on every rewrite — NOT
    by caller convention, but mechanically: if a manifest already
    exists at this id's path, its `created_at` wins over whatever `m`
    carries. A caller may pass a freshly re-stamped `created_at` (h8's
    trim/mark_saved does) without breaking this; only a genuinely new
    id keeps the incoming value."""
    p = manifest_path(scratch_dir, m.id)
    existing = read_manifest(p)
    if existing is not None:
        m = replace(m, created_at=existing.created_at)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(asdict(m), f)
    tmp.replace(p)
    return p


def read_manifest(path: Path | str) -> Optional[Manifest]:
    """None for anything that is not a complete manifest — adoption
    skips it and leaves the file in place."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # Subset check, not equality: the TypeError catch below cannot tell
    # a missing required field from one this reader fills in (`file`),
    # so the guard is what enforces "every required field is present".
    if not isinstance(data, dict) or not (_REQUIRED <= set(data)):
        return None
    # `file` arrived after the first manifests were written. An old
    # root owns its own file; an old slice's best guess is its parent's,
    # which is what adoption assumed then. The next rewrite stores the
    # real value.
    data.setdefault("file", data["parent"] if data["parent"] is not None else data["id"])
    try:
        return Manifest(**{k: data[k] for k in _FIELDS})
    except TypeError:
        return None


def scan(scratch_dir: Path | str) -> list[Manifest]:
    """Every readable manifest, roots first (a slice needs its parent
    adopted already), then by creation time, then by id. The id
    tiebreak matters: `write_manifest` preserves `created_at` across
    rewrites (see its docstring), but `time.time()` resolution on
    Windows is ~15.6 ms, so two manifests created in the same tick
    would otherwise adopt in whatever order `glob` happens to enumerate
    them — this makes that order deterministic instead of incidental."""
    d = Path(scratch_dir)
    if not d.is_dir():
        return []
    found = [m for m in (read_manifest(p) for p in d.glob("*.json")) if m is not None]
    found.sort(key=lambda m: (m.parent is not None, m.created_at, m.id))
    return found


def resolve_audio(scratch_dir: Path | str, m: Manifest) -> Optional[tuple[Path, bool]]:
    """(path, partial) for the audio `m` lives in: `<file>.wav`, its own
    for a root, the root's for a slice. The `.wav` wins; a lone
    `<file>.wav.part` (crash mid-write) is renamed into place and flagged
    partial — the reader clamps to what it holds. None when neither
    exists. Never deletes anything."""
    wav = audio_path(scratch_dir, m.file)
    if wav.exists():
        return wav, False
    part = Path(scratch_dir) / f"{m.file}{PART_SUFFIX}"
    if part.exists():
        part.rename(wav)
        return wav, True
    return None


def bins_to_json(bins: dict[str, np.ndarray]) -> dict:
    return {k: np.asarray(v, dtype=np.float32).ravel().tolist() for k, v in bins.items()}


def bins_from_json(d: Optional[dict], channels: int) -> dict[str, np.ndarray]:
    """Same skip-don't-crash contract as `read_manifest`: a corrupt key
    (not an int string), a non-dict `bins`, or non-numeric contents are
    dropped rather than raised — adoption drops one bin size, not the
    whole checkout."""
    out: dict[str, np.ndarray] = {}
    if not isinstance(d, dict):
        return out
    for k, flat in d.items():
        try:
            n_bins = int(k)
            arr = np.asarray(flat, dtype=np.float32)
            if arr.size != n_bins * 2 * channels:
                continue
            out[k] = arr.reshape(n_bins, 2, channels)
        except (ValueError, TypeError):
            continue
    return out
