"""
Unit tests for CheckoutTrack trim-selection logic. Focus is on the
pure-Python methods that mutate the bound Checkout's trim_in_samples
and trim_out_samples — the paint code is covered by the main-window
smoke run.
"""

from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from flashback_sampler.app.widgets.checkout_track import CheckoutTrack
from flashback_sampler.core.buffer import AudioCircularBuffer
from flashback_sampler.core.checkout import Checkout, CheckoutManager


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def bound_checkout_track(qapp):
    buf = AudioCircularBuffer(duration_seconds=5.0, sample_rate=48_000, channels=2)
    # Write enough audio for a 4-second checkout
    t = np.arange(48_000 * 5) / 48_000
    sine = (np.sin(2 * np.pi * 440.0 * t) * 0.5).astype(np.float32)
    buf.write(np.column_stack([sine, sine]))

    mgr = CheckoutManager(buffer=buf)
    co = mgr.create(duration_s=4.0)
    track = CheckoutTrack()
    track.set_checkout(co)
    return track, co


def test_set_checkout_starts_with_no_trim(bound_checkout_track):
    track, co = bound_checkout_track
    assert track.trim_range_seconds() is None


def test_drag_committed_sets_trim_in_and_out(bound_checkout_track):
    track, co = bound_checkout_track
    # Frac 0.25 .. 0.75 of a 4s clip = 1s .. 3s
    track._on_trim_drag_committed(0.25, 0.75)
    rng = track.trim_range_seconds()
    assert rng is not None
    lo, hi = rng
    assert lo == pytest.approx(1.0, abs=1e-4)
    assert hi == pytest.approx(3.0, abs=1e-4)
    assert co.trim_in_samples == pytest.approx(48_000, abs=2)
    assert co.trim_out_samples == pytest.approx(144_000, abs=2)


def test_set_mark_in_moves_trim_in(bound_checkout_track):
    track, co = bound_checkout_track
    track._on_trim_drag_committed(0.25, 0.75)  # (1.0, 3.0)
    track.set_mark_in(1.5)
    rng = track.trim_range_seconds()
    assert rng is not None
    lo, _ = rng
    assert lo == pytest.approx(1.5, abs=1e-4)
    assert co.trim_in_samples == pytest.approx(72_000, abs=2)


def test_set_mark_out_moves_trim_out(bound_checkout_track):
    track, co = bound_checkout_track
    track._on_trim_drag_committed(0.25, 0.75)  # (1.0, 3.0)
    track.set_mark_out(2.5)
    rng = track.trim_range_seconds()
    assert rng is not None
    _, hi = rng
    assert hi == pytest.approx(2.5, abs=1e-4)
    assert co.trim_out_samples == pytest.approx(120_000, abs=2)


def test_set_mark_in_rejects_if_past_mark_out(bound_checkout_track):
    track, co = bound_checkout_track
    track._on_trim_drag_committed(0.25, 0.75)  # (1.0, 3.0)
    # Try to set mark-in at 3.5 — past the current mark-out — should no-op
    prev_ti = co.trim_in_samples
    track.set_mark_in(3.5)
    assert co.trim_in_samples == prev_ti


def test_clear_trim_restores_full_range(bound_checkout_track):
    track, co = bound_checkout_track
    track._on_trim_drag_committed(0.25, 0.75)
    track.clear_trim()
    assert track.trim_range_seconds() is None
    assert co.trim_in_samples == 0
    assert co.trim_out_samples == 0


def test_checkout_trimmed_audio_respects_trim(bound_checkout_track):
    track, co = bound_checkout_track
    track._on_trim_drag_committed(0.25, 0.75)  # (1.0, 3.0) of 4s clip = 2s
    trimmed = co.trimmed_audio()
    # At 48 kHz, 2 seconds = 96_000 samples
    assert trimmed.shape[0] == pytest.approx(96_000, abs=2)


def test_rebinding_same_checkout_preserves_trim(bound_checkout_track):
    """set_checkout should not silently clobber existing trim state."""
    track, co = bound_checkout_track
    track._on_trim_drag_committed(0.25, 0.75)
    assert track.trim_range_seconds() is not None
    # Simulate the list-selection slot re-binding the same co
    track.set_checkout(co)
    assert track.trim_range_seconds() is not None


def test_setting_none_checkout_clears_overlay(bound_checkout_track):
    track, co = bound_checkout_track
    track._on_trim_drag_committed(0.25, 0.75)
    track.set_checkout(None)
    assert track.current_checkout_id() is None
    assert track.trim_range_seconds() is None


def test_set_cursor_without_trimmed_preview_is_absolute(bound_checkout_track):
    track, co = bound_checkout_track
    # With no preview_trimmed, set_cursor(seconds) uses seconds as
    # absolute clip position — the playhead lands exactly there.
    track.set_preview_trimmed(False)
    track.set_cursor(1.0)
    # Playhead frac = 1.0 / 4.0 = 0.25
    assert track._wave._playhead_frac == pytest.approx(0.25)


def test_set_cursor_with_trimmed_preview_offsets_by_trim_in(bound_checkout_track):
    """
    When previewing trimmed audio, ScrubPlayer.cursor_seconds is
    relative to trim-in. CheckoutTrack.set_cursor should add trim_in
    to produce the absolute clip position for the playhead.
    """
    track, co = bound_checkout_track
    # Set a trim: frac 0.25..0.75 -> samples 48000..144000 -> seconds 1..3
    track._on_trim_drag_committed(0.25, 0.75)
    track.set_preview_trimmed(True)
    # 0.5 seconds into the trimmed region = absolute 1.5 s in full clip
    track.set_cursor(0.5)
    # Playhead frac = 1.5 / 4.0 = 0.375
    assert track._wave._playhead_frac == pytest.approx(0.375)


def test_set_cursor_with_trimmed_preview_respects_zero_trim_in(bound_checkout_track):
    """
    If trim_out is set but trim_in is 0, cursor seconds are already
    absolute from the start of the clip.
    """
    track, co = bound_checkout_track
    co.trim_in_samples = 0
    co.trim_out_samples = 96000  # first 2 seconds
    track.set_preview_trimmed(True)
    track.set_cursor(1.0)
    # 1.0 s into the clip, no offset
    assert track._wave._playhead_frac == pytest.approx(0.25)
