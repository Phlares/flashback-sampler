"""tests/fixtures/wavread.py is the WAV oracle for fb_wav_write. It must
decode what wav.zig writes today (plain 44-byte header, tags 1/3) and a
WAVE_FORMAT_EXTENSIBLE header with a padded odd-sized chunk in front."""
from __future__ import annotations

import struct

import numpy as np

from flashback_sampler.core import native
from tests.fixtures.wavread import read_wav


def test_float32_round_trips_bit_exact(tmp_path):
    audio = np.array([[0.25, -0.25], [0.5, -0.5]], dtype=np.float32)
    native.wav_write(tmp_path / "f.wav", audio, 48_000, "FLOAT")
    got, info = read_wav(tmp_path / "f.wav")
    np.testing.assert_array_equal(got, audio)
    assert (info.samplerate, info.channels, info.subtype, info.frames) == (48_000, 2, "FLOAT", 2)


def test_pcm16_decodes_the_documented_quantizer(tmp_path):
    # wav.zig: code = round(x * 32767); decode = code / 32768.
    audio = np.array([1.0, 0.5, -1.0, 0.0], dtype=np.float32)[:, None]
    native.wav_write(tmp_path / "p.wav", audio, 44_100, "PCM_16")
    got, info = read_wav(tmp_path / "p.wav")
    expected = np.array([32767, 16384, -32767, 0], dtype=np.float32) / np.float32(32768.0)
    np.testing.assert_array_equal(got[:, 0], expected)
    assert (info.subtype, info.frames, info.channels) == ("PCM_16", 4, 1)


def test_pcm24_decodes_the_documented_quantizer(tmp_path):
    # code = round(x * 8388607): 0.5 -> 4194303.5 -> 4194304 (half away from zero).
    audio = np.array([0.5, -1.0], dtype=np.float32)[:, None]
    native.wav_write(tmp_path / "q.wav", audio, 48_000, "PCM_24")
    got, info = read_wav(tmp_path / "q.wav")
    expected = np.array([4194304, -8388607], dtype=np.float32) / np.float32(8388608.0)
    np.testing.assert_array_equal(got[:, 0], expected)
    assert info.subtype == "PCM_24"


def test_extensible_header_and_odd_chunk_padding(tmp_path):
    # Hand-built file: LIST chunk of 3 bytes (+1 pad), then a 40-byte
    # WAVE_FORMAT_EXTENSIBLE fmt whose SubFormat GUID starts 0x0003
    # (IEEE float), then one float frame.
    fmt = struct.pack("<HHIIHH", 0xFFFE, 1, 8000, 32000, 4, 32)
    fmt += struct.pack("<HHI", 22, 32, 0x4)
    fmt += struct.pack("<H", 3) + bytes.fromhex("0000000010008000 00AA00389B71".replace(" ", ""))
    assert len(fmt) == 40
    data = struct.pack("<f", 0.75)
    body = b"WAVE"
    body += b"LIST" + struct.pack("<I", 3) + b"abc" + b"\x00"
    body += b"fmt " + struct.pack("<I", 40) + fmt
    body += b"data" + struct.pack("<I", 4) + data
    (tmp_path / "x.wav").write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    got, info = read_wav(tmp_path / "x.wav")
    assert info == type(info)(8000, 1, "FLOAT", 1)
    assert got[0, 0] == np.float32(0.75)
