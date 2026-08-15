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


# PCM_24's tolerance is 1 raw LSB (wav.zig's documented, deliberate scale
# choice: 8388607, not 8388608, so +1.0 stays in range without clamping).
# PCM_16 needs 2 raw LSBs, not 1: measured empirically (20 seeds x 2000
# stereo frames -- see Task 7 report) libsndfile's own PCM_16 writer uses
# scale 32768 (not 32767), so the two encoders' raw-integer outputs can
# differ by up to |scale_gap| (1 unit, from the 32768-vs-32767 asymmetry)
# PLUS up to 1 more unit from each encoder's own independent rounding --
# a deterministic ~2 LSB ceiling, not test flakiness. It reproduced on
# every one of 20 random seeds tried, never exceeding 2. PCM_24's much
# larger raw range (8388607 vs 32767) makes the same absolute 1-unit
# scale gap negligible in the same sample count, so its 1-LSB tolerance
# holds empirically (also confirmed against 20 seeds).
@pytest.mark.parametrize("subtype,tol", [("PCM_24", 1 / 8388607), ("PCM_16", 2 / 32767)])
def test_wav_pcm_decode_within_documented_quantizer_gap_of_soundfile(tmp_path, subtype, tol):
    import soundfile as sf
    rng = np.random.default_rng(11)
    audio = rng.uniform(-1, 1, size=(997, 2)).astype(np.float32)
    zig_path, sf_path = tmp_path / "zig.wav", tmp_path / "sf.wav"
    native.wav_write(zig_path, audio, 48_000, subtype)
    sf.write(str(sf_path), audio, 48_000, format="WAV", subtype=subtype)
    got_z, _ = sf.read(str(zig_path), dtype="float32")
    got_s, _ = sf.read(str(sf_path), dtype="float32")
    assert np.abs(got_z - got_s).max() <= tol  # quantizers may differ by up to 2 LSB (PCM_16) / 1 LSB (PCM_24) -- see comment above
