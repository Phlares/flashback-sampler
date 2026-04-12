"""
Unit tests for ScrubPlayer — the callback-driven preview player used to
audition checked-out clips.

These tests drive the audio callback directly with numpy buffers; no real
audio device is opened. The `open()` / `close()` wrappers that create an
actual sounddevice OutputStream are covered only by the opt-in audio_hw
integration tests.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from flashback_sampler.core.scrub_player import ScrubPlayer


def _ramp(n: int, channels: int = 1, start: float = 0.0) -> np.ndarray:
    """Monotonic float ramp — each sample equals its absolute index."""
    arr = np.arange(start, start + n, dtype=np.float32)
    return np.tile(arr[:, None], (1, channels))


# ─────────────────────────────────────────────────────────────────────────
# Empty / idle states
# ─────────────────────────────────────────────────────────────────────────


def test_new_player_has_no_source_and_is_not_playing():
    sp = ScrubPlayer(sample_rate=1000, channels=2)
    assert sp.cursor_samples == 0
    assert sp.is_playing is False


def test_callback_with_no_source_zero_fills_output():
    sp = ScrubPlayer(sample_rate=1000, channels=2)
    out = np.ones((128, 2), dtype=np.float32)
    sp._audio_callback(out, 128, None, None)
    assert np.all(out == 0.0)


def test_callback_while_paused_zero_fills_and_does_not_advance():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    sp.bind(_ramp(500, channels=1))
    # bind() leaves the player paused by default
    out = np.ones((100, 1), dtype=np.float32)
    sp._audio_callback(out, 100, None, None)
    assert np.all(out == 0.0)
    assert sp.cursor_samples == 0


# ─────────────────────────────────────────────────────────────────────────
# Normal playback
# ─────────────────────────────────────────────────────────────────────────


def test_callback_fills_from_cursor_and_advances():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    sp.bind(_ramp(1000, channels=1))
    sp.play()
    out = np.zeros((100, 1), dtype=np.float32)
    sp._audio_callback(out, 100, None, None)
    assert np.array_equal(out[:, 0], np.arange(100, dtype=np.float32))
    assert sp.cursor_samples == 100


def test_successive_callbacks_stream_contiguous_audio():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    sp.bind(_ramp(1000, channels=1))
    sp.play()
    out = np.zeros((100, 1), dtype=np.float32)
    sp._audio_callback(out, 100, None, None)
    first = out.copy()
    sp._audio_callback(out, 100, None, None)
    second = out.copy()
    assert np.array_equal(first[:, 0], np.arange(0, 100, dtype=np.float32))
    assert np.array_equal(second[:, 0], np.arange(100, 200, dtype=np.float32))
    assert sp.cursor_samples == 200


def test_stereo_playback_passes_through_both_channels():
    sp = ScrubPlayer(sample_rate=1000, channels=2)
    src = _ramp(500, channels=2)
    sp.bind(src)
    sp.play()
    out = np.zeros((50, 2), dtype=np.float32)
    sp._audio_callback(out, 50, None, None)
    assert np.array_equal(out, src[:50])


# ─────────────────────────────────────────────────────────────────────────
# End-of-source behavior (auto-stop, no loop)
# ─────────────────────────────────────────────────────────────────────────


def test_callback_at_exact_end_fills_nothing_and_stops():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    sp.bind(_ramp(100, channels=1))
    sp.play()
    out = np.zeros((100, 1), dtype=np.float32)
    sp._audio_callback(out, 100, None, None)  # drains source exactly
    # Request more — should zero-fill and not be playing
    out2 = np.ones((50, 1), dtype=np.float32)
    sp._audio_callback(out2, 50, None, None)
    assert np.all(out2 == 0.0)
    assert sp.is_playing is False
    assert sp.cursor_samples == 100


def test_partial_final_callback_fills_remaining_zero_pads_rest_and_stops():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    sp.bind(_ramp(110, channels=1))
    sp.play()
    out = np.zeros((100, 1), dtype=np.float32)
    sp._audio_callback(out, 100, None, None)  # cursor at 100
    out2 = np.zeros((50, 1), dtype=np.float32)
    sp._audio_callback(out2, 50, None, None)  # 10 real + 40 zero-padded
    assert np.array_equal(out2[:10, 0], np.arange(100, 110, dtype=np.float32))
    assert np.all(out2[10:] == 0.0)
    assert sp.is_playing is False
    assert sp.cursor_samples == 110


def test_play_after_drain_rewinds_to_zero():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    sp.bind(_ramp(100, channels=1))
    sp.play()
    out = np.zeros((100, 1), dtype=np.float32)
    sp._audio_callback(out, 100, None, None)
    assert sp.cursor_samples == 100
    # Auto-stopped at end; user hits play again
    sp.play()
    assert sp.cursor_samples == 0
    assert sp.is_playing is True


# ─────────────────────────────────────────────────────────────────────────
# Seek / scrub
# ─────────────────────────────────────────────────────────────────────────


def test_seek_samples_jumps_cursor_and_next_callback_starts_there():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    sp.bind(_ramp(1000, channels=1))
    sp.play()
    sp.seek_samples(500)
    out = np.zeros((50, 1), dtype=np.float32)
    sp._audio_callback(out, 50, None, None)
    assert np.array_equal(out[:, 0], np.arange(500, 550, dtype=np.float32))
    assert sp.cursor_samples == 550


def test_seek_seconds_is_samples_times_sample_rate():
    sp = ScrubPlayer(sample_rate=48_000, channels=1)
    sp.bind(_ramp(48_000, channels=1))
    sp.seek(0.25)  # 25% of a 1-second clip
    assert sp.cursor_samples == 12_000


def test_seek_clamped_to_source_bounds():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    sp.bind(_ramp(500, channels=1))
    sp.seek_samples(-100)
    assert sp.cursor_samples == 0
    sp.seek_samples(9999)
    assert sp.cursor_samples == 500


def test_cursor_seconds_property():
    sp = ScrubPlayer(sample_rate=48_000, channels=1)
    sp.bind(_ramp(48_000, channels=1))
    sp.seek_samples(24_000)
    assert sp.cursor_seconds == pytest.approx(0.5)


# ─────────────────────────────────────────────────────────────────────────
# Bind / stop — source lifecycle
# ─────────────────────────────────────────────────────────────────────────


def test_bind_resets_cursor_and_pauses():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    sp.bind(_ramp(1000, channels=1))
    sp.play()
    out = np.zeros((100, 1), dtype=np.float32)
    sp._audio_callback(out, 100, None, None)
    assert sp.cursor_samples == 100
    # Bind a new source — cursor resets and player pauses
    sp.bind(np.full((500, 1), 7.0, dtype=np.float32))
    assert sp.cursor_samples == 0
    assert sp.is_playing is False
    # Next callback yields zeros until play() is called again
    out2 = np.ones((50, 1), dtype=np.float32)
    sp._audio_callback(out2, 50, None, None)
    assert np.all(out2 == 0.0)
    sp.play()
    sp._audio_callback(out2, 50, None, None)
    assert np.all(out2 == 7.0)


def test_stop_zero_fills_and_clears_source():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    sp.bind(_ramp(500, channels=1))
    sp.play()
    sp.stop()
    assert sp.is_playing is False
    assert sp.cursor_samples == 0
    out = np.ones((50, 1), dtype=np.float32)
    sp._audio_callback(out, 50, None, None)
    assert np.all(out == 0.0)


def test_channel_mismatch_raises():
    sp = ScrubPlayer(sample_rate=1000, channels=2)
    mono = _ramp(500, channels=1)
    with pytest.raises(ValueError, match="channels"):
        sp.bind(mono)


def test_1d_input_raises():
    sp = ScrubPlayer(sample_rate=1000, channels=1)
    flat = np.arange(500, dtype=np.float32)
    with pytest.raises(ValueError):
        sp.bind(flat)


# ─────────────────────────────────────────────────────────────────────────
# Concurrency — seek and callback on different threads
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.timeout(5)
def test_concurrent_seek_during_callback_is_safe():
    sp = ScrubPlayer(sample_rate=48_000, channels=2)
    src = np.zeros((48_000 * 5, 2), dtype=np.float32)
    # Mark each sample with its index so we can verify contiguous reads
    src[:, 0] = np.arange(len(src), dtype=np.float32)
    src[:, 1] = np.arange(len(src), dtype=np.float32)
    sp.bind(src)
    sp.play()

    stop = threading.Event()
    errors: list[BaseException] = []

    def callback_loop():
        out = np.zeros((1024, 2), dtype=np.float32)
        try:
            while not stop.is_set():
                sp._audio_callback(out, 1024, None, None)
                if not sp.is_playing:
                    sp.play()
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=callback_loop, daemon=True)
    t.start()

    rng = np.random.default_rng(42)
    deadline = time.monotonic() + 0.3
    seeks = 0
    while time.monotonic() < deadline:
        sp.seek_samples(int(rng.integers(0, 48_000 * 4)))
        seeks += 1

    stop.set()
    t.join(timeout=1.0)
    assert errors == []
    assert seeks > 100
