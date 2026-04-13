"""
Unit tests for widget logic that can be verified without a running Qt
event loop. paintEvent code is covered by headless smoke runs, not here.
"""

from __future__ import annotations

import math

import pytest

from flashback_sampler.app.widgets.level_meter import (
    FLOOR_DB,
    N_SEGMENTS,
    level_to_lit_segments,
    segment_color_token,
)


# ─────────────────────────────────────────────────────────────────────────
# level_to_lit_segments — dBFS → segment count
# ─────────────────────────────────────────────────────────────────────────


def test_silence_lights_no_segments():
    assert level_to_lit_segments(0.0) == 0
    assert level_to_lit_segments(1e-9) == 0


def test_below_floor_lights_no_segments():
    # -61 dB is below our -60 floor
    rms_below = 10 ** (-61.0 / 20.0)
    assert level_to_lit_segments(rms_below) == 0


def test_just_above_floor_lights_at_least_one():
    rms = 10 ** ((FLOOR_DB + 3) / 20.0)  # -57 dB
    lit = level_to_lit_segments(rms)
    assert lit >= 1


def test_full_scale_lights_all_segments():
    assert level_to_lit_segments(1.0) == N_SEGMENTS
    assert level_to_lit_segments(2.0) == N_SEGMENTS


def test_half_fullscale_is_mid_ish():
    # -6 dBFS ≈ 0.5 RMS; should light roughly 18/20 segments
    lit = level_to_lit_segments(0.5)
    assert 17 <= lit <= 19


def test_minus_20_db_is_around_two_thirds():
    rms = 10 ** (-20.0 / 20.0)  # 0.1
    lit = level_to_lit_segments(rms)
    # -20 dB on a -60..0 scale is 40/60 = 66.6% → ~13/20 segments
    assert 12 <= lit <= 14


def test_monotonic_in_rms():
    prev = 0
    for db in range(-60, 1, 3):
        rms = 10 ** (db / 20.0)
        lit = level_to_lit_segments(rms)
        assert lit >= prev
        prev = lit


# ─────────────────────────────────────────────────────────────────────────
# segment_color_token — thermal mapping
# ─────────────────────────────────────────────────────────────────────────


def test_bottom_segments_are_meter_low():
    # Indices 0..11 (12/20 = 0.60) -> meter_low
    for i in range(12):
        assert segment_color_token(i) == "meter_low"


def test_mid_segments_are_meter_mid():
    for i in range(12, 17):  # 13/20..17/20 -> meter_mid
        assert segment_color_token(i) == "meter_mid"


def test_hot_segments_are_meter_hot():
    for i in range(17, 19):  # 18/20..19/20 -> meter_hot
        assert segment_color_token(i) == "meter_hot"


def test_top_segment_is_meter_peak():
    assert segment_color_token(N_SEGMENTS - 1) == "meter_peak"


# ─────────────────────────────────────────────────────────────────────────
# RotaryKnob value → angle mapping (pure function)
# ─────────────────────────────────────────────────────────────────────────


def test_rotary_value_at_min_maps_to_sweep_start():
    from flashback_sampler.app.widgets.rotary_knob import (
        SWEEP_START_DEG,
        value_to_angle_deg,
    )
    assert value_to_angle_deg(0.0, 0.0, 900.0) == pytest.approx(SWEEP_START_DEG)


def test_rotary_value_at_max_maps_to_sweep_end():
    from flashback_sampler.app.widgets.rotary_knob import (
        SWEEP_END_DEG,
        value_to_angle_deg,
    )
    assert value_to_angle_deg(900.0, 0.0, 900.0) == pytest.approx(SWEEP_END_DEG)


def test_rotary_midpoint_maps_to_midsweep():
    from flashback_sampler.app.widgets.rotary_knob import (
        SWEEP_END_DEG,
        SWEEP_START_DEG,
        value_to_angle_deg,
    )
    mid = (SWEEP_START_DEG + SWEEP_END_DEG) / 2
    assert value_to_angle_deg(450.0, 0.0, 900.0) == pytest.approx(mid)


def test_rotary_value_out_of_range_is_clamped():
    from flashback_sampler.app.widgets.rotary_knob import (
        SWEEP_END_DEG,
        SWEEP_START_DEG,
        value_to_angle_deg,
    )
    assert value_to_angle_deg(-10.0, 0.0, 900.0) == pytest.approx(SWEEP_START_DEG)
    assert value_to_angle_deg(9999.0, 0.0, 900.0) == pytest.approx(SWEEP_END_DEG)


def test_rotary_degenerate_range_returns_sweep_start():
    from flashback_sampler.app.widgets.rotary_knob import (
        SWEEP_START_DEG,
        value_to_angle_deg,
    )
    assert value_to_angle_deg(5.0, 10.0, 10.0) == pytest.approx(SWEEP_START_DEG)


# ─────────────────────────────────────────────────────────────────────────
# DurationPreset — format + step logic (no Qt needed)
# ─────────────────────────────────────────────────────────────────────────


def test_format_preset_under_a_minute():
    from flashback_sampler.app.widgets.duration_preset import format_preset
    assert format_preset(15) == "0:15"
    assert format_preset(30) == "0:30"


def test_format_preset_multi_minute():
    from flashback_sampler.app.widgets.duration_preset import format_preset
    assert format_preset(60) == "1:00"
    assert format_preset(180) == "3:00"
    assert format_preset(900) == "15:00"


def test_default_presets_cover_expected_range():
    from flashback_sampler.app.widgets.duration_preset import DEFAULT_PRESETS
    assert DEFAULT_PRESETS[0] == 15.0
    assert DEFAULT_PRESETS[-1] == 900.0
    assert 180.0 in DEFAULT_PRESETS
    # Monotonic
    for a, b in zip(DEFAULT_PRESETS, DEFAULT_PRESETS[1:]):
        assert a < b


# ─────────────────────────────────────────────────────────────────────────
# CheckoutTrack clip-binning helper
# ─────────────────────────────────────────────────────────────────────────


def test_compute_clip_bins_shape_and_min_max():
    from flashback_sampler.app.widgets.checkout_track import _compute_clip_bins
    import numpy as np

    audio = np.arange(1000, dtype=np.float32).reshape(-1, 1) / 1000.0
    bins = _compute_clip_bins(audio, n_bins=10)
    assert bins.shape == (10, 2, 1)
    # First bin: samples 0..99, min ≈ 0, max ≈ 0.099
    assert bins[0, 0, 0] == pytest.approx(0.0, abs=1e-6)
    assert bins[0, 1, 0] == pytest.approx(0.099, abs=1e-3)
    # Last bin: samples 900..999, max ≈ 0.999
    assert bins[-1, 1, 0] == pytest.approx(0.999, abs=1e-3)


def test_compute_clip_bins_empty_audio_returns_zeros():
    from flashback_sampler.app.widgets.checkout_track import _compute_clip_bins
    import numpy as np

    bins = _compute_clip_bins(np.zeros((0, 2), dtype=np.float32), n_bins=20)
    assert bins.shape == (20, 2, 2)
    assert np.all(bins == 0.0)


# ─────────────────────────────────────────────────────────────────────────
# compute_anchor_section — section band math
# ─────────────────────────────────────────────────────────────────────────


def test_anchor_section_at_now():
    from flashback_sampler.app.widgets.buffer_track import compute_anchor_section
    # 10 s buffered, 5 s duration, anchor at 0 → [5..10] seconds = [0.5..1.0]
    result = compute_anchor_section(
        anchor_offset_s=0.0, duration_s=5.0, buffered_s=10.0
    )
    assert result == pytest.approx((0.5, 1.0))


def test_anchor_section_middle_of_buffer():
    from flashback_sampler.app.widgets.buffer_track import compute_anchor_section
    # 10 s buffered, 3 s duration, anchor 2 s ago → [5..7] seconds = [0.5..0.8]
    result = compute_anchor_section(
        anchor_offset_s=2.0, duration_s=3.0, buffered_s=10.0
    )
    assert result is not None
    start, end = result
    assert start == pytest.approx(0.5)
    assert end == pytest.approx(0.8)


def test_anchor_section_at_oldest():
    """
    When the anchor pushes the clip start off the left edge, the section
    should clamp to [0, something] — still visible, just truncated.
    """
    from flashback_sampler.app.widgets.buffer_track import compute_anchor_section
    # 10 s buffered, 5 s duration, anchor 9 s ago:
    # requested [14..9 ago] → clamp start to 0
    # end_frac = 1 - 9/10 = 0.1, start_frac = 1 - 14/10 = -0.4 → 0
    result = compute_anchor_section(
        anchor_offset_s=9.0, duration_s=5.0, buffered_s=10.0
    )
    assert result is not None
    start, end = result
    assert start == pytest.approx(0.0)
    assert end == pytest.approx(0.1)


def test_anchor_section_duration_larger_than_buffer():
    """3 min preset on a 30 s buffer should fill the whole band."""
    from flashback_sampler.app.widgets.buffer_track import compute_anchor_section
    result = compute_anchor_section(
        anchor_offset_s=0.0, duration_s=180.0, buffered_s=30.0
    )
    assert result is not None
    start, end = result
    assert start == pytest.approx(0.0)
    assert end == pytest.approx(1.0)


def test_anchor_section_empty_buffer_returns_none():
    from flashback_sampler.app.widgets.buffer_track import compute_anchor_section
    assert compute_anchor_section(0.0, 3.0, 0.0) is None
    assert compute_anchor_section(0.0, 3.0, -1.0) is None


def test_anchor_section_zero_duration_returns_none():
    from flashback_sampler.app.widgets.buffer_track import compute_anchor_section
    assert compute_anchor_section(0.0, 0.0, 10.0) is None


def test_anchor_section_collapsed_at_oldest_returns_none():
    """
    When anchor is exactly at the oldest sample and duration is so
    large that both boundaries collapse to 0, the function returns None
    so the UI hides the band rather than drawing a zero-width rectangle.
    """
    from flashback_sampler.app.widgets.buffer_track import compute_anchor_section
    # 10 s buffered, anchor 10 s ago (the oldest), any duration:
    # end_frac = 1 - 10/10 = 0
    # start_frac clamps to 0
    # end <= start → None
    result = compute_anchor_section(
        anchor_offset_s=10.0, duration_s=5.0, buffered_s=10.0
    )
    assert result is None
