"""Minimal RIFF/WAVE reader for tests: FLOAT32 and PCM16/24, plain or
WAVE_FORMAT_EXTENSIBLE headers. `struct` walks the chunks; numpy decodes
the samples. This is the oracle for fb_wav_write, so it shares no code
with wav.zig and never calls the engine."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_EXTENSIBLE = 0xFFFE
_TAGS = {1: "PCM", 3: "FLOAT"}


@dataclass(frozen=True)
class WavInfo:
    samplerate: int
    channels: int
    subtype: str  # "FLOAT" | "PCM_16" | "PCM_24"
    frames: int


def read_wav(path) -> tuple[np.ndarray, WavInfo]:
    """(samples float32 (frames, channels), info). PCM codes are scaled by
    2**(bits-1) -- the libsndfile convention -- so 32767 reads as 32767/32768."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid, size = struct.unpack_from("<4sI", raw, pos)
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)  # chunks are word-aligned
    if fmt is None or data is None:
        raise ValueError("missing fmt or data chunk")
    tag, channels, rate, _byte_rate, _block_align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if tag == _EXTENSIBLE:
        # cbSize(2) validBits(2) channelMask(4) precede the SubFormat GUID
        # at offset 24; its first two bytes carry the real format tag.
        (tag,) = struct.unpack_from("<H", fmt, 24)
    kind = _TAGS.get(tag)
    if kind == "FLOAT" and bits == 32:
        samples = np.frombuffer(data, dtype="<f4").astype(np.float32)
        subtype = "FLOAT"
    elif kind == "PCM" and bits == 16:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / np.float32(32768.0)
        subtype = "PCM_16"
    elif kind == "PCM" and bits == 24:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        codes = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        codes = np.where(codes & 0x800000, codes - 0x1000000, codes)  # sign-extend
        samples = codes.astype(np.float32) / np.float32(8388608.0)
        subtype = "PCM_24"
    else:
        raise ValueError(f"unsupported format tag {tag:#x} at {bits} bits")
    frames = samples.shape[0] // channels
    return samples.reshape(frames, channels), WavInfo(rate, channels, subtype, frames)
