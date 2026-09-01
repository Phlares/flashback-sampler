"""
Render a checkout into the export pool for an OS drag-out.

Pure Python — no Qt; the write goes through CheckoutManager.save and
fb_wav_write. The app layer decides when to render (drag-start) and
what to do afterward (mark saved on drop, delete on cancel); this
module only owns naming.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .checkout import CheckoutManager, CheckoutSubtype

_MAX_COLLISION_SUFFIX = 999


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


def render_drag_file(
    manager: CheckoutManager,
    checkout_id: str,
    pool_dir: Path | str,
    source_name: str,
    *,
    bit_depth: CheckoutSubtype = "FLOAT",
    trimmed: bool = True,
    now: datetime | None = None,
) -> Path:
    """
    Write the checkout's (trimmed) audio to the export pool and return
    the path. Does NOT mark the checkout saved — the caller commits that
    only once the drop target has accepted the file.
    """
    co = manager.get(checkout_id)
    start, n = co.trim_range() if trimmed else (0, co.n_frames)
    duration_s = n / co.sample_rate
    when = now or datetime.now()
    pool = Path(pool_dir)
    pool.mkdir(parents=True, exist_ok=True)
    target = resolve_collision(pool / drag_filename(source_name, when, duration_s))
    manager.export_range(checkout_id, target, start, n, bit_depth)
    return target
