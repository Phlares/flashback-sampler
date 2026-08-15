"""
Ratify existing AudioCircularBuffer behavior via tests.

These tests exercise the buffer as it is today (pre-seqlock refactor). They
must keep passing after the M2 seqlock refactor with no semantic changes.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from flashback_sampler.core.buffer import AudioCircularBuffer
from flashback_sampler.core import native as native_mod
from tests.fixtures.sine_source import ramp_block


@pytest.fixture(params=["python", "native"])
def buffer_cls(request):
    """Every test in this file runs twice: once per implementation.
    This suite IS the parity contract for the Zig core."""
    if request.param == "native":
        if native_mod.load() is None:
            pytest.skip("flashback_core library not built")
        return native_mod.NativeAudioCircularBuffer
    return AudioCircularBuffer


# ─────────────────────────────────────────────────────────────────────────
# Basic construction
# ─────────────────────────────────────────────────────────────────────────


def test_new_buffer_is_empty(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=2)
    assert buf.buffered_seconds == 0.0
    assert buf.is_full is False
    assert buf.total_written == 0
    assert buf.buffer_size == 1000
    assert buf.buffer.shape[0] >= 1000
    assert buf.buffer.shape[1] == 2
    assert buf.buffer.dtype == np.float32


def test_empty_buffer_get_latest_returns_zero_length(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=2)
    result = buf.get_latest(0.5)
    assert result.shape == (0, 2)


def test_status_shape(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=2)
    s = buf.status()
    for key in (
        "buffered_seconds",
        "buffer_capacity_seconds",
        "fill_percent",
        "write_pos",
        "total_written_samples",
        "sample_rate",
        "channels",
        "memory_mb",
    ):
        assert key in s


def test_capacity_bytes_is_readable_capacity_not_physical_storage(buffer_cls):
    """capacity_bytes must be derived from buffer_size (the READABLE
    window), not from len(buf.buffer) -- NativeAudioCircularBuffer's raw
    storage array is buffer_size PLUS a guard band (see native.py's module
    docstring), so touching .buffer.nbytes directly over-reports RAM.
    This is exactly the mistake AppState.total_project_ram_bytes made
    before routing buffer construction through the native implementation
    surfaced it."""
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=2)
    assert buf.capacity_bytes == buf.buffer_size * buf.channels * 4


# ─────────────────────────────────────────────────────────────────────────
# Write path
# ─────────────────────────────────────────────────────────────────────────


def test_write_advances_position_and_total(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 400, channels=1))
    assert buf.write_pos == 400
    assert buf.total_written == 400
    assert buf.buffered_seconds == pytest.approx(0.4)


def test_write_pos_wraps_at_the_physical_buffer_size_after_a_real_wrap(buffer_cls):
    """Implementation-agnostic invariant: write_pos == total_written %
    buffer.shape[0] -- buffer.shape[0] is each implementation's own
    PHYSICAL modulus (buffer_size for Python, storage_frames for native;
    see native.py's TWO SIZES note), so this holds for both without
    either implementation needing to agree on what that physical size
    actually is. Writes past 4196 frames (buffer_size=100 + native's
    4096-frame guard band) so native's physical wrap point is actually
    exercised, not just Python's smaller one. Pins native.py's write_pos
    against being computed with buffer_size (the READABLE capacity)
    instead of storage_frames (the PHYSICAL size it actually wraps at)
    -- that mutation reddens this test (write_pos comes back 0 instead
    of the correct 804 at these exact numbers; see the Task 7 fix
    report's mutation record)."""
    buf = buffer_cls(duration_seconds=1.0, sample_rate=100, channels=1)
    chunk = np.zeros((100, 1), dtype=np.float32)  # == buffer_size: one write() call per lap, no multi-wrap
    for _ in range(50):  # 5000 frames total, past native's storage_frames (100 + 4096 = 4196)
        buf.write(chunk)
    assert buf.write_pos == buf.total_written % buf.buffer.shape[0]


# python-impl internal: probes write_pos and raw physical buffer indexing
# past the wrap point. Both are genuinely different between implementations
# by design (native.py's TWO SIZES note) — native wraps its PHYSICAL
# storage at storage_frames = buffer_size + a guard band (thousands of
# frames), not at buffer_size, so write_pos and buffer[i] here would not
# agree with the Python values asserted below even though both
# implementations are behaving correctly. Not parity-testable via raw
# indexing; get_latest/get_segment (which native re-derives from abs
# indices, hiding the physical layout) are the parity-tested equivalent.
def test_write_wraps_around_end_of_ring():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 800, channels=1))  # fills 0..800
    buf.write(ramp_block(800, 400, channels=1))  # wraps: 800..1000 then 0..200
    assert buf.write_pos == 200
    assert buf.total_written == 1200
    assert buf.is_full is True
    # Sample at absolute position 1000 should have overwritten the sample at
    # ring index 0 — check we see `1000.0` there, not `0.0`.
    assert buf.buffer[0, 0] == pytest.approx(1000.0)
    assert buf.buffer[199, 0] == pytest.approx(1199.0)
    # The un-wrapped tail (200..800) should still hold its original content.
    assert buf.buffer[200, 0] == pytest.approx(200.0)
    assert buf.buffer[799, 0] == pytest.approx(799.0)


def test_write_mono_1d_gets_reshaped(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    mono = np.arange(100, dtype=np.float32)
    buf.write(mono)
    assert buf.write_pos == 100
    assert buf.buffer[50, 0] == 50.0


# ─────────────────────────────────────────────────────────────────────────
# get_latest
# ─────────────────────────────────────────────────────────────────────────


def test_get_latest_below_buffered_returns_exact_tail(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    # Last 100 samples should be 400..500
    latest = buf.get_latest(0.1)
    assert latest.shape == (100, 1)
    assert latest[0, 0] == pytest.approx(400.0)
    assert latest[-1, 0] == pytest.approx(499.0)


def test_get_latest_more_than_buffered_is_clamped(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 300, channels=1))
    latest = buf.get_latest(1.0)  # asked for 1000, only have 300
    assert latest.shape == (300, 1)
    assert latest[0, 0] == pytest.approx(0.0)
    assert latest[-1, 0] == pytest.approx(299.0)


def test_get_latest_across_wrap_boundary(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 1200, channels=1))  # fills + wraps
    # Buffer now holds samples 200..1200 (the oldest 200 were overwritten)
    latest = buf.get_latest(1.0)
    assert latest.shape == (1000, 1)
    assert latest[0, 0] == pytest.approx(200.0)
    assert latest[-1, 0] == pytest.approx(1199.0)


# ─────────────────────────────────────────────────────────────────────────
# get_segment
# ─────────────────────────────────────────────────────────────────────────


def test_get_segment_non_wrapped(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 900, channels=1))
    # Segment from 500ms ago to 100ms ago = samples 400..800
    seg = buf.get_segment(start_ago=0.5, end_ago=0.1)
    assert seg.shape[0] == 400
    assert seg[0, 0] == pytest.approx(400.0)
    assert seg[-1, 0] == pytest.approx(799.0)


def test_get_segment_raises_on_inverted_boundaries(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    with pytest.raises(ValueError):
        buf.get_segment(start_ago=0.1, end_ago=0.5)  # start must be > end


def test_get_segment_across_wrap(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 1500, channels=1))  # holds 500..1500
    # 800ms ago -> sample 700; 200ms ago -> sample 1300. Span: 600 samples.
    seg = buf.get_segment(start_ago=0.8, end_ago=0.2)
    assert seg.shape[0] == 600
    assert seg[0, 0] == pytest.approx(700.0)
    assert seg[-1, 0] == pytest.approx(1299.0)


def test_get_segment_clamped_to_available(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 300, channels=1))
    # Ask for 500ms ago when only 300ms exists — should clamp
    seg = buf.get_segment(start_ago=0.5, end_ago=0.0)
    # clamped start = 0.3s ago = sample 0; end at sample 300
    assert seg.shape[0] == 300
    assert seg[0, 0] == pytest.approx(0.0)
    assert seg[-1, 0] == pytest.approx(299.0)


# ─────────────────────────────────────────────────────────────────────────
# copy_abs_range — the shared-surface entry point checkout.py and
# mixed_capture.py use to read an absolute [abs_start, abs_end) span
# without reaching into implementation-private internals (a Python lock
# for AudioCircularBuffer; a raw ctypes read for NativeAudioCircularBuffer).
# ─────────────────────────────────────────────────────────────────────────


def test_copy_abs_range_returns_exact_absolute_span(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 900, channels=1))
    # Absolute samples 400..800 (same span as test_get_segment_non_wrapped)
    seg = buf.copy_abs_range(400, 800)
    assert seg.shape[0] == 400
    assert seg[0, 0] == pytest.approx(400.0)
    assert seg[-1, 0] == pytest.approx(799.0)


def test_copy_abs_range_across_wrap(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 1500, channels=1))  # holds abs 500..1500
    seg = buf.copy_abs_range(700, 1300)
    assert seg.shape[0] == 600
    assert seg[0, 0] == pytest.approx(700.0)
    assert seg[-1, 0] == pytest.approx(1299.0)


def test_copy_abs_range_overwritten_span_returns_empty(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 1500, channels=1))  # holds abs 500..1500
    seg = buf.copy_abs_range(0, 100)  # long since overwritten
    assert seg.shape == (0, 1)


# ─────────────────────────────────────────────────────────────────────────
# RMS / levels
# ─────────────────────────────────────────────────────────────────────────


def test_get_rms_levels_silence_is_zero(buffer_cls):
    buf = buffer_cls(duration_seconds=0.1, sample_rate=1000, channels=2)
    buf.write(np.zeros((50, 2), dtype=np.float32))
    rms = buf.get_rms_levels(window_seconds=0.05)
    assert rms.shape == (2,)
    assert np.allclose(rms, 0.0)


def test_get_rms_levels_sine_is_sqrt_half_amplitude(buffer_cls):
    buf = buffer_cls(duration_seconds=0.1, sample_rate=48_000, channels=1)
    # Full-amplitude sine — RMS should be ~ 1/sqrt(2)
    t = np.arange(4800) / 48_000
    sine = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)[:, None]
    buf.write(sine)
    rms = buf.get_rms_levels(window_seconds=0.1)
    assert rms.shape == (1,)
    assert rms[0] == pytest.approx(1.0 / np.sqrt(2.0), rel=0.02)


# ─────────────────────────────────────────────────────────────────────────
# get_summary_bins — pre-decimated RMS ring (had NO test coverage, either
# implementation, before this parity test: Summary is the phase's largest
# numeric port, and the two implementations could have silently agreed on
# a shared mistake at their SUMMARY_SLOT_SAMPLES==4096 boundary (Python
# excludes unfrozen partial slots via its slot-generation tag; native's
# rmsBins clamps against `capacity` instead) without either side's own
# suite ever catching it. Asserted ANALYTICALLY (constant amplitude ->
# known RMS), not cross-checked against the other implementation, which
# is what actually makes a shared mistake visible instead of invisible.
# ─────────────────────────────────────────────────────────────────────────


def test_get_summary_bins_constant_amplitude_is_exact_rms(buffer_cls):
    # Frame count is an exact multiple of the summary slot size (4096, both
    # implementations' default) so every slot involved is fully FROZEN --
    # no partial-slot edge case to reason about, keeping the analytic
    # assertion simple: RMS of a constant-amplitude signal is exactly that
    # amplitude, in every bin, with no approximation needed.
    buf = buffer_cls(duration_seconds=2.0, sample_rate=4096, channels=1)
    buf.write(np.full((8192, 1), 0.5, dtype=np.float32))  # == 2 slots exactly
    bins = buf.get_summary_bins(n_bins=2)  # bin_span defaults to 8192/2 == 4096, one slot per bin
    assert bins.shape == (2, 1)
    np.testing.assert_allclose(bins, 0.5, atol=1e-6)


def test_get_summary_bins_seconds_zero_is_zero_bins_not_all_available(buffer_cls):
    """An explicit seconds=0 must return all-zero bins (a zero-length
    window), NOT the full-buffer answer. The ABI/Zig side overloads
    n_samples_req=0 to mean "all available" (fb_ring_summary_bins /
    Summary.rmsBins), and native.py's get_summary_bins computed
    n_samples = int(seconds * sample_rate) without distinguishing
    seconds=0 (a real, deliberate zero-length request) from seconds=None
    (the "give me everything" default) -- both collapsed to the same
    n_samples=0 wire value, so native() silently returned the FULL
    window's RMS for an explicit zero-second request. Confirmed
    divergence before the fix: native returned non-zero bins here while
    AudioCircularBuffer correctly returned all zeros."""
    buf = buffer_cls(duration_seconds=1.0, sample_rate=4096, channels=1)
    buf.write(np.full((4096, 1), 0.5, dtype=np.float32))
    bins = buf.get_summary_bins(n_bins=2, seconds=0)
    assert bins.shape == (2, 1)
    np.testing.assert_array_equal(bins, 0.0)


# ─────────────────────────────────────────────────────────────────────────
# Concurrency smoke test
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.timeout(5)
def test_writer_and_reader_concurrent_no_corruption(buffer_cls):
    """
    Pound the buffer from a writer thread while the main thread takes
    repeated get_segment snapshots. Neither should crash, deadlock, or
    return malformed arrays.

    Also asserts at least one read came back non-empty (`reads_with_data
    > 0`) -- shape-only assertions (`seg.ndim`/`seg.shape[1]`) hold
    trivially for an empty `(0, channels)` array too, so a native
    get_segment that silently returned empty on EVERY call (e.g. no
    retry against writer contention -- a real bug this test previously
    missed, see the Task 7 fix report) would still have passed the
    shape checks alone.
    """
    buf = buffer_cls(duration_seconds=1.0, sample_rate=48_000, channels=2)
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        try:
            pos = 0
            while not stop.is_set():
                buf.write(ramp_block(pos, 512, channels=2))
                pos += 512
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 0.5
        reads = 0
        reads_with_data = 0
        while time.monotonic() < deadline:
            seg = buf.get_segment(start_ago=0.3, end_ago=0.05)
            # Shape sanity — should never come back malformed
            assert seg.ndim == 2
            assert seg.shape[1] == 2
            if seg.shape[0] > 0:
                reads_with_data += 1
            reads += 1
        assert reads > 10  # we actually did some work
        assert reads_with_data > 0, "every read came back empty -- get_segment never returned data under contention"
    finally:
        stop.set()
        t.join(timeout=1.0)

    assert errors == []


# ─────────────────────────────────────────────────────────────────────────
# get_peak_bins — downsampled waveform data for the UI
# ─────────────────────────────────────────────────────────────────────────


def test_get_peak_bins_empty_buffer_returns_zeros(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=2)
    bins = buf.get_peak_bins(seconds=0.5, n_bins=40)
    assert bins.shape == (40, 2, 2)  # (n_bins, min/max, channels)
    assert np.all(bins == 0.0)


def test_get_peak_bins_shape_is_n_bins_by_2_by_channels(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=2)
    buf.write(ramp_block(0, 500, channels=2))
    bins = buf.get_peak_bins(seconds=0.5, n_bins=10)
    assert bins.shape == (10, 2, 2)


def test_get_peak_bins_sine_is_symmetric(buffer_cls):
    buf = buffer_cls(duration_seconds=0.2, sample_rate=48_000, channels=1)
    t = np.arange(9600) / 48_000
    sine = (np.sin(2 * np.pi * 440.0 * t) * 0.8).astype(np.float32)[:, None]
    buf.write(sine)
    bins = buf.get_peak_bins(seconds=0.2, n_bins=50)
    # min ≈ -max for each bin; sine spans full amplitude after a few cycles
    mins = bins[:, 0, 0]
    maxs = bins[:, 1, 0]
    # All bins contain at least a few cycles of a 440 Hz sine at 48 kHz
    # (each bin = ~192 samples = ~1.8 cycles), so every bin hits full range.
    assert np.all(maxs > 0.7)
    assert np.all(mins < -0.7)
    assert np.allclose(mins, -maxs, atol=0.05)


def test_get_peak_bins_single_bin_is_global_minmax(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    # Write values 0..999 so the window's min=0, max=999
    buf.write(ramp_block(0, 1000, channels=1))
    bins = buf.get_peak_bins(seconds=1.0, n_bins=1)
    assert bins.shape == (1, 2, 1)
    assert bins[0, 0, 0] == pytest.approx(0.0)
    assert bins[0, 1, 0] == pytest.approx(999.0)


def test_get_peak_bins_channels_independent(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=2)
    # Channel 0 is silent, channel 1 is a ramp
    block = np.zeros((500, 2), dtype=np.float32)
    block[:, 1] = np.arange(500, dtype=np.float32)
    buf.write(block)
    bins = buf.get_peak_bins(seconds=0.5, n_bins=5)
    # ch 0: all zeros everywhere
    assert np.all(bins[:, :, 0] == 0.0)
    # ch 1: non-zero maxes that increase monotonically across bins
    assert np.all(np.diff(bins[:, 1, 1]) > 0)


# ─────────────────────────────────────────────────────────────────────────
# Flush
# ─────────────────────────────────────────────────────────────────────────


def test_flush_resets_counters_and_content(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 800, channels=1))
    assert buf.buffered_seconds > 0
    buf.flush()
    assert buf.total_written == 0
    assert buf.write_pos == 0
    assert buf.buffered_seconds == 0.0
    assert np.all(buf.buffer == 0.0)


def test_flush_on_empty_buffer_is_harmless(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.flush()
    assert buf.total_written == 0


def test_close_is_safe_to_call_and_idempotent(buffer_cls):
    """Both implementations must expose close() so app code (e.g.
    AppState.rebuild_buffer, discarding the buffer it just replaced) can
    release a NativeAudioCircularBuffer's Zig-owned handle deterministically
    instead of waiting on __del__/GC. AudioCircularBuffer's close() is a
    no-op (pure Python, GC already handles it) but must still exist and be
    safe to call, including twice, so callers don't need an isinstance
    check to know which implementation they're holding."""
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    buf.close()
    buf.close()  # idempotent — must not raise


def test_writer_works_immediately_after_flush(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    buf.flush()
    # Next write should start from position 0 and be visible via get_latest
    buf.write(ramp_block(0, 100, channels=1) + 42.0)
    assert buf.total_written == 100
    latest = buf.get_latest(0.1)
    assert latest.shape == (100, 1)
    assert latest[0, 0] == pytest.approx(42.0)
    assert latest[-1, 0] == pytest.approx(141.0)


def test_flush_after_wrap_fully_clears_ring(buffer_cls):
    buf = buffer_cls(duration_seconds=1.0, sample_rate=1000, channels=1)
    # Write past the ring boundary so wrap-around overwrites sample 0
    buf.write(ramp_block(0, 1500, channels=1))
    buf.flush()
    assert np.all(buf.buffer == 0.0)
    assert buf.write_pos == 0
    assert buf.total_written == 0


# ─────────────────────────────────────────────────────────────────────────
# Non-blocking reads (seqlock verification)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.timeout(15)
@pytest.mark.perf
def test_get_segment_does_not_stall_writer(buffer_cls):
    """
    The writer thread must not see inter-write latency spikes even when
    the reader is pulling multi-second segments.

    [python]: proves get_segment is not holding self._lock during its
    memcpy (a copy-under-lock regression would stall the writer, which
    also acquires the lock, for the copy's full duration).

    [native]: there is no lock on this path at all -- get_segment goes
    straight through ctypes into Zig's lock-free seqlock read, so this
    param instead bounds overall writer-thread tail latency under
    concurrent reader contention (ctypes call overhead, GIL handoff
    between the two threads, and the Zig read's own copy time). A real
    regression there (e.g. get_segment somehow blocking the writer) would
    still show up as an inter-write stall, just not a *lock*-shaped one.

    Buffer: 30s @ 48kHz stereo (~11 MB). Reader pulls 20s slices (~7.7 MB
    memcpy each) — large enough that a copy-under-lock regression stalls
    the writer for several ms per read. 60 reads total; writer must keep
    its max inter-write gap under 8 ms.
    """
    buf = buffer_cls(duration_seconds=30.0, sample_rate=48_000, channels=2)
    stop = threading.Event()
    results = {}

    def writer():
        max_write_time = 0.0
        count = 0
        block = np.zeros((512, 2), dtype=np.float32)
        while not stop.is_set():
            t0 = time.monotonic()
            buf.write(block)
            t1 = time.monotonic()
            dt = t1 - t0
            if dt > max_write_time:
                max_write_time = dt
            count += 1
        results["max_write_time"] = max_write_time
        results["count"] = count

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    # Prime the ring — writer needs time to fill past 20s of audio
    time.sleep(0.3)

    # Hammer the reader with 7.7 MB memcpies
    for _ in range(60):
        seg = buf.get_segment(start_ago=20.0, end_ago=0.0)
        assert seg.ndim == 2
        assert seg.shape[1] == 2

    stop.set()
    t.join(timeout=1.0)

    assert results["count"] > 500, "writer didn't run often enough"
    # Uncontended write() on a 512-sample block takes ~10-50µs. The
    # pre-seqlock regression had the reader holding the lock across a
    # ~2.29 ms memcpy AND scaling linearly with buffer size, so a
    # regression on a 7.7 MB read would be ~20 ms+ even without
    # scheduler noise. Typical post-seqlock numbers are < 1 ms; we
    # set the ceiling at 4 ms to absorb Windows scheduler jitter
    # while still catching any real stall-under-lock regression
    # (which would be multiples of 4 ms).
    assert results["max_write_time"] < 0.004, (
        f"writer was stalled: max write() duration = "
        f"{results['max_write_time']*1000:.2f}ms"
    )


@pytest.mark.timeout(15)
def test_get_peak_bins_does_not_flicker_on_saturated_ring(buffer_cls):
    """
    Regression: once the ring fills, repeated get_peak_bins() calls must
    not intermittently return all-zero frames. The symptom was a visible
    UI flicker between flat and waveform once the buffer hit max.

    Cause: the reader snapshotted abs_start = total_written - n with
    n == buffer_size, so any writer advance between snapshot and verify
    satisfied (total_written_new - abs_start > buffer_size) and fired the
    tear path. Fix leaves slack below buffer_size so the writer can
    advance normally without invalidating the oldest sample.
    """
    buf = buffer_cls(duration_seconds=2.0, sample_rate=48_000, channels=2)
    sr = buf.sample_rate
    block = np.full((512, 2), 0.5, dtype=np.float32)

    # Saturate the ring (overfill so total_written > buffer_size).
    for _ in range(int(2.0 * sr / 512) + 50):
        buf.write(block)
    assert buf.is_full

    stop = threading.Event()

    def writer():
        while not stop.is_set():
            buf.write(block)
            time.sleep(0.01)  # ≈WASAPI period

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    time.sleep(0.03)  # let writer get into rhythm

    zero_frames = 0
    total_frames = 50
    for _ in range(total_frames):
        bins = buf.get_peak_bins(seconds=2.0, n_bins=360)
        if not np.any(bins):
            zero_frames += 1
        time.sleep(0.033)  # ≈30 Hz UI refresh

    stop.set()
    t.join(timeout=1.0)

    # Allow a couple of zero frames for scheduler jitter, but nothing
    # close to flicker-level counts.
    assert zero_frames < 5, (
        f"saw {zero_frames}/{total_frames} zero frames — flicker regression"
    )


def test_get_peak_bins_stable_under_rolling_window(buffer_cls):
    """
    Regression: rolling the window by tiny writer advances must not
    shift the peaks of still-visible bins. The stride-sampling pattern
    must be anchored to the absolute audio sample index, not to the
    window's rolling start — otherwise each tick picks a different
    subset of the bin's samples and the bar heights jump (visible as a
    "rolling flicker" on the waveform display).
    """
    sr = 10_000
    duration = 10.0  # 100 000-sample ring → span=900 per bin → stride=3
    buf = buffer_cls(
        duration_seconds=duration, sample_rate=sr, channels=1
    )
    rng = np.random.default_rng(seed=0)
    content = (rng.standard_normal(int(duration * sr)) * 100).astype(np.float32)
    buf.write(content[:, None])
    assert buf.is_full

    # Collect 10 frames, advancing by 1 sample between each. Middle bins
    # cover essentially identical audio across all 10 reads (the window
    # rolls by 10 samples total, far less than one bin's 900 samples).
    frames = []
    for _ in range(10):
        frames.append(
            buf.get_peak_bins(seconds=duration, n_bins=100)[:, 1, 0].copy()
        )
        buf.write(np.zeros((1, 1), dtype=np.float32))

    mid = slice(30, 70)  # bins well inside the window
    maxes = np.stack([f[mid] for f in frames])  # (10 frames, 40 bins)
    per_bin_drift = maxes.max(axis=0) - maxes.min(axis=0)
    per_bin_mean = np.abs(maxes).mean(axis=0)
    ratio = (per_bin_drift / (per_bin_mean + 1e-6)).mean()

    assert ratio < 0.03, (
        f"bin heights drifted by avg {ratio*100:.1f}% across small writer "
        f"advances — stride sampling is not abs-aligned (flicker regression)"
    )


# ── Per-source record gain (applied at the write boundary) ──────────────

def test_buffer_gain_defaults_to_unity(buffer_cls):
    import numpy as np
    buf = buffer_cls(duration_seconds=1, sample_rate=1000, channels=1)
    assert buf.gain == 1.0
    assert buf.gain_db == 0.0
    buf.write(np.full((100, 1), 0.25, dtype=np.float32))
    out = buf.get_latest(0.05)
    assert abs(float(out.max()) - 0.25) < 1e-6  # unchanged at unity


def test_buffer_gain_boost_scales_written_frames(buffer_cls):
    import math
    import numpy as np
    buf = buffer_cls(duration_seconds=1, sample_rate=1000, channels=1)
    buf.gain_db = 6.0  # ~2x
    assert abs(buf.gain - 10 ** (6.0 / 20.0)) < 1e-6
    buf.write(np.full((100, 1), 0.25, dtype=np.float32))
    out = buf.get_latest(0.05)
    assert abs(float(out.max()) - 0.25 * (10 ** (6.0 / 20.0))) < 1e-4
    assert out.dtype == np.float32  # gain must not upcast


def test_buffer_gain_db_roundtrips_and_mutes(buffer_cls):
    import math
    buf = buffer_cls(duration_seconds=1, sample_rate=1000, channels=1)
    buf.gain_db = -6.0
    assert abs(buf.gain_db - (-6.0)) < 1e-6
    buf.gain_db = float("-inf")  # mute
    assert buf.gain == 0.0
    assert buf.gain_db == float("-inf")


# ─────────────────────────────────────────────────────────────────────────
# make_ring_buffer factory
# ─────────────────────────────────────────────────────────────────────────


def test_make_ring_buffer_prefers_native_when_available():
    """The single constructor every app call site routes through. On a
    machine with the native library built (this one), it must return
    NativeAudioCircularBuffer -- this is the assertion that fails if the
    factory is ever hardcoded to always return the Python fallback."""
    from flashback_sampler.core.buffer import make_ring_buffer
    buf = make_ring_buffer(duration_seconds=1.0, sample_rate=8, channels=2)
    if native_mod.load() is not None:
        assert type(buf).__name__ == "NativeAudioCircularBuffer"
    else:
        assert isinstance(buf, AudioCircularBuffer)
    assert buf.sample_rate == 8 and buf.channels == 2


def test_make_ring_buffer_falls_back_to_python_when_native_unavailable(monkeypatch):
    """Covers the other half of the branch: force native.load() to report
    unavailable and confirm the factory falls back to AudioCircularBuffer
    rather than raising or still returning a native instance."""
    from flashback_sampler.core.buffer import make_ring_buffer
    monkeypatch.setattr(native_mod, "load", lambda: None)
    buf = make_ring_buffer(duration_seconds=1.0, sample_rate=8, channels=2)
    assert isinstance(buf, AudioCircularBuffer)
    assert type(buf).__name__ != "NativeAudioCircularBuffer"
