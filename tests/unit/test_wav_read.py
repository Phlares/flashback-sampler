"""fb_wav_info / fb_wav_read / fb_wav_peak_bins against two oracles:
tests/fixtures/wavread.py (an independent stdlib reader) for files the
engine wrote, and a struct-built WAVE_FORMAT_EXTENSIBLE fixture for
DAW-written headers."""
from __future__ import annotations

import struct

import numpy as np
import pytest

from flashback_sampler.core import native
from tests.fixtures.wavread import read_wav


def _ramp(frames: int, channels: int) -> np.ndarray:
    a = np.arange(frames * channels, dtype=np.float32).reshape(frames, channels)
    return a / np.float32(frames * channels)


def _extensible_pcm16(path, rate: int, channels: int, codes: np.ndarray) -> None:
    """A WAVE_FORMAT_EXTENSIBLE header (tag 0xFFFE, 40-byte fmt) around
    little-endian int16 codes — the shape a DAW export carries."""
    data = codes.astype("<i2").tobytes()
    block = channels * 2
    fmt = struct.pack("<HHIIHH", 0xFFFE, channels, rate, rate * block, block, 16)
    fmt += struct.pack("<HHI", 22, 16, 3)  # cbSize, valid bits, channel mask
    fmt += struct.pack("<H", 1) + b"\x00\x00" + bytes.fromhex("000010008000 00aa00389b71".replace(" ", ""))
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


@pytest.mark.parametrize("subtype", ["FLOAT", "PCM_24", "PCM_16"])
def test_wav_read_agrees_with_the_stdlib_oracle(tmp_path, subtype):
    p = tmp_path / "engine.wav"
    audio = _ramp(1000, 2)
    native.wav_write(p, audio, 44_100, subtype)
    info = native.wav_info(p)
    assert (info.rate, info.channels, info.frames) == (44_100, 2, 1000)
    got = native.wav_read(p, 0, 1000)
    oracle, oinfo = read_wav(p)
    assert oinfo.subtype == subtype
    np.testing.assert_array_equal(got, oracle)


def test_wav_read_sub_span(tmp_path):
    p = tmp_path / "span.wav"
    audio = _ramp(50, 1)
    native.wav_write(p, audio, 8_000, "FLOAT")
    got = native.wav_read(p, 10, 5)
    np.testing.assert_array_equal(got, audio[10:15])


def test_wav_read_extensible_pcm16_header(tmp_path):
    p = tmp_path / "daw.wav"
    codes = np.array([[0, 32767], [-32768, 1], [100, -100]], dtype=np.int16)
    _extensible_pcm16(p, 96_000, 2, codes)
    info = native.wav_info(p)
    assert (info.rate, info.channels, info.frames, info.subtype) == (96_000, 2, 3, native.SUBTYPE_INTS["PCM_16"])
    got = native.wav_read(p, 0, 3)
    oracle, _ = read_wav(p)
    np.testing.assert_array_equal(got, oracle)


def test_wav_read_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        native.wav_info(tmp_path / "missing.wav")
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wave file at all")
    with pytest.raises(ValueError):
        native.wav_info(junk)
    p = tmp_path / "short.wav"
    native.wav_write(p, _ramp(4, 1), 8_000, "FLOAT")
    with pytest.raises(ValueError):
        native.wav_read(p, 3, 2)


def test_wav_peak_bins_match_ring_peak_bins_on_the_same_audio(tmp_path):
    # Ring.peakBins and peaks.peakBinsFile share one reducer; prove it
    # through the ABI on a stride-1 window (n <= 256 * n_bins).
    from flashback_sampler.core.native import NativeAudioCircularBuffer
    audio = (np.random.default_rng(7).standard_normal((3000, 2)) * 0.5).astype(np.float32)
    p = tmp_path / "peaks.wav"
    native.wav_write(p, audio, 48_000, "FLOAT")
    buf = NativeAudioCircularBuffer(duration_seconds=1.0, sample_rate=48_000, channels=2)
    buf.write(audio)
    from_ring = buf.get_peak_bins(3000 / 48_000, 30)
    from_file = native.wav_peak_bins(p, 0, 3000, 30)
    buf.close()
    np.testing.assert_array_equal(from_file, from_ring)


def test_wav_peak_bins_shape_and_sub_range(tmp_path):
    audio = _ramp(100, 1)
    p = tmp_path / "sub.wav"
    native.wav_write(p, audio, 8_000, "FLOAT")
    bins = native.wav_peak_bins(p, 20, 40, 4)
    assert bins.shape == (4, 2, 1)
    # bin 0 = frames 20..30
    assert bins[0, 0, 0] == pytest.approx(audio[20, 0])
    assert bins[0, 1, 0] == pytest.approx(audio[29, 0])
