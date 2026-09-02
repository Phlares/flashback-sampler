"""Native library smoke: bindings load and round-trip. The session gate
in tests/conftest.py is the single mechanism that requires the built
library; no per-file check is needed."""
import numpy as np
import pytest

from flashback_sampler.core import native


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
    clean error.

    raising is deliberate: broadcasting would mask a caller bug by
    writing plausible-looking wrong audio, which is exactly the
    "conflating shapes corrupts silently" failure mode this phase
    exists to close off, even though no current app caller reaches
    this path (every capture source already conforms its channel
    count before writing)."""
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
    retry loop (matching get_latest's own and copy_abs_range's).
    A live writer/reader race is inherently probabilistic -- pounding the
    buffer from a background thread does NOT reliably prove the retry
    loop matters, since most single-attempt reads still succeed by luck
    even with zero retries (confirmed while fixing this: the
    concurrency stress test in test_buffer.py,
    test_writer_and_reader_concurrent_no_corruption, passed 3/3 runs
    with the retry loop removed entirely). Monkeypatching fb_ring_read
    to force exactly two synthetic failures before succeeding makes the
    retry behavior deterministic instead of luck-dependent."""
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
    later."""
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
    + a 4096-frame guard band).

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
    """A bundled-but-broken library must not crash load(); it reports None
    and the constructor raises a clear RuntimeError."""
    bad = tmp_path / "not_a_real_library.dll"
    bad.write_text("this is not a shared library")
    monkeypatch.setattr(native, "_candidates", lambda: [bad])
    monkeypatch.setattr(native, "_lib", None)
    monkeypatch.setattr(native, "_lib_tried", False)
    assert native.load() is None
    with pytest.raises(RuntimeError):
        native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8, channels=1)


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


def test_get_peak_bins_and_get_rms_levels_after_close_keep_their_shape():
    """Pins the closed-handle guards' shape against the live path's. A
    mono ring's get_peak_bins guard returned `out` (n_bins, channels, 2)
    RAW, before the transpose the live path applies -- the documented
    (n_bins, 2, channels) shape only held for a live handle. Confirmed
    broken before the fix: shape came back (4, 1, 2), not (4, 2, 1);
    waveform_view's bins[:, 1, ch] would IndexError on that shape for a
    mono ring closed mid-read."""
    buf = native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8, channels=1)
    buf.close()

    bins = buf.get_peak_bins(0.1, 4)
    assert bins.shape == (4, 2, 1)
    assert bins.dtype == np.float32
    assert np.all(bins == 0.0)

    rms = buf.get_rms_levels()
    assert rms.shape == (1,)
    assert np.all(rms == 0.0)

    summary = buf.get_summary_bins(4)
    assert summary.shape == (4, 1)
    assert np.all(summary == 0.0)


def test_load_skips_a_candidate_that_loads_but_lacks_a_symbol(tmp_path, monkeypatch):
    """#48: a stale bundled library that loads but misses an export must be
    skipped like a broken one, not raise AttributeError through startup."""
    class Stale:
        def __getattr__(self, name):
            raise AttributeError(name)

    good = object()
    stale_path = tmp_path / "stale.dll"
    good_path = tmp_path / "good.dll"
    stale_path.write_text("x")
    good_path.write_text("x")
    monkeypatch.setattr(native, "_candidates", lambda: [stale_path, good_path])
    monkeypatch.setattr(native.C, "CDLL", lambda p: Stale() if p == str(stale_path) else good)
    declared = []
    monkeypatch.setattr(native, "_declare", lambda lib: declared.append(lib) if lib is good else Stale().fb_ring_create)
    monkeypatch.setattr(native, "_lib", None)
    monkeypatch.setattr(native, "_lib_tried", False)

    assert native.load() is good
    assert declared == [good]


def test_not_available_error_names_the_skipped_candidate_and_why(tmp_path, monkeypatch):
    """A skipped candidate must not vanish into "not built anywhere": the
    RuntimeError names the path and the missing export."""
    class Stale:
        def __getattr__(self, name):
            raise AttributeError(f"function '{name}' not found")

    stale_path = tmp_path / "stale.dll"
    stale_path.write_text("x")
    monkeypatch.setattr(native, "_candidates", lambda: [stale_path])
    monkeypatch.setattr(native.C, "CDLL", lambda p: Stale())
    monkeypatch.setattr(native, "_lib", None)
    monkeypatch.setattr(native, "_lib_tried", False)
    monkeypatch.setattr(native, "_skipped", [])

    assert native.load() is None
    with pytest.raises(RuntimeError) as info:
        native.NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=8, channels=1)
    assert str(stale_path) in str(info.value)
    assert "fb_" in str(info.value)


def test_mem_info_reports_physical_ram_and_free_within_it():
    """#41: the footprint check reads total and available physical bytes
    from the engine; on Windows both are known and non-zero."""
    total, available = native.mem_info()
    assert total > 1024 ** 3  # more than 1 GB on any dev box
    assert 0 < available <= total
