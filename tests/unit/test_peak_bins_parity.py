"""TEMPORARY parity pin: numpy `_peak_bins_impl` (buffer.py) versus
`fb_ring_peak_bins` on the same ring contents. Deleted with buffer.py in
Task 5 of the PR f plan; the Zig tests in Ring.zig pin the arithmetic
permanently."""
from __future__ import annotations

import numpy as np
import pytest

from flashback_sampler.core import native
from flashback_sampler.core.buffer import _peak_bins_impl

pytestmark = pytest.mark.skipif(native.load() is None, reason="flashback_core not built")


def _numpy_peaks(buf, seconds, n_bins):
    return _peak_bins_impl(
        buf.buffer, buf.buffer_size,
        lambda: buf.total_written,
        lambda abs_start: buf.total_written - abs_start <= buf.buffer_size,
        buf.sample_rate, buf.channels, seconds, n_bins,
    )


@pytest.mark.parametrize("rate,channels,duration,frames,seconds,n_bins", [
    (1000, 1, 1.0, 1000, 1.0, 4),          # case A: stride 1, exact edges
    (1000, 1, 1.0, 10, 0.01, 4),           # case B: uneven edges
    (1000, 1, 1.0, 3, 0.003, 5),           # case C: empty bins copy predecessor
    (1000, 2, 1.0, 6000, 1.0, 2),          # case D: physical wrap past storage_frames
    (10_000, 1, 1.0, 10_000, 1.0, 2),      # case E: headroom + stride 11
    (10_000, 2, 1.0, 10_001, 1.0, 100),    # rolling by one frame, stereo
    (48_000, 2, 2.0, 96_000 + 512 * 7, 2.0, 360),  # the UI's call (turntable_window.py:1623)
])
def test_zig_peak_bins_equal_numpy(rate, channels, duration, frames, seconds, n_bins):
    buf = native.NativeAudioCircularBuffer(duration_seconds=duration, sample_rate=rate, channels=channels)
    try:
        rng = np.random.default_rng(frames)
        buf.write((rng.standard_normal((frames, channels)) * 100).astype(np.float32))
        zig = buf.get_peak_bins(seconds, n_bins)
        ref = _numpy_peaks(buf, seconds, n_bins)
        assert zig.shape == ref.shape == (n_bins, 2, channels)
        np.testing.assert_array_equal(zig, ref)
    finally:
        buf.close()


def test_zig_rms_matches_numpy_reference():
    from flashback_sampler.core.buffer import RingDerivedOps
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=48_000, channels=2)
    try:
        rng = np.random.default_rng(3)
        buf.write(rng.standard_normal((30_000, 2)).astype(np.float32))
        ref = RingDerivedOps.get_rms_levels(buf, 0.2)   # the numpy path, called unbound
        np.testing.assert_allclose(buf.get_rms_levels(0.2), ref, rtol=1e-5)
    finally:
        buf.close()
