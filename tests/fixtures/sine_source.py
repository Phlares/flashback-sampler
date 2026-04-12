"""
Deterministic sine-wave source for buffer/checkout/scrub tests.

Produces identical audio every run so assertions can use exact equality.
Not tied to any real audio device — pure numpy.
"""

from __future__ import annotations

import numpy as np


def sine_block(
    start_sample: int,
    n_frames: int,
    freq_hz: float = 440.0,
    sample_rate: int = 48_000,
    channels: int = 2,
    amplitude: float = 0.5,
    phase_per_channel: tuple[float, ...] | None = None,
) -> np.ndarray:
    """
    Return a float32 block of shape (n_frames, channels) containing a sine
    wave continuous with `start_sample`. Each channel can have its own
    phase offset (default: each channel is offset by pi/2 * channel_index).
    """
    if phase_per_channel is None:
        phase_per_channel = tuple((np.pi / 2) * c for c in range(channels))
    t = (np.arange(n_frames, dtype=np.float64) + start_sample) / sample_rate
    out = np.empty((n_frames, channels), dtype=np.float32)
    omega = 2.0 * np.pi * freq_hz
    for c in range(channels):
        out[:, c] = (amplitude * np.sin(omega * t + phase_per_channel[c])).astype(
            np.float32
        )
    return out


def ramp_block(
    start_sample: int,
    n_frames: int,
    channels: int = 2,
) -> np.ndarray:
    """
    Return an integer ramp cast to float32, shape (n_frames, channels).
    Useful for verifying exact sample positions — each sample equals its
    absolute index, so tests can assert `arr[k] == start_sample + k`.
    """
    idx = np.arange(start_sample, start_sample + n_frames, dtype=np.float32)
    return np.tile(idx[:, None], (1, channels))
