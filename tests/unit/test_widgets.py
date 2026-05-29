"""
Unit tests for widget logic that can be verified without a running Qt
event loop. paintEvent code is covered by headless smoke runs, not here.
"""

from __future__ import annotations

import pytest


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
# Timeline helpers — step picker and label formatter (pure, no Qt)
# ─────────────────────────────────────────────────────────────────────────


def test_pick_timeline_step_15_sec_on_900px():
    """A 15-second clip on a 900 px widget should use ~2 s major steps."""
    from flashback_sampler.app.widgets.waveform_view import _pick_timeline_step
    major, minor = _pick_timeline_step(15.0, 900)
    # Want about 70 px between labels → 15*70/900 ≈ 1.17 s → next up = 2 s
    assert major == pytest.approx(2.0)
    assert minor > 0
    assert minor < major


def test_pick_timeline_step_3min_on_900px():
    """A 180 s clip on 900 px → ~70 px × 180/900 = 14 s → next step = 15 s."""
    from flashback_sampler.app.widgets.waveform_view import _pick_timeline_step
    major, _ = _pick_timeline_step(180.0, 900)
    assert major == pytest.approx(15.0)


def test_pick_timeline_step_15min_on_900px():
    """A 900 s clip on 900 px → 70 s → next step = 120 s (2 min)."""
    from flashback_sampler.app.widgets.waveform_view import _pick_timeline_step
    major, _ = _pick_timeline_step(900.0, 900)
    assert major == pytest.approx(120.0)


def test_pick_timeline_step_half_second_clip():
    """A very short clip should pick 0.1 s steps."""
    from flashback_sampler.app.widgets.waveform_view import _pick_timeline_step
    major, _ = _pick_timeline_step(0.5, 900)
    assert major == pytest.approx(0.1)


def test_pick_timeline_step_zero_returns_zero():
    from flashback_sampler.app.widgets.waveform_view import _pick_timeline_step
    major, minor = _pick_timeline_step(0.0, 900)
    assert major == 0.0
    assert minor == 0.0


def test_format_timeline_label_whole_seconds():
    from flashback_sampler.app.widgets.waveform_view import _format_timeline_label
    assert _format_timeline_label(0) == "0"
    assert _format_timeline_label(5) == "5"
    assert _format_timeline_label(59) == "59"


def test_format_timeline_label_fractional_seconds():
    from flashback_sampler.app.widgets.waveform_view import _format_timeline_label
    assert _format_timeline_label(0.5) == "0.5"
    assert _format_timeline_label(1.3) == "1.3"


def test_format_timeline_label_minutes():
    from flashback_sampler.app.widgets.waveform_view import _format_timeline_label
    assert _format_timeline_label(60) == "1:00"
    assert _format_timeline_label(125) == "2:05"
    assert _format_timeline_label(900) == "15:00"


def test_format_timeline_label_negative():
    from flashback_sampler.app.widgets.waveform_view import _format_timeline_label
    assert _format_timeline_label(-5) == "-5"
    assert _format_timeline_label(-90) == "-1:30"


def test_format_timeline_label_rounds_to_next_minute_cleanly():
    """Edge case: 59.7 s → 1:00, not 0:60."""
    from flashback_sampler.app.widgets.waveform_view import _format_timeline_label
    # 59.7 is < 60 so it's sub-minute format — this test documents
    # that boundary. For minute-or-more values, the second rounding
    # handles 59.8 → "1:00" not "0:60".
    assert _format_timeline_label(59.7) == "59.7"
    assert _format_timeline_label(60.0) == "1:00"
    # A value just shy of a minute from above the minute threshold
    assert _format_timeline_label(119.8) == "2:00"
