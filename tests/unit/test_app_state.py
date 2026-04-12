"""
Unit tests for AppState — the headless object graph root.

These tests do not import PySide6; AppState is a plain Python container
that owns the buffer, checkout manager, and scrub player.
"""

from __future__ import annotations

import numpy as np

from flashback_sampler.app.state import AppState
from flashback_sampler.core.buffer import AudioCircularBuffer
from flashback_sampler.core.checkout import CheckoutManager
from flashback_sampler.core.scrub_player import ScrubPlayer


def test_appstate_wires_core_objects_with_matching_sample_rate_and_channels():
    st = AppState(buffer_seconds=5.0, sample_rate=16_000, channels=2)
    assert isinstance(st.buffer, AudioCircularBuffer)
    assert isinstance(st.checkout_manager, CheckoutManager)
    assert isinstance(st.scrub_player, ScrubPlayer)
    assert st.sample_rate == 16_000
    assert st.channels == 2
    assert st.buffer.sample_rate == 16_000
    assert st.buffer.channels == 2
    assert st.scrub_player.sample_rate == 16_000
    assert st.scrub_player.channels == 2


def test_appstate_is_not_capturing_initially():
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    assert st.is_capturing() is False
    assert st.capture is None


def test_shutdown_is_idempotent_without_capture_or_stream():
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    st.shutdown()  # must not raise


def test_checkout_from_live_buffer_then_bind_to_scrub_player():
    """
    End-to-end headless: push audio into the buffer, create a checkout,
    bind it to the scrub player, and verify the callback plays it back.
    This is the core P1 path exercised without any Qt or audio hardware.
    """
    st = AppState(buffer_seconds=1.0, sample_rate=1000, channels=1)
    # Write a ramp so we can assert exact sample positions
    ramp = np.arange(500, dtype=np.float32).reshape(-1, 1) / 500.0  # [0, 1)
    st.buffer.write(ramp)
    co = st.checkout_manager.create(duration_s=0.5)
    assert co.audio.shape == (500, 1)

    st.scrub_player.bind(co.audio)
    st.scrub_player.play()
    out = np.zeros((100, 1), dtype=np.float32)
    st.scrub_player._audio_callback(out, 100, None, None)
    assert np.allclose(out[:, 0], ramp[:100, 0])


# ─────────────────────────────────────────────────────────────────────────
# CLI argument parsing
# ─────────────────────────────────────────────────────────────────────────


def test_cli_defaults():
    from flashback_sampler.app.main import _parse_args

    args = _parse_args([])
    assert args.buffer_minutes == 15.0
    assert args.sample_rate == 48_000
    assert args.channels == 2


def test_cli_custom_buffer_for_rollover_testing():
    from flashback_sampler.app.main import _parse_args

    args = _parse_args(["--buffer-minutes", "0.5"])
    assert args.buffer_minutes == 0.5
    # AppState constructor accepts the computed seconds
    st = AppState(
        buffer_seconds=args.buffer_minutes * 60,
        sample_rate=args.sample_rate,
        channels=args.channels,
    )
    assert st.buffer.duration == 30.0


def test_cli_mono_override():
    from flashback_sampler.app.main import _parse_args

    args = _parse_args(["--channels", "1", "--sample-rate", "16000"])
    assert args.channels == 1
    assert args.sample_rate == 16_000


# ─────────────────────────────────────────────────────────────────────────
# Duration preset stepper logic (headless — tests the module constants
# and math; no Qt widget required)
# ─────────────────────────────────────────────────────────────────────────


def test_duration_presets_are_monotonic():
    from flashback_sampler.app.main_window import DURATION_PRESETS_S

    # Must be strictly ascending so the stepper makes sense
    for a, b in zip(DURATION_PRESETS_S, DURATION_PRESETS_S[1:]):
        assert a < b
    # Must contain the default (3:00) and cover 0:15 up to 15:00
    assert 15.0 in DURATION_PRESETS_S
    assert 180.0 in DURATION_PRESETS_S
    assert 900.0 in DURATION_PRESETS_S


def test_duration_default_index_maps_to_three_minutes():
    from flashback_sampler.app.main_window import (
        DEFAULT_DURATION_INDEX,
        DURATION_PRESETS_S,
    )

    assert DURATION_PRESETS_S[DEFAULT_DURATION_INDEX] == 180.0
