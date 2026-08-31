"""
Checkout workflow — pull immutable snapshots of the live ring buffer.

Mental model (user-provided): a DJ with one turntable still spinning,
pulling a record off the rack to audition. The ring buffer keeps writing
throughout. Each Checkout is a frozen, in-RAM copy of a slice of the ring.
The user can scrub, trim, preview, then save to WAV or discard.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import numpy as np

from .buffer import RingDerivedOps
from flashback_sampler.core import native


CheckoutState = Literal["pending", "ready", "saved", "discarded"]
CheckoutFormat = Literal["WAV"]
CheckoutSubtype = Literal["FLOAT", "PCM_24", "PCM_16"]

# FLOAT keeps the float32 ring bit-perfect on disk (fb_wav_write memcpy).
_DEFAULT_SUBTYPE: dict[str, str] = {"WAV": "FLOAT"}
_VALID_SUBTYPES: tuple[str, ...] = ("FLOAT", "PCM_24", "PCM_16")


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

    _VALID_FORMATS: tuple[str, ...] = ("WAV",)

    def __init__(
        self,
        buffer: RingDerivedOps,
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

    def create(
        self,
        duration_s: float,
        anchor: str = "latest",
        anchor_offset_s: float = 0.0,
    ) -> Checkout:
        """
        Create a new Checkout by snapshotting `duration_s` seconds of audio
        from the buffer.

        `anchor="latest"` is the only supported anchor today.
        `anchor_offset_s` shifts the trailing edge of the slice earlier in
        time. 0.0 (default) ends the clip at "now"; 60.0 ends it 60 s ago.
        This is how the rotary knob moves a checkout back in time through
        the ring buffer.
        """
        if anchor != "latest":
            raise NotImplementedError(f"anchor={anchor!r} not yet supported")
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if anchor_offset_s < 0:
            raise ValueError("anchor_offset_s must be non-negative")

        # Defensive clamp: the rotary UI can be dragged to anchor offsets
        # up to the buffer's capacity, but early in capture only a few
        # seconds of audio actually exist. We clamp the effective offset
        # to leave at least a tiny span of audio for get_segment to
        # return — concretely, offset_max = max(0, buffered_s - 1 sample)
        # so the window always contains at least one sample when
        # buffered_s > 0. get_segment then internally clamps the older
        # boundary to what's actually available, so the returned clip is
        # everything the ring has from the anchor going backward.
        buffered_s = self._buffer.buffered_seconds
        one_sample = 1.0 / float(self._buffer.sample_rate)
        effective_offset_s = min(
            float(anchor_offset_s),
            max(0.0, buffered_s - one_sample),
        )

        if effective_offset_s <= 0:
            # Fast path — unchanged from before
            audio = self._buffer.get_latest(duration_s)
        else:
            # Resolve to a segment ending `effective_offset_s` seconds ago.
            # get_segment clamps start_ago to avail_secs, so a duration
            # request larger than what remains before the offset yields
            # everything up to the offset point.
            audio = self._buffer.get_segment(
                start_ago=effective_offset_s + duration_s,
                end_ago=effective_offset_s,
            )
        # total_written is a single atomic read on both implementations
        # (a GIL-protected int attribute on AudioCircularBuffer, an atomic
        # ctypes call on NativeAudioCircularBuffer) — no lock needed, and
        # NativeAudioCircularBuffer has no self._lock to reach for.
        total = self._buffer.total_written
        abs_end = total - int(effective_offset_s * self._buffer.sample_rate)
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

    def create_from_abs_range(
        self,
        abs_start: int,
        abs_end: int,
    ) -> Checkout:
        """
        Create a Checkout from an absolute sample range in
        `total_written` space. Used by the drag-select UI — the user
        picks a range on the live waveform (which the BufferTrack pins
        to absolute samples so the selection stays anchored to real
        audio even as the ring scrolls), then right-clicks → Check Out
        Segment to commit those exact samples.

        Raises RuntimeError if the requested range has already scrolled
        out of the ring, or has not yet been written, or if capacity
        caps would be exceeded.
        """
        if abs_end <= abs_start:
            raise ValueError(
                f"abs_end must be greater than abs_start ({abs_end} <= {abs_start})"
            )

        # Check the range is still available in the ring. total_written is
        # a single atomic read on both implementations — see create()'s
        # comment above for why no lock is needed here.
        buf = self._buffer
        total = buf.total_written
        if abs_end > total:
            raise RuntimeError(
                f"requested range extends past current head "
                f"(abs_end={abs_end}, total_written={total})"
            )
        if total - abs_start > buf.buffer_size:
            raise RuntimeError(
                "requested range has already been overwritten"
            )

        audio = buf.copy_abs_range(abs_start, abs_end)
        if audio.shape[0] == 0:
            raise RuntimeError(
                "could not read requested range; the writer may have "
                "lapped the slice during the copy"
            )

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
                sample_rate=buf.sample_rate,
                channels=buf.channels,
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
        trimmed: bool = True,
        subtype: CheckoutSubtype | None = None,
        mark_saved: bool = True,
    ) -> Path:
        """
        Write the checkout's audio to `target_path` in the requested
        format. When `trimmed` is True (default) the file contains just
        the region between trim_in_samples / trim_out_samples; when False,
        the full untrimmed snapshot is written regardless of trim state.

        `subtype` controls the bit depth; None resolves to FLOAT.
        `mark_saved` controls whether the checkout state is flipped to
        'saved' (default True); when False, the caller can write without
        affecting checkout state (used by drag-out flow).
        """
        fmt = fmt.upper()  # type: ignore[assignment]
        if fmt not in self._VALID_FORMATS:
            raise ValueError(
                f"Unsupported format {fmt!r}; must be one of {self._VALID_FORMATS}"
            )

        if subtype is None:
            subtype = _DEFAULT_SUBTYPE[fmt]  # type: ignore[assignment]
        if subtype not in _VALID_SUBTYPES:
            raise ValueError(
                f"Unsupported subtype {subtype!r}; must be one of {_VALID_SUBTYPES}"
            )

        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            co = self._checkouts[checkout_id]
            audio = co.trimmed_audio() if trimmed else co.audio
            sr = co.sample_rate

        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # The Zig encoder is the only write path; native.wav_write raises
        # RuntimeError when the library is missing.
        native.wav_write(target, np.ascontiguousarray(audio, dtype=np.float32), sr, subtype)

        if mark_saved:
            with self._lock:
                co.state = "saved"
        return target

    def mark_saved(self, checkout_id: str) -> None:
        """Flip a checkout to `saved` without writing anything — used by
        the drag-out flow, which renders first and only commits the state
        once the drop target has accepted the file."""
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            self._checkouts[checkout_id].state = "saved"

    def discard(self, checkout_id: str) -> None:
        with self._lock:
            if checkout_id not in self._checkouts:
                raise KeyError(checkout_id)
            co = self._checkouts.pop(checkout_id)
            co.state = "discarded"
            # Drop the big ndarray so RAM is reclaimed promptly
            co.audio = np.zeros((0, co.channels), dtype=np.float32)
