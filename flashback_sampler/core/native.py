"""
ctypes bindings for the Zig core (core/ -> flashback_core shared library).

Mirrors core/include/flashback_core.h. NativeAudioCircularBuffer is a
drop-in for AudioCircularBuffer: Zig owns the memory, the write path,
span reads, summary aggregation, and WAV encoding; Python keeps the
visualization readers over a ZERO-COPY numpy view of Zig-owned storage
(same seqlock verify, no lock -- the atomics are on the Zig side).

TWO SIZES, not one: fb_ring_capacity() is the READABLE window (what
Python's buffer_size reports, what get_latest/get_segment clamp against).
fb_ring_storage_frames() is the PHYSICAL frame count backing
fb_ring_storage -- capacity plus a guard band (see flashback_core.h and
Ring.zig's struct-level comment) that makes an accepted reader's span
provably disjoint from whatever the writer might currently be mid-copy
into. The zero-copy numpy view over fb_ring_storage MUST be shaped with
storage_frames, and write_pos's modulo MUST wrap at storage_frames too --
using capacity for either silently corrupts get_peak_bins, which walks
the raw buffer directly.
"""
from __future__ import annotations

import ctypes as C
import sys
from pathlib import Path

import numpy as np

from flashback_sampler.core.buffer import RingDerivedOps, _peak_bins_impl

_OK, _OVERWRITTEN, _OUT_OF_RANGE, _IO_ERROR, _INVALID_ARG = range(5)
# Public: mirrors flashback_core.h's FbSubtype and checkout.py's
# CheckoutSubtype strings. Not yet wired into checkout.py (that routing
# is future work, not current behavior) -- when it is, only subtypes
# present here should go to the native encoder, with anything else
# falling back to soundfile.
SUBTYPE_INTS = {"FLOAT": 0, "PCM_24": 1, "PCM_16": 2}

_lib: C.CDLL | None = None
_lib_tried = False


def _candidates() -> list[Path]:
    names = {"win32": "flashback_core.dll", "darwin": "libflashback_core.dylib"}
    name = names.get(sys.platform, "libflashback_core.so")
    here = Path(__file__).resolve()
    repo = here.parents[2]
    return [
        here.parent / name,                         # bundled (PyInstaller / wheel)
        repo / "core" / "zig-out" / "bin" / name,    # dev build (Windows DLLs land in bin/)
        repo / "core" / "zig-out" / "lib" / name,    # dev build (unix)
    ]


def load() -> C.CDLL | None:
    """Load and memoize the core library; None if not built anywhere."""
    global _lib, _lib_tried
    if _lib_tried:
        return _lib
    _lib_tried = True
    for path in _candidates():
        if not path.exists():
            continue
        lib = C.CDLL(str(path))
        _declare(lib)
        _lib = lib
        break
    return _lib


def _declare(lib: C.CDLL) -> None:
    """Argument/return types mirroring core/include/flashback_core.h,
    export by export. A mismatch here is silent memory corruption, not
    an error -- ctypes trusts these declarations completely."""
    f32p = C.POINTER(C.c_float)

    lib.fb_ring_create.argtypes = [C.c_uint32, C.c_uint16, C.c_double]
    lib.fb_ring_create.restype = C.c_void_p

    lib.fb_ring_destroy.argtypes = [C.c_void_p]
    lib.fb_ring_destroy.restype = None

    lib.fb_ring_write.argtypes = [C.c_void_p, f32p, C.c_size_t]
    lib.fb_ring_write.restype = None

    lib.fb_ring_total_written.argtypes = [C.c_void_p]
    lib.fb_ring_total_written.restype = C.c_uint64

    # The READABLE window -- see the module docstring's "TWO SIZES" note.
    lib.fb_ring_capacity.argtypes = [C.c_void_p]
    lib.fb_ring_capacity.restype = C.c_uint64

    # The PHYSICAL frame count backing fb_ring_storage (capacity + guard
    # band). Required for the zero-copy view's shape and for write_pos's
    # modulus -- see the module docstring.
    lib.fb_ring_storage_frames.argtypes = [C.c_void_p]
    lib.fb_ring_storage_frames.restype = C.c_uint64

    lib.fb_ring_storage.argtypes = [C.c_void_p]
    lib.fb_ring_storage.restype = f32p

    lib.fb_ring_set_gain.argtypes = [C.c_void_p, C.c_float]
    lib.fb_ring_set_gain.restype = None

    lib.fb_ring_gain.argtypes = [C.c_void_p]
    lib.fb_ring_gain.restype = C.c_float

    lib.fb_ring_flush.argtypes = [C.c_void_p]
    lib.fb_ring_flush.restype = None

    lib.fb_ring_read.argtypes = [C.c_void_p, C.c_uint64, C.c_size_t, f32p]
    lib.fb_ring_read.restype = C.c_int

    lib.fb_ring_summary_bins.argtypes = [C.c_void_p, C.c_size_t, C.c_uint64, C.c_uint64, f32p]
    lib.fb_ring_summary_bins.restype = C.c_int

    lib.fb_wav_write.argtypes = [C.c_char_p, f32p, C.c_size_t, C.c_uint32, C.c_uint16, C.c_int]
    lib.fb_wav_write.restype = C.c_int


def _as_f32p(a: np.ndarray):
    return a.ctypes.data_as(C.POINTER(C.c_float))


def wav_write(path, audio: np.ndarray, sample_rate: int, subtype: str) -> None:
    """Write `audio` [N, channels] float32 via the Zig encoder. Mono 1-D
    input is reshaped to [N, 1], matching NativeAudioCircularBuffer.write's
    own mono handling -- audio.shape would otherwise fail to unpack below."""
    lib = load()
    if lib is None:
        raise RuntimeError("flashback_core library not available")
    if audio.ndim == 1:
        audio = audio[:, np.newaxis]
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    n_frames, channels = audio.shape
    status = lib.fb_wav_write(
        str(path).encode("utf-8"), _as_f32p(audio), n_frames,
        sample_rate, channels, SUBTYPE_INTS[subtype],
    )
    if status != _OK:
        raise RuntimeError(f"fb_wav_write failed with status {status}")


class NativeAudioCircularBuffer(RingDerivedOps):
    """AudioCircularBuffer's public surface over the Zig core."""

    def __init__(self, duration_seconds: float = 900.0, sample_rate: int = 48_000, channels: int = 2):
        lib = load()
        if lib is None:
            raise RuntimeError("flashback_core library not available")
        self._lib = lib
        self.sample_rate = sample_rate
        self.channels = channels
        self.duration = duration_seconds
        self._h = lib.fb_ring_create(sample_rate, channels, duration_seconds)
        if not self._h:
            raise MemoryError("fb_ring_create failed")
        # capacity == the READABLE window -- every clamp of "how much audio
        # can I get back" (buffered_seconds, is_full, status, get_latest /
        # get_segment availability) uses THIS.
        self.buffer_size = int(lib.fb_ring_capacity(self._h))
        # storage_frames == the PHYSICAL frame count -- shapes the
        # zero-copy view and is what write_pos actually wraps at. Larger
        # than buffer_size by the guard band; see the module docstring.
        storage_frames = int(lib.fb_ring_storage_frames(self._h))
        self._storage_frames = storage_frames
        # Zero-copy view of Zig-owned storage. Read-only by convention;
        # valid until close(). Visualization readers (get_peak_bins)
        # iterate this directly -- no copies at 30 Hz. Shaped with
        # storage_frames, NOT buffer_size -- see the module docstring.
        storage = lib.fb_ring_storage(self._h)
        self.buffer = np.ctypeslib.as_array(storage, shape=(storage_frames, channels))

    # -- primitives -----------------------------------------------------

    @property
    def total_written(self) -> int:
        return int(self._lib.fb_ring_total_written(self._h))

    @property
    def write_pos(self) -> int:
        # Wraps at storage_frames (the PHYSICAL size), matching the Zig
        # writer's own `tw % storage_frames` -- using buffer_size
        # (capacity) here would report a position that does not match
        # where the next write actually lands in `self.buffer`.
        return self.total_written % self._storage_frames

    @property
    def gain(self) -> float:
        return float(self._lib.fb_ring_gain(self._h))

    @gain.setter
    def gain(self, value: float) -> None:
        self._lib.fb_ring_set_gain(self._h, float(value))

    def write(self, frames: np.ndarray) -> None:
        if frames.ndim == 1:
            frames = frames[:, np.newaxis]
        frames = np.ascontiguousarray(frames, dtype=np.float32)
        self._lib.fb_ring_write(self._h, _as_f32p(frames), len(frames))

    def flush(self) -> None:
        self._lib.fb_ring_flush(self._h)

    def _read_abs(self, abs_start: int, n: int) -> np.ndarray:
        out = np.empty((n, self.channels), dtype=np.float32)
        status = self._lib.fb_ring_read(self._h, abs_start, n, _as_f32p(out))
        if status != _OK:
            # Overwritten mid-read is the seqlock's honest answer for a
            # span that no longer exists; callers get empty, same shape
            # as the Python implementation's clamped-to-nothing result.
            return np.zeros((0, self.channels), dtype=np.float32)
        return out

    # -- AudioCircularBuffer surface ------------------------------------

    def get_latest(self, seconds: float) -> np.ndarray:
        # Unlike the Python impl there is no lock between snapshotting
        # total_written and reading -- a fast writer on a tiny ring can lap
        # us in the gap. Re-snapshot and retry; the span rides the writer.
        for _ in range(3):
            tw = self.total_written
            n = min(int(seconds * self.sample_rate), min(tw, self.buffer_size))
            if n <= 0:
                return np.zeros((0, self.channels), dtype=np.float32)
            got = self._read_abs(tw - n, n)
            if len(got):
                return got
        return np.zeros((0, self.channels), dtype=np.float32)

    def get_segment(self, start_ago: float, end_ago: float) -> np.ndarray:
        if start_ago <= end_ago:
            raise ValueError("start_ago must be greater than end_ago")
        # Same re-snapshot-and-retry pattern as get_latest, and for the
        # same reason: no lock between snapshotting total_written and the
        # read completing, so a fast writer can lap this span in the gap.
        # Without a retry here, get_segment silently returns empty under
        # writer contention where the Python implementation (which holds
        # the lock across the whole snapshot-and-retry loop in
        # _copy_abs_range) returns data -- a real parity divergence on
        # the exact path checkout.py's create()/create_from_abs_range use.
        for _ in range(3):
            tw = self.total_written
            n_avail = min(tw, self.buffer_size)
            avail_secs = n_avail / self.sample_rate
            start_ago_clamped = min(start_ago, avail_secs)
            end_ago_clamped = max(end_ago, 0.0)
            n_start = int(start_ago_clamped * self.sample_rate)
            n_end = int(end_ago_clamped * self.sample_rate)
            span = n_start - n_end
            if span <= 0:
                return np.zeros((0, self.channels), dtype=np.float32)
            got = self._read_abs(tw - n_start, span)
            if len(got):
                return got
        return np.zeros((0, self.channels), dtype=np.float32)

    def get_peak_bins(self, seconds: float, n_bins: int) -> np.ndarray:
        return _peak_bins_impl(
            self.buffer, self.buffer_size,
            lambda: self.total_written,
            lambda abs_start: self.total_written - abs_start <= self.buffer_size,
            self.sample_rate, self.channels, seconds, n_bins,
        )

    def get_summary_bins(self, n_bins: int, seconds=None, bin_span_samples=None) -> np.ndarray:
        if n_bins <= 0:
            raise ValueError("n_bins must be positive")
        out = np.zeros((n_bins, self.channels), dtype=np.float32)
        n_samples = 0 if seconds is None else int(seconds * self.sample_rate)
        span = 0 if not bin_span_samples else int(bin_span_samples)
        status = self._lib.fb_ring_summary_bins(self._h, n_bins, n_samples, span, _as_f32p(out))
        if status != _OK:
            raise ValueError(f"fb_ring_summary_bins status {status}")
        return out

    def close(self) -> None:
        if self._h:
            self.buffer = None
            self._lib.fb_ring_destroy(self._h)
            self._h = None

    def __del__(self):  # belt-and-braces; tests call close() explicitly
        try:
            self.close()
        except Exception:
            pass
