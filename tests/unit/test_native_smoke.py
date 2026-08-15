"""Native library smoke: bindings load and round-trip. Skips (not fails)
when the Zig library isn't built, so Zig-less dev environments stay green."""
import numpy as np
import pytest

native = pytest.importorskip("flashback_sampler.core.native")

pytestmark = pytest.mark.skipif(native.load() is None, reason="flashback_core library not built (cd core && zig build -Doptimize=ReleaseSafe)")


def test_roundtrip_write_read():
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8, channels=2)
    frames = np.array([[0.1, -0.1], [0.2, -0.2]], dtype=np.float32)
    buf.write(frames)
    got = buf.get_latest(10.0)
    np.testing.assert_array_equal(got, frames)
    buf.close()


def test_zero_copy_storage_view_sees_writes():
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8, channels=1)
    buf.write(np.array([0.5], dtype=np.float32))
    assert buf.buffer[0, 0] == np.float32(0.5)
    buf.close()


def test_get_segment_retries_on_transient_read_failure(monkeypatch):
    """native-impl internal: deterministically pins get_segment's 3-attempt
    retry loop (matching get_latest's own, and Python's copy_abs_range).
    A live writer/reader race is inherently probabilistic -- pounding the
    buffer from a background thread does NOT reliably prove the retry
    loop matters, since most single-attempt reads still succeed by luck
    even with zero retries (confirmed while fixing this: the concurrency
    stress test in test_buffer.py passed 3/3 runs with the retry loop
    removed entirely). Monkeypatching fb_ring_read to force exactly two
    synthetic failures before succeeding makes the retry behavior
    deterministic instead of luck-dependent."""
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(np.arange(500, dtype=np.float32)[:, None])

    real_read = buf._lib.fb_ring_read
    calls = {"n": 0}

    def flaky_read(ring, abs_start, n_frames, out_ptr):
        calls["n"] += 1
        if calls["n"] <= 2:
            return native._OVERWRITTEN  # simulate a transient seqlock tear
        return real_read(ring, abs_start, n_frames, out_ptr)

    monkeypatch.setattr(buf._lib, "fb_ring_read", flaky_read)
    seg = buf.get_segment(start_ago=0.3, end_ago=0.05)
    assert calls["n"] == 3, "get_segment did not retry 3 times"
    assert seg.shape[0] > 0, "get_segment gave up instead of succeeding on the 3rd attempt"
    buf.close()


def test_copy_abs_range_retries_on_transient_read_failure(monkeypatch):
    """native-impl internal: copy_abs_range must retry a transient seqlock
    tear the same way get_latest/get_segment do (see the retry test above
    for why a live writer/reader race can't deterministically prove this --
    monkeypatching fb_ring_read forces two synthetic failures before
    success). Without a retry, checkout.py's create_from_abs_range (drag-
    select) and MixedCaptureSource's mixer thread (mixed_capture.py,
    polling a live sub-source ring every 10ms) both see a torn read as a
    hard failure -- an empty array / RuntimeError -- on a request that
    would have succeeded a moment later, a real behavior gap against
    AudioCircularBuffer.copy_abs_range's 3-attempt retry."""
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=1000, channels=1)
    buf.write(np.arange(500, dtype=np.float32)[:, None])

    real_read = buf._lib.fb_ring_read
    calls = {"n": 0}

    def flaky_read(ring, abs_start, n_frames, out_ptr):
        calls["n"] += 1
        if calls["n"] <= 2:
            return native._OVERWRITTEN  # simulate a transient seqlock tear
        return real_read(ring, abs_start, n_frames, out_ptr)

    monkeypatch.setattr(buf._lib, "fb_ring_read", flaky_read)
    seg = buf.copy_abs_range(100, 200)
    assert calls["n"] == 3, "copy_abs_range did not retry 3 times"
    assert seg.shape[0] == 100, "copy_abs_range gave up instead of succeeding on the 3rd attempt"
    buf.close()


def test_get_peak_bins_correct_past_capacity_before_physical_wrap():
    """Pins _peak_bins_impl's split between `capacity` (the readable
    window) and the ring's PHYSICAL modulus (native's storage_frames =
    capacity + a 4096-frame guard band) -- native-only, because for the
    Python implementation the two are always equal (len(buffer) ==
    buffer_size), so this scenario cannot be constructed there.

    total_written here (10,000) exceeds capacity (8000) but stays well
    under storage_frames (8000 + 4096 = 12096), so NO physical wrap has
    happened yet -- if buffer.py's `modulus = len(ring)` were mutated to
    `modulus = capacity` instead, this reads WRONG ring positions that
    still hold real (but stale/out-of-window) samples from an earlier
    physical offset, not zeros: measured under that exact mutation,
    bins 3-4's maxes come back as 7994/1996 instead of the correct
    8001/9996 -- plausible-looking, silently wrong data, which is why
    this needs an explicit pin rather than relying on it to look broken
    if it breaks. See the Task 7 fix report for the mutation record."""
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8000, channels=1)
    ramp = np.arange(10_000, dtype=np.float32)[:, None]
    buf.write(ramp)
    bins = buf.get_peak_bins(seconds=1.0, n_bins=4)
    maxes = bins[:, 1, 0]
    assert np.all(np.diff(maxes) > 0), f"bins not monotonically increasing: {maxes}"
    assert maxes[-1] == pytest.approx(9996.0, abs=5)
    buf.close()


def test_wav_float32_decode_equals_soundfile(tmp_path):
    import soundfile as sf
    rng = np.random.default_rng(7)
    audio = rng.uniform(-1, 1, size=(4801, 2)).astype(np.float32)
    zig_path, sf_path = tmp_path / "zig.wav", tmp_path / "sf.wav"
    native.wav_write(zig_path, audio, 48_000, "FLOAT")
    sf.write(str(sf_path), audio, 48_000, format="WAV", subtype="FLOAT")
    got_z, sr_z = sf.read(str(zig_path), dtype="float32")
    got_s, sr_s = sf.read(str(sf_path), dtype="float32")
    assert sr_z == sr_s == 48_000
    np.testing.assert_array_equal(got_z, got_s)  # bit-identical samples


# wav.zig deliberately quantizes with scale 32767 (not 32768) so +1.0
# stays in range without clamping (see wav.zig's own doc comment); this
# is the plan's contract, Task 5 golden-tested it, and it keeps +/-
# full-scale symmetric. libsndfile's own PCM_16 writer uses scale 32768.
# For x in [-1, 1] the two raw-integer outputs are round(x*32767) and
# round(x*32768); their difference is bounded by
#   |round(x*32768) - round(x*32767)| <= |x*32768 - x*32767| + 0.5 + 0.5
#                                       = |x| + 1 <= 2
# (the two independent +-0.5 terms come from each encoder's own
# round-to-nearest; |x| <= 1 is what caps the scale-gap term at 1). So 2
# raw LSBs is a PROVEN ceiling for this domain, not a fitted number --
# confirmed against 40 random seeds plus an adversarial 200,001-point
# sweep of the whole [-1, 1] domain, max integer gap exactly 2 in every
# case, never more, so a 1-LSB tolerance was genuinely impossible. This
# bound holds only because `rng.uniform(-1, 1)` below never exceeds
# full scale: above +-1.0 wav.zig's encoder clamps (see its own doc
# comment) while libsndfile's non-clipping PCM path can wrap instead, so
# the [-1, 1] draw is a REQUIRED precondition for this bound, not an
# incidental choice of test data. PCM_24 gets the same derivation at its
# own scale (8388607 vs 8388608): |x| + 1 <= 2 raw units too, but its
# much larger raw range makes that same absolute 2-unit ceiling round
# down to 1 LSB at 24-bit's finer float32 tolerance in the range checked
# below -- both bounds are the same formula, expressed in each encoder's
# own quantum (2/32768 here; 1/8388607 for PCM_24, i.e. effectively
# 2/8388608 rounded to float32 precision).
@pytest.mark.parametrize("subtype,tol", [("PCM_24", 1 / 8388607), ("PCM_16", 2 / 32768)])
def test_wav_pcm_decode_within_documented_quantizer_gap_of_soundfile(tmp_path, subtype, tol):
    import soundfile as sf
    rng = np.random.default_rng(11)
    audio = rng.uniform(-1, 1, size=(997, 2)).astype(np.float32)
    zig_path, sf_path = tmp_path / "zig.wav", tmp_path / "sf.wav"
    native.wav_write(zig_path, audio, 48_000, subtype)
    sf.write(str(sf_path), audio, 48_000, format="WAV", subtype=subtype)
    got_z, _ = sf.read(str(zig_path), dtype="float32")
    got_s, _ = sf.read(str(sf_path), dtype="float32")
    assert np.abs(got_z - got_s).max() <= tol  # quantizers differ by <= 2 raw LSB (PCM_16) / 1 raw LSB (PCM_24) -- see the proven bound above
