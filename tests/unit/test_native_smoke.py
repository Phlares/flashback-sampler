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


@pytest.mark.parametrize("frames", [
    pytest.param(np.arange(4, dtype=np.float32), id="1d_mono_into_stereo"),
    pytest.param(np.arange(4, dtype=np.float32).reshape(4, 1), id="2d_single_column_into_stereo"),
    pytest.param(np.arange(12, dtype=np.float32).reshape(4, 3), id="3_columns_into_stereo"),
])
def test_write_rejects_channel_count_mismatch(frames):
    """write() must reject frames whose channel count doesn't match the
    ring's. Previously it passed len(frames) straight to fb_ring_write,
    which reads n_frames * self.channels floats out of whatever buffer
    the caller handed it regardless of that buffer's actual width --
    confirmed by reproducing against the built DLL: writing a 4-element
    1-D (mono) array into a channels=2 ring returns 2 real frames
    followed by 2 frames of uninitialized heap (e.g. 8.19e+34), not a
    clean error and not the value AudioCircularBuffer would produce.

    AudioCircularBuffer instead silently BROADCASTS a narrower array
    across channels (e.g. the same 1-D input becomes [[0,0],[1,1],[2,2],
    [3,3]]) -- a deliberate, documented parity divergence: broadcasting
    masks a real caller bug by writing plausible-looking wrong audio,
    which is exactly the "conflating shapes corrupts silently" failure
    mode this phase exists to close off. Raising is the safer contract
    even though no current app caller reaches this path (every capture
    source already conforms its channel count before writing)."""
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8, channels=2)
    with pytest.raises(ValueError):
        buf.write(frames)
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
    success). Without a retry, checkout.py's create_from_abs_range
    (drag-select) sees a torn read as a hard failure -- an empty array /
    RuntimeError -- on a request that would have succeeded a moment
    later, a real behavior gap against AudioCircularBuffer.copy_abs_range's
    3-attempt retry."""
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
    """Pins `Ring.peakBins`'s split between `capacity` (the readable
    window) and the ring's PHYSICAL modulus (`storage_frames` = capacity
    + a 4096-frame guard band) -- native-only, because for the Python
    implementation the two are always equal (len(buffer) == buffer_size),
    so this scenario cannot be constructed there.

    total_written here (10,000) exceeds capacity (8000) but stays well
    under storage_frames (8000 + 4096 = 12096), so NO physical wrap has
    happened yet -- if Ring.zig's `modulus = self.storage_frames` were
    mutated to `modulus = self.capacity` instead, this reads WRONG ring
    positions that still hold real (but stale/out-of-window) samples
    from an earlier physical offset, not zeros: measured under that
    exact mutation, bins 3-4's maxes come back as 7994/1996 instead of
    the correct 8001/9996 -- plausible-looking, silently wrong data,
    which is why this needs an explicit pin rather than relying on it to
    look broken if it breaks. See the Task 1 fix report for the mutation
    record."""
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8000, channels=1)
    ramp = np.arange(10_000, dtype=np.float32)[:, None]
    buf.write(ramp)
    bins = buf.get_peak_bins(seconds=1.0, n_bins=4)
    maxes = bins[:, 1, 0]
    assert np.all(np.diff(maxes) > 0), f"bins not monotonically increasing: {maxes}"
    assert maxes[-1] == pytest.approx(9996.0, abs=5)
    buf.close()


def test_load_skips_a_candidate_that_exists_but_is_not_a_valid_library(tmp_path, monkeypatch):
    """A bundled-but-broken library (architecture mismatch, missing
    runtime dependency, a corrupted/truncated file) is the realistic
    distribution failure this fallback exists for -- load()'s own
    docstring promises None "if not built anywhere", and
    make_ring_buffer()/PLATFORM.md both promise a graceful fallback to
    the Python implementation, not a crash. Previously C.CDLL(...) was
    unguarded: a candidate that EXISTS but is not a loadable library
    raises OSError straight out of load() -> make_ring_buffer() ->
    AppState.__init__, crashing app startup instead of skipping to the
    next candidate (or falling back to Python if none work) exactly like
    a MISSING candidate already does."""
    bad = tmp_path / "not_a_real_library.dll"
    bad.write_text("this is not a shared library")
    monkeypatch.setattr(native, "_candidates", lambda: [bad])
    monkeypatch.setattr(native, "_lib", None)
    monkeypatch.setattr(native, "_lib_tried", False)
    assert native.load() is None


def test_wav_float32_round_trips_bit_exact(tmp_path):
    from tests.fixtures.wavread import read_wav
    rng = np.random.default_rng(7)
    audio = rng.uniform(-1, 1, size=(4801, 2)).astype(np.float32)
    native.wav_write(tmp_path / "zig.wav", audio, 48_000, "FLOAT")
    got, info = read_wav(tmp_path / "zig.wav")
    assert (info.samplerate, info.channels, info.frames) == (48_000, 2, 4801)
    np.testing.assert_array_equal(got, audio)  # FLOAT32 is a memcpy of the f32 bits (wav.zig:84-90)


# wav.zig quantizes with scale 32767 / 8388607 (not 32768 / 8388608) so
# +1.0 needs no clamp; -1.0 lands one LSB short of the negative rail
# (wav.zig:91-96). @round is half-away-from-zero, hence the sign/floor form.
@pytest.mark.parametrize("subtype,scale,denom", [("PCM_16", 32767.0, 32768.0), ("PCM_24", 8388607.0, 8388608.0)])
def test_wav_pcm_codes_match_the_documented_quantizer(tmp_path, subtype, scale, denom):
    from tests.fixtures.wavread import read_wav
    rng = np.random.default_rng(11)
    audio = rng.uniform(-1, 1, size=(997, 2)).astype(np.float32)
    native.wav_write(tmp_path / "zig.wav", audio, 48_000, subtype)
    got, _ = read_wav(tmp_path / "zig.wav")
    v = (audio * np.float32(scale)).astype(np.float64)  # f32 multiply as in wav.zig, then exact rounding in f64
    codes = np.sign(v) * np.floor(np.abs(v) + 0.5)     # half away from zero == Zig @round
    np.testing.assert_array_equal(got, codes.astype(np.float32) / np.float32(denom))
