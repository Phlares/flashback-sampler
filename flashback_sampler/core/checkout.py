"""
Checkout workflow — pull immutable snapshots of the live ring buffer.

Mental model (user-provided): a DJ with one turntable still spinning,
pulling a record off the rack to audition. The ring buffer keeps writing
throughout. Each Checkout is a frozen, in-RAM copy of a slice of the ring.
The user can scrub, trim, preview, then save to WAV/FLAC or discard.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import soundfile as sf

from .buffer import AudioCircularBuffer


CheckoutState = Literal["pending", "ready", "saved", "discarded"]
CheckoutFormat = Literal["WAV", "FLAC"]


@dataclass
class Checkout:
    """
    A frozen snapshot of a ring-buffer slice.

    `audio` is the in-RAM float32 [N, channels] copy that the UI scrubs
    and plays back. `abs_sample_start` / `abs_sample_end` are the absolute
    sample positions (in total_written space) at creation time — metadata
    that lets the UI display "this clip is T-3:00 → T-0:00" accurately
    even after the ring has moved on.
    """

    id: str
    created_at: float  # monotonic
    sample_rate: int
    channels: int
    audio: np.ndarray  # (N, channels) float32
    abs_sample_start: int
    abs_sample_end: int
    trim_in_samples: int = 0
    trim_out_samples: int = 0
    temp_path: Optional[Path] = None
    state: CheckoutState = "pending"

    @property
    def duration_seconds(self) -> float:
        return self.audio.shape[0] / self.sample_rate

    @property
    def ram_bytes(self) -> int:
        return int(self.audio.nbytes)

    def trimmed_audio(self) -> np.ndarray:
        """Return the portion between trim_in and trim_out (defaults = full)."""
        n = self.audio.shape[0]
        start = max(0, min(self.trim_in_samples, n))
        if self.trim_out_samples <= 0:
            end = n
        else:
            end = max(start, min(self.trim_out_samples, n))
        return self.audio[start:end]


class CheckoutManager:
    """
    Creates, tracks, saves, and discards Checkouts.

    A single CheckoutManager instance is held by the app's AppState and
    shared across the UI controllers. Core operations (`create`, `save`,
    `discard`, `list`) are thread-safe.
    """

    _VALID_FORMATS: tuple[str, ...] = ("WAV", "FLAC")

    def __init__(
        self,
        buffer: AudioCircularBuffer,
        max_active_checkouts: int = 16,
        max_total_ram_mb: float = 1024.0,
    ):
        self._buffer = buffer
        self._max_active = int(max_active_checkouts)
        self._max_ram_bytes = int(max_total_ram_mb * 1024 * 1024)
        self._lock = threading.Lock()
        self._checkouts: dict[str, Checkout] = {}

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create(self, duration_s: float, anchor: str = "latest") -> Checkout:
        """
        Create a new Checkout by snapshotting `duration_s` seconds of audio
        from the buffer.

        Currently only `anchor="latest"` is supported (take the most
        recent N seconds). Additional anchors ("oldest", "from_mark") will
        land with the UI integration in later milestones.
        """
        if anchor != "latest":
            raise NotImplementedError(f"anchor={anchor!r} not yet supported")
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")

        # Pull the slice BEFORE checking caps — otherwise clamped-duration
        # checkouts would produce stale cap estimates.
        audio = self._buffer.get_latest(duration_s)
        # Snapshot abs sample range under the buffer's lock (cheap) for metadata
        with self._buffer._lock:  # noqa: SLF001 — internal coordination
            abs_end = self._buffer.total_written
            abs_start = abs_end - audio.shape[0]

        # Check caps atomically with insertion
        with self._lock:
            if len(self._checkouts) >= self._max_active:
                raise RuntimeError(
                    f"Maximum active checkouts reached ({self._max_active})"
                )
            prospective_bytes = (
                sum(c.ram_bytes for c in self._checkouts.values()) + audio.nbytes
            )
            if prospective_bytes > self._max_ram_bytes:
                raise RuntimeError(
                    f"Checkout RAM cap exceeded: "
                    f"{prospective_bytes / 1024 / 1024:.1f} MB > "
                    f"{self._max_ram_bytes / 1024 / 1024:.1f} MB"
                )

            co = Checkout(
                id=uuid.uuid4().hex[:12],
                created_at=time.monotonic(),
                sample_rate=self._buffer.sample_rate,
                channels=self._buffer.channels,
                audio=audio,
                abs_sample_start=int(abs_start),
                abs_sample_end=int(abs_end),
                state="pending",
            )
            self._checkouts[co.id] = co
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

    # ------------------------------------------------------------------
    # Save / discard
    # ------------------------------------------------------------------

    def save(
        self,
        checkout_id: str,
        target_path: Path | str,
        fmt: CheckoutFormat = "WAV",
    ) -> Path:
        """
        Write the checkout's (trimmed) audio to `target_path` in the
        requested format and mark the checkout as `saved`.
        """
        fmt = fmt.upper()  # type: ignore[assignment]
        if fmt not in self._VALID_FORMATS:
            raise ValueError(
                f"Unsupported format {fmt!r}; must be one of {self._VALID_FORMATS}"
            )

        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            co = self._checkouts[checkout_id]
            audio = co.trimmed_audio()
            sr = co.sample_rate

        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(target), audio, sr, format=fmt)

        with self._lock:
            co.state = "saved"
        return target

    def discard(self, checkout_id: str) -> None:
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            co = self._checkouts.pop(checkout_id)
            co.state = "discarded"
            # Drop the big ndarray so RAM is reclaimed promptly
            co.audio = np.zeros((0, co.channels), dtype=np.float32)
