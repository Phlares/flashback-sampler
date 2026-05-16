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
from tests.fixtures.sine_source import ramp_block


# ─────────────────────────────────────────────────────────────────────────
# Basic construction
# ─────────────────────────────────────────────────────────────────────────


def test_new_buffer_is_empty():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=2)
    assert buf.buffered_seconds == 0.0
    assert buf.is_full is False
    assert buf.total_written == 0
    assert buf.buffer_size == 1000
    assert buf.buffer.shape == (1000, 2)
    assert buf.buffer.dtype == np.float32


def test_empty_buffer_get_latest_returns_zero_length():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=2)
    result = buf.get_latest(0.5)
    assert result.shape == (0, 2)


def test_status_shape():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=2)
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


# ─────────────────────────────────────────────────────────────────────────
# Write path
# ─────────────────────────────────────────────────────────────────────────


def test_write_advances_position_and_total():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 400, channels=1))
    assert buf.write_pos == 400
    assert buf.total_written == 400
    assert buf.buffered_seconds == pytest.approx(0.4)


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


def test_write_mono_1d_gets_reshaped():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    mono = np.arange(100, dtype=np.float32)
    buf.write(mono)
    assert buf.write_pos == 100
    assert buf.buffer[50, 0] == 50.0


# ─────────────────────────────────────────────────────────────────────────
# get_latest
# ─────────────────────────────────────────────────────────────────────────


def test_get_latest_below_buffered_returns_exact_tail():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    # Last 100 samples should be 400..500
    latest = buf.get_latest(0.1)
    assert latest.shape == (100, 1)
    assert latest[0, 0] == pytest.approx(400.0)
    assert latest[-1, 0] == pytest.approx(499.0)


def test_get_latest_more_than_buffered_is_clamped():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 300, channels=1))
    latest = buf.get_latest(1.0)  # asked for 1000, only have 300
    assert latest.shape == (300, 1)
    assert latest[0, 0] == pytest.approx(0.0)
    assert latest[-1, 0] == pytest.approx(299.0)


def test_get_latest_across_wrap_boundary():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 1200, channels=1))  # fills + wraps
    # Buffer now holds samples 200..1200 (the oldest 200 were overwritten)
    latest = buf.get_latest(1.0)
    assert latest.shape == (1000, 1)
    assert latest[0, 0] == pytest.approx(200.0)
    assert latest[-1, 0] == pytest.approx(1199.0)


# ─────────────────────────────────────────────────────────────────────────
# get_segment
# ─────────────────────────────────────────────────────────────────────────


def test_get_segment_non_wrapped():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 900, channels=1))
    # Segment from 500ms ago to 100ms ago = samples 400..800
    seg = buf.get_segment(start_ago=0.5, end_ago=0.1)
    assert seg.shape[0] == 400
    assert seg[0, 0] == pytest.approx(400.0)
    assert seg[-1, 0] == pytest.approx(799.0)


def test_get_segment_raises_on_inverted_boundaries():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    with pytest.raises(ValueError):
        buf.get_segment(start_ago=0.1, end_ago=0.5)  # start must be > end


def test_get_segment_across_wrap():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 1500, channels=1))  # holds 500..1500
    # 800ms ago -> sample 700; 200ms ago -> sample 1300. Span: 600 samples.
    seg = buf.get_segment(start_ago=0.8, end_ago=0.2)
    assert seg.shape[0] == 600
    assert seg[0, 0] == pytest.approx(700.0)
    assert seg[-1, 0] == pytest.approx(1299.0)


def test_get_segment_clamped_to_available():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 300, channels=1))
    # Ask for 500ms ago when only 300ms exists — should clamp
    seg = buf.get_segment(start_ago=0.5, end_ago=0.0)
    # clamped start = 0.3s ago = sample 0; end at sample 300
    assert seg.shape[0] == 300
    assert seg[0, 0] == pytest.approx(0.0)
    assert seg[-1, 0] == pytest.approx(299.0)


# ─────────────────────────────────────────────────────────────────────────
# RMS / levels
# ─────────────────────────────────────────────────────────────────────────


def test_get_rms_levels_silence_is_zero():
    buf = AudioCircularBuffer(duration_seconds=0.1, sample_rate=1000, channels=2)
    buf.write(np.zeros((50, 2), dtype=np.float32))
    rms = buf.get_rms_levels(window_seconds=0.05)
    assert rms.shape == (2,)
    assert np.allclose(rms, 0.0)


def test_get_rms_levels_sine_is_sqrt_half_amplitude():
    buf = AudioCircularBuffer(duration_seconds=0.1, sample_rate=48_000, channels=1)
    # Full-amplitude sine — RMS should be ~ 1/sqrt(2)
    t = np.arange(4800) / 48_000
    sine = np.sin(2 * np.pi * 440.0 * t).astype(np.float32)[:, None]
    buf.write(sine)
    rms = buf.get_rms_levels(window_seconds=0.1)
    assert rms.shape == (1,)
    assert rms[0] == pytest.approx(1.0 / np.sqrt(2.0), rel=0.02)


# ─────────────────────────────────────────────────────────────────────────
# Concurrency smoke test
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.timeout(5)
def test_writer_and_reader_concurrent_no_corruption():
    """
    Pound the buffer from a writer thread while the main thread takes
    repeated get_segment snapshots. Neither should crash, deadlock, or
    return malformed arrays.
    """
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=48_000, channels=2)
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
        while time.monotonic() < deadline:
            seg = buf.get_segment(start_ago=0.3, end_ago=0.05)
            # Shape sanity — should never come back malformed
            assert seg.ndim == 2
            assert seg.shape[1] == 2
            reads += 1
        assert reads > 10  # we actually did some work
    finally:
        stop.set()
        t.join(timeout=1.0)

    assert errors == []


# ─────────────────────────────────────────────────────────────────────────
# get_peak_bins — downsampled waveform data for the UI
# ─────────────────────────────────────────────────────────────────────────


def test_get_peak_bins_empty_buffer_returns_zeros():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=2)
    bins = buf.get_peak_bins(seconds=0.5, n_bins=40)
    assert bins.shape == (40, 2, 2)  # (n_bins, min/max, channels)
    assert np.all(bins == 0.0)


def test_get_peak_bins_shape_is_n_bins_by_2_by_channels():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=2)
    buf.write(ramp_block(0, 500, channels=2))
    bins = buf.get_peak_bins(seconds=0.5, n_bins=10)
    assert bins.shape == (10, 2, 2)


def test_get_peak_bins_sine_is_symmetric():
    buf = AudioCircularBuffer(duration_seconds=0.2, sample_rate=48_000, channels=1)
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


def test_get_peak_bins_single_bin_is_global_minmax():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    # Write values 0..999 so the window's min=0, max=999
    buf.write(ramp_block(0, 1000, channels=1))
    bins = buf.get_peak_bins(seconds=1.0, n_bins=1)
    assert bins.shape == (1, 2, 1)
    assert bins[0, 0, 0] == pytest.approx(0.0)
    assert bins[0, 1, 0] == pytest.approx(999.0)


def test_get_peak_bins_channels_independent():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=2)
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


def test_flush_resets_counters_and_content():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 800, channels=1))
    assert buf.buffered_seconds > 0
    buf.flush()
    assert buf.total_written == 0
    assert buf.write_pos == 0
    assert buf.buffered_seconds == 0.0
    assert np.all(buf.buffer == 0.0)


def test_flush_on_empty_buffer_is_harmless():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.flush()
    assert buf.total_written == 0


def test_writer_works_immediately_after_flush():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(ramp_block(0, 500, channels=1))
    buf.flush()
    # Next write should start from position 0 and be visible via get_latest
    buf.write(ramp_block(0, 100, channels=1) + 42.0)
    assert buf.total_written == 100
    latest = buf.get_latest(0.1)
    assert latest.shape == (100, 1)
    assert latest[0, 0] == pytest.approx(42.0)
    assert latest[-1, 0] == pytest.approx(141.0)


def test_flush_after_wrap_fully_clears_ring():
    buf = AudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
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
def test_get_segment_does_not_stall_writer():
    """
    The writer thread must not see inter-write latency spikes even when
    the reader is pulling multi-second segments. This proves get_segment
    is not holding the lock during its memcpy.

    Buffer: 30s @ 48kHz stereo (~11 MB). Reader pulls 20s slices (~7.7 MB
    memcpy each) — large enough that a copy-under-lock regression stalls
    the writer for several ms per read. 60 reads total; writer must keep
    its max inter-write gap under 8 ms.
    """
    buf = AudioCircularBuffer(duration_seconds=30.0, sample_rate=48_000, channels=2)
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
def test_get_peak_bins_does_not_flicker_on_saturated_ring():
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
    buf = AudioCircularBuffer(duration_seconds=2.0, sample_rate=48_000, channels=2)
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


def test_get_peak_bins_stable_under_rolling_window():
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
    buf = AudioCircularBuffer(
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
