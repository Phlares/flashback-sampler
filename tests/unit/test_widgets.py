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
