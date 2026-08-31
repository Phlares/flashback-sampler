"""Per-checkout manifest: the JSON sidecar next to a scratch WAV.

The manifest is what adoption reads at launch: identity, provenance
(slot, absolute ring range), the file range this checkout covers, its
parent for a slice, trim, state, and the deck's peak bins (so a launch
with gigabytes of scratch draws the deck without reading audio).

Pure Python, no Qt, no engine calls. Bins travel as flat float lists in
the numpy layout (n_bins, 2, channels); `bins_to_json` / `bins_from_json`
convert.

`created_at` here is a wall-clock stamp set once at manifest creation
and preserved by every later rewrite (trim, mark_saved, ...) — it is
NOT `Checkout.created_at` (a `time.monotonic()` value scoped to one
process run, used for in-memory LRU ordering only). `write_manifest`
never stamps this field itself; a caller that wants the "written once,
preserved on rewrite" contract gets it for free by round-tripping the
value it read, not by regenerating it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
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
    start_frame: int
    n_frames: int
    trim_in: int
    trim_out: int
    state: str
    partial: bool
    bins: Optional[dict]  # {"540": [floats], "360": [floats]} or None


_FIELDS = {f.name for f in fields(Manifest)}


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

    Serializes `m` as given — it does not touch `m.created_at`, so a
    caller that reads-modifies-writes (h8's trim/mark_saved) preserves
    creation order across rewrites automatically."""
    p = manifest_path(scratch_dir, m.id)
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
    if not isinstance(data, dict) or set(data) != _FIELDS:
        return None
    try:
        return Manifest(**data)
    except TypeError:
        return None


def scan(scratch_dir: Path | str) -> list[Manifest]:
    """Every readable manifest, roots first (a slice needs its parent
    adopted already), then by creation time. Because `write_manifest`
    never touches `created_at`, this order is stable across rewrites."""
    d = Path(scratch_dir)
    if not d.is_dir():
        return []
    found = [m for m in (read_manifest(p) for p in d.glob("*.json")) if m is not None]
    found.sort(key=lambda m: (m.parent is not None, m.created_at))
    return found


def resolve_audio(scratch_dir: Path | str, m: Manifest) -> Optional[tuple[Path, bool]]:
    """(path, partial) for a root's audio. `<id>.wav` wins; a lone
    `<id>.wav.part` (crash mid-write) is renamed into place and flagged
    partial — the reader clamps to what it holds. None when neither
    exists. Never deletes anything."""
    wav = audio_path(scratch_dir, m.id)
    if wav.exists():
        return wav, False
    part = Path(scratch_dir) / f"{m.id}{PART_SUFFIX}"
    if part.exists():
        part.rename(wav)
        return wav, True
    return None


def bins_to_json(bins: dict[str, np.ndarray]) -> dict:
    return {k: np.asarray(v, dtype=np.float32).ravel().tolist() for k, v in bins.items()}


def bins_from_json(d: Optional[dict], channels: int) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for k, flat in (d or {}).items():
        n_bins = int(k)
        arr = np.asarray(flat, dtype=np.float32)
        if arr.size != n_bins * 2 * channels:
            continue
        out[k] = arr.reshape(n_bins, 2, channels)
    return out
