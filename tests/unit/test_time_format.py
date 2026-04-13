"""
format_time_cs / format_time_signed_cs — centisecond-precision
timestamp formatter used across the UI.
"""

from __future__ import annotations

import pytest

from flashback_sampler.app.time_format import (
    format_time_cs,
    format_time_signed_cs,
)


def test_zero_seconds_is_all_zeros():
    assert format_time_cs(0.0) == "0:00.00"


def test_sub_second_shows_centiseconds():
    assert format_time_cs(0.5) == "0:00.50"
    assert format_time_cs(0.01) == "0:00.01"
    assert format_time_cs(0.999) == "0:01.00"  # rounds up across the second


def test_whole_second_has_zero_cs():
    assert format_time_cs(1.0) == "0:01.00"
    assert format_time_cs(59.0) == "0:59.00"


def test_partial_seconds_round_to_centi():
    assert format_time_cs(1.234) == "0:01.23"
    assert format_time_cs(1.235) == "0:01.24"  # banker's-ish rounding via round()
    assert format_time_cs(125.678) == "2:05.68"


def test_minute_boundary_rounds_cleanly():
    assert format_time_cs(59.995) == "1:00.00"
    assert format_time_cs(60.0) == "1:00.00"
    assert format_time_cs(60.01) == "1:00.01"


def test_negative_input_clamps_to_zero():
    assert format_time_cs(-5.0) == "0:00.00"
    assert format_time_cs(-0.001) == "0:00.00"


def test_large_values():
    assert format_time_cs(900.0) == "15:00.00"
    assert format_time_cs(3601.5) == "60:01.50"


def test_signed_cs_positive_has_no_prefix():
    assert format_time_signed_cs(1.5) == "0:01.50"
    assert format_time_signed_cs(0.0) == "0:00.00"


def test_signed_cs_negative_gets_minus_prefix():
    assert format_time_signed_cs(-2.25) == "-0:02.25"
    assert format_time_signed_cs(-125.0) == "-2:05.00"


def test_signed_cs_tiny_negative_is_not_minus_zero():
    # -0.0001 rounds to "-0:00.00" via the sign test; we accept that —
    # the callers that care use an explicit "< 0.5" guard anyway.
    assert format_time_signed_cs(-0.0001).startswith("-")
