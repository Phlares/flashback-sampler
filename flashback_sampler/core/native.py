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
using capacity for either silently corrupts any host that walks the raw
buffer directly (the Python host no longer does -- peaks come from
fb_ring_peak_bins).
"""
from __future__ import annotations

import ctypes as C
import sys
from pathlib import Path

import numpy as np

from flashback_sampler.core.buffer import RingDerivedOps, _peak_bins_impl

_OK, _OVERWRITTEN, _OUT_OF_RANGE, _IO_ERROR, _INVALID_ARG, _OUT_OF_MEMORY = range(6)
# Public: mirrors flashback_core.h's FbSubtype and checkout.py's
# CheckoutSubtype strings. checkout.py's save() routes a WAV write here
# only when the requested subtype is a key of this dict AND the native
# library is loaded; any subtype absent from this dict (or a non-WAV
# format, or no native library) falls back to soundfile.
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
        try:
            lib = C.CDLL(str(path))
        except OSError:
            # Exists but won't load: an architecture mismatch, a missing
            # runtime dependency, or a corrupted/truncated file -- the
            # realistic way a BUNDLED library breaks (a dev-build path
            # that exists but is empty/garbage would hit this too). This
            # is the bundled-but-broken case load()'s own docstring and
            # make_ring_buffer()'s fallback contract promise to handle
            # the same as a missing candidate: skip it and keep looking,
            # never let a crash here take down app startup.
            continue
        _declare(lib)
        _lib = lib
        break
    return _lib


KIND_INTS = {"loopback": 0, "input": 1, "process": 2, "render": 3}
_KIND_NAMES = {v: k for k, v in KIND_INTS.items()}

MAX_MIXER_SOURCES = 8  # Mixer.max_sources


class FbDevice(C.Structure):
    _fields_ = [("kind", C.c_uint8), ("is_default", C.c_uint8), ("mix_rate", C.c_uint32),
                ("mix_channels", C.c_uint16), ("id", C.c_char * 128), ("name", C.c_char * 128)]


class FbCaptureSpec(C.Structure):
    _fields_ = [("kind", C.c_uint8), ("pid", C.c_uint32), ("rate", C.c_uint32),
                ("channels", C.c_uint16), ("device_id", C.c_char_p)]


class FbCaptureStats(C.Structure):
    _fields_ = [("running", C.c_uint8), ("frames_written", C.c_uint64),
                ("xruns", C.c_uint32), ("mix_rate", C.c_uint32)]


class FbProcess(C.Structure):
    _fields_ = [("pid", C.c_uint32), ("ppid", C.c_uint32), ("name", C.c_char * 128)]


class FbPlaybackState(C.Structure):
    _fields_ = [("running", C.c_uint8), ("playing", C.c_uint8), ("cursor", C.c_uint64),
                ("clip_frames", C.c_uint64), ("mix_rate", C.c_uint32)]


class FbPeakBin(C.Structure):
    _fields_ = [("min", C.c_float), ("max", C.c_float)]


def _declare(lib: C.CDLL) -> None:
    """Argument/return types mirroring core/include/flashback_core.h,
    export by export. A mismatch here is silent memory corruption, not
    an error -- ctypes trusts these declarations completely."""
    f32p = C.POINTER(C.c_float)

    lib.fb_ring_create.argtypes = [C.c_uint32, C.c_uint16, C.c_double, C.POINTER(C.c_int)]
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

    lib.fb_ring_peak_bins.argtypes = [C.c_void_p, C.c_uint64, C.c_size_t, C.POINTER(FbPeakBin)]
    lib.fb_ring_peak_bins.restype = C.c_int

    lib.fb_wav_write.argtypes = [C.c_char_p, f32p, C.c_size_t, C.c_uint32, C.c_uint16, C.c_int]
    lib.fb_wav_write.restype = C.c_int

    lib.fb_devices_list.argtypes = [C.POINTER(FbDevice), C.c_size_t]
    lib.fb_devices_list.restype = C.c_size_t
    lib.fb_capture_create.argtypes = [C.c_void_p, C.POINTER(FbCaptureSpec)]
    lib.fb_capture_create.restype = C.c_void_p
    lib.fb_capture_start.argtypes = [C.c_void_p]
    lib.fb_capture_start.restype = C.c_int
    lib.fb_capture_stop.argtypes = [C.c_void_p]
    lib.fb_capture_stop.restype = None
    lib.fb_capture_destroy.argtypes = [C.c_void_p]
    lib.fb_capture_destroy.restype = None
    lib.fb_capture_stats.argtypes = [C.c_void_p, C.POINTER(FbCaptureStats)]
    lib.fb_capture_stats.restype = None
    lib.fb_capture_last_error.argtypes = [C.c_void_p]
    lib.fb_capture_last_error.restype = C.c_char_p

    lib.fb_mixer_create.argtypes = [C.c_void_p, C.POINTER(FbCaptureSpec), C.c_size_t]
    lib.fb_mixer_create.restype = C.c_void_p
    lib.fb_mixer_start.argtypes = [C.c_void_p]
    lib.fb_mixer_start.restype = C.c_int
    lib.fb_mixer_stop.argtypes = [C.c_void_p]
    lib.fb_mixer_stop.restype = None
    lib.fb_mixer_destroy.argtypes = [C.c_void_p]
    lib.fb_mixer_destroy.restype = None
    lib.fb_mixer_stats.argtypes = [C.c_void_p, C.POINTER(FbCaptureStats)]
    lib.fb_mixer_stats.restype = None
    lib.fb_mixer_last_error.argtypes = [C.c_void_p]
    lib.fb_mixer_last_error.restype = C.c_char_p

    lib.fb_processes_list.argtypes = [C.POINTER(FbProcess), C.c_size_t]
    lib.fb_processes_list.restype = C.c_size_t

    lib.fb_playback_create.argtypes = [C.c_char_p, C.c_uint32, C.c_uint16]
    lib.fb_playback_create.restype = C.c_void_p
    lib.fb_playback_bind.argtypes = [C.c_void_p, f32p, C.c_size_t, C.c_uint32, C.c_uint16]
    lib.fb_playback_bind.restype = C.c_int
    lib.fb_playback_play.argtypes = [C.c_void_p]
    lib.fb_playback_play.restype = C.c_int
    lib.fb_playback_pause.argtypes = [C.c_void_p]
    lib.fb_playback_pause.restype = None
    lib.fb_playback_seek.argtypes = [C.c_void_p, C.c_uint64]
    lib.fb_playback_seek.restype = None
    lib.fb_playback_set_device.argtypes = [C.c_void_p, C.c_char_p]
    lib.fb_playback_set_device.restype = None
    lib.fb_playback_state.argtypes = [C.c_void_p, C.POINTER(FbPlaybackState)]
    lib.fb_playback_state.restype = None
    lib.fb_playback_last_error.argtypes = [C.c_void_p]
    lib.fb_playback_last_error.restype = C.c_char_p
    lib.fb_playback_destroy.argtypes = [C.c_void_p]
    lib.fb_playback_destroy.restype = None


def list_devices(max_devices: int = 128) -> list[dict]:
    """Every active WASAPI endpoint: capture endpoints as kind="loopback"
    or kind="input", render endpoints appear twice: as kind="loopback"
    (capture candidate) and kind="render" (playback output). Empty when
    the library is missing or the OS has no backend."""
    lib = load()
    if lib is None:
        return []
    arr = (FbDevice * max_devices)()
    n = int(lib.fb_devices_list(arr, max_devices))
    return [
        {"kind": _KIND_NAMES.get(d.kind, "input"), "is_default": bool(d.is_default),
         "mix_rate": int(d.mix_rate), "mix_channels": int(d.mix_channels),
         "id": d.id.decode("utf-8", "replace"), "name": d.name.decode("utf-8", "replace")}
        for d in arr[:n]
    ]


def _as_f32p(a: np.ndarray):
    return a.ctypes.data_as(C.POINTER(C.c_float))


def _frames2d(a: np.ndarray) -> np.ndarray:
    """The frame layout every fb_* call takes: C-contiguous float32
    [N, channels]. Mono 1-D input is reshaped to [N, 1] so `shape`
    unpacks into two names; the contiguous float32 layout is what makes
    the pointer safe to read n_frames * channels floats from."""
    if a.ndim == 1:
        a = a[:, np.newaxis]
    return np.ascontiguousarray(a, dtype=np.float32)


def wav_write(path, audio: np.ndarray, sample_rate: int, subtype: str) -> None:
    """Write `audio` [N, channels] float32 via the Zig encoder."""
    lib = load()
    if lib is None:
        raise RuntimeError("flashback_core library not available")
    audio = _frames2d(audio)
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
        status = C.c_int(_OK)
        self._h = lib.fb_ring_create(sample_rate, channels, duration_seconds, C.byref(status))
        if not self._h:
            if status.value == _OUT_OF_MEMORY:
                # The readable window's payload; the guard band and the
                # summary ring add a little on top (see Ring.init).
                requested = int(duration_seconds * sample_rate) * channels * 4
                raise MemoryError(
                    f"fb_ring_create: could not reserve {requested:,} bytes "
                    f"for a {duration_seconds:g} s ring at {sample_rate} Hz x {channels} ch"
                )
            raise ValueError(
                f"fb_ring_create rejected rate={sample_rate} channels={channels} seconds={duration_seconds}"
            )
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
        frames = _frames2d(frames)
        # fb_ring_write trusts len(frames) and reads n_frames * self.channels
        # floats from whatever buffer we hand it -- a caller that passes a
        # narrower array (e.g. mono into a stereo ring) would otherwise make
        # it read past the array's end into uninitialized heap (confirmed:
        # the tail frames come back as garbage floats, not zeros or an
        # error). AudioCircularBuffer.write instead silently broadcasts a
        # narrower array across channels; raising here is a deliberate
        # parity divergence -- broadcasting would mask a real caller bug by
        # writing plausible-looking wrong audio into the recording.
        if frames.shape[1] != self.channels:
            raise ValueError(
                f"write() frames has {frames.shape[1]} channel(s), "
                f"ring has {self.channels}"
            )
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
        # copy_abs_range) returns data -- a real parity divergence on
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

    def copy_abs_range(self, abs_start: int, abs_end: int) -> np.ndarray:
        """Public counterpart to AudioCircularBuffer.copy_abs_range — the
        shared surface checkout.py (create_from_abs_range, the drag-select
        checkout path) reads an absolute span through instead of
        implementation-private internals.

        Retries up to 3 times on the SAME fixed (abs_start, abs_end) --
        unlike get_latest/get_segment, which re-resolve abs_start from a
        relative window on each attempt, this method's range is pinned by
        the caller and never moves. The retry still matters: a torn read
        (the writer mid-copy during our read) can make a single
        fb_ring_read report OVERWRITTEN for a span that is not actually
        gone -- the exact same request would succeed on the next attempt.
        Matches AudioCircularBuffer.copy_abs_range's 3-attempt retry so
        both implementations answer a transient tear the same way."""
        n = abs_end - abs_start
        if n <= 0:
            return np.zeros((0, self.channels), dtype=np.float32)
        for _ in range(3):
            got = self._read_abs(abs_start, n)
            if len(got):
                return got
        return np.zeros((0, self.channels), dtype=np.float32)

    def get_peak_bins(self, seconds: float, n_bins: int) -> np.ndarray:
        """(n_bins, 2, channels) float32: [i, 0, c] = min, [i, 1, c] = max.
        The engine fills PeakBin{min, max} per (bin, channel); the transpose
        below is the only Python work. Zeros for an empty or torn window,
        and for a closed handle."""
        out = np.zeros((n_bins, self.channels, 2), dtype=np.float32)
        if self._h is None:
            return out  # closed handle: NULL into a *Ring is a process abort, not an exception
        status = self._lib.fb_ring_peak_bins(
            self._h, max(0, int(seconds * self.sample_rate)), n_bins,
            out.ctypes.data_as(C.POINTER(FbPeakBin)),
        )
        if status == _INVALID_ARG:
            raise ValueError("n_bins must be positive")
        return np.ascontiguousarray(out.transpose(0, 2, 1))

    def get_summary_bins(self, n_bins: int, seconds=None, bin_span_samples=None) -> np.ndarray:
        """See AudioCircularBuffer.get_summary_bins's docstring for the
        shared contract. One divergence: n_bins > 4096 raises ValueError
        here (Summary.rmsBins's max_bins stack-scratch bound), where
        AudioCircularBuffer accepts any positive n_bins -- unreachable
        today since the UI never requests more than 360 bins."""
        if n_bins <= 0:
            raise ValueError("n_bins must be positive")
        out = np.zeros((n_bins, self.channels), dtype=np.float32)
        n_samples = 0 if seconds is None else int(seconds * self.sample_rate)
        # fb_ring_summary_bins/Summary.rmsBins overload n_samples=0 as ITS
        # OWN sentinel for "all available" (mirroring get_summary_bins's
        # seconds=None default) -- so an explicit seconds=0 (a real,
        # deliberate zero-length request) must never reach the wire as 0,
        # or it collides with that sentinel and silently returns the FULL
        # window instead of an empty one. `out` is already all-zero.
        if seconds is not None and n_samples <= 0:
            return out
        span = 0 if not bin_span_samples else int(bin_span_samples)
        status = self._lib.fb_ring_summary_bins(self._h, n_bins, n_samples, span, _as_f32p(out))
        if status != _OK:
            raise ValueError(f"fb_ring_summary_bins status {status}")
        return out

    def close(self) -> None:
        """Destroys the Zig-owned ring and frees its storage. Unlike
        AudioCircularBuffer.close() (a no-op), this one does real work,
        with two consequences a caller must respect:

        - Never retain a reference to `self.buffer` across a call to
          close() -- it is a zero-copy numpy VIEW over storage that
          fb_ring_destroy frees; a view held past this point points at
          freed memory.
        - `self.buffer` itself becomes None (see below), so any method
          that reads it -- get_peak_bins, for instance -- raises
          TypeError if called after close(). AudioCircularBuffer has no
          such post-close failure mode (its close() does nothing), so
          this is implementation-specific behavior a caller holding a
          RingDerivedOps generically should not rely on either way:
          don't use a buffer after closing it.
        """
        if self._h:
            self.buffer = None
            self._lib.fb_ring_destroy(self._h)
            self._h = None

    def __del__(self):  # belt-and-braces; tests call close() explicitly
        try:
            self.close()
        except Exception:
            pass


def _process_entries(max_processes: int = 4096) -> list[tuple[int, int, str]]:
    """One Toolhelp32 snapshot via the core: (pid, ppid, exe_name) rows.
    Both public views below derive from this."""
    lib = load()
    if lib is None:
        return []
    arr = (FbProcess * max_processes)()
    n = int(lib.fb_processes_list(arr, max_processes))
    return [(int(p.pid), int(p.ppid), p.name.decode("utf-8", "replace"))
            for p in arr[:n] if p.pid > 0 and p.name]


def list_processes(max_processes: int = 4096) -> list[tuple[int, str]]:
    rows = [(pid, name) for pid, _ppid, name in _process_entries(max_processes)]
    rows.sort(key=lambda t: (t[1].lower(), t[0]))
    return rows


def resolve_root_pid(pid: int) -> int:
    """Walk up from `pid` to the highest ancestor sharing the same exe
    name (port of win32_process_loopback.resolve_audio_root_pid — apps
    like Spotify/Chrome play audio from the ROOT of a same-exe tree).
    Returns `pid` unchanged if it is absent or the chain breaks."""
    procs = {p: (pp, name) for p, pp, name in _process_entries()}
    if pid not in procs:
        return pid
    name_lc = procs[pid][1].lower()
    current, visited = pid, set()
    while current not in visited:
        visited.add(current)
        parent, _ = procs.get(current, (0, ""))
        if parent <= 0 or parent not in procs or procs[parent][1].lower() != name_lc:
            break
        current = parent
    return current
