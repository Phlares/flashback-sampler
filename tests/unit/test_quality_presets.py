"""
Quality preset table + RAM math. Pure Python — no audio backends.
"""

from __future__ import annotations

import pytest

from flashback_sampler.core.quality_presets import (
    BYTES_PER_SAMPLE,
    DEFAULT_PRESET_NAME,
    MB,
    PRESETS,
    QualityPreset,
    compute_ram_bytes,
    compute_ram_mb,
    default_preset,
    preset_by_name,
)


# ─────────────────────────────────────────────────────────────────────────
# compute_ram_bytes / compute_ram_mb
# ─────────────────────────────────────────────────────────────────────────


def test_ram_full_preset():
    # 48000 Hz * 2 ch * 900 s * 4 bytes = 345_600_000
    b = compute_ram_bytes(48_000, 2, 900.0)
    assert b == 345_600_000
    mb = compute_ram_mb(48_000, 2, 900.0)
    assert mb == pytest.approx(329.59, abs=0.01)


def test_ram_chat_preset():
    # 16000 * 1 * 600 * 4 = 38_400_000 bytes ≈ 36.62 MB
    b = compute_ram_bytes(16_000, 1, 600.0)
    assert b == 38_400_000
    assert compute_ram_mb(16_000, 1, 600.0) == pytest.approx(36.62, abs=0.01)


def test_ram_voice_preset():
    # 22050 * 1 * 900 * 4 = 79_380_000 bytes ≈ 75.7 MB
    b = compute_ram_bytes(22_050, 1, 900.0)
    assert b == 79_380_000
    assert compute_ram_mb(22_050, 1, 900.0) == pytest.approx(75.70, abs=0.01)


def test_ram_scales_linearly_with_duration():
    a = compute_ram_bytes(48_000, 2, 60.0)
    b = compute_ram_bytes(48_000, 2, 120.0)
    assert b == 2 * a


def test_ram_scales_linearly_with_channels():
    mono = compute_ram_bytes(48_000, 1, 60.0)
    stereo = compute_ram_bytes(48_000, 2, 60.0)
    assert stereo == 2 * mono


def test_ram_zero_or_negative_returns_zero():
    assert compute_ram_bytes(0, 2, 60.0) == 0
    assert compute_ram_bytes(48_000, 0, 60.0) == 0
    assert compute_ram_bytes(48_000, 2, 0.0) == 0
    assert compute_ram_bytes(-48_000, 2, 60.0) == 0


def test_bytes_per_sample_is_float32():
    assert BYTES_PER_SAMPLE == 4


def test_mb_constant_is_mib():
    assert MB == 1024 * 1024


# ─────────────────────────────────────────────────────────────────────────
# PRESETS table
# ─────────────────────────────────────────────────────────────────────────


def test_presets_table_has_five_entries():
    assert len(PRESETS) == 5


def test_preset_names_are_unique_and_upper():
    names = [p.name for p in PRESETS]
    assert len(set(names)) == len(names)
    for n in names:
        assert n.isupper() and n != ""


def test_presets_cover_expected_sample_rates():
    rates = {p.sample_rate for p in PRESETS}
    assert 48_000 in rates
    assert 22_050 in rates
    assert 16_000 in rates


def test_preset_by_name_returns_expected_preset():
    full = preset_by_name("FULL")
    assert full is not None
    assert full.sample_rate == 48_000
    assert full.channels == 2
    assert full.buffer_seconds == 900.0


def test_preset_by_name_unknown_returns_none():
    assert preset_by_name("nonsense") is None


def test_default_preset_returns_music():
    p = default_preset()
    assert p.name == DEFAULT_PRESET_NAME == "MUSIC"


def test_preset_ram_matches_function():
    """The preset's ram_bytes() method must match compute_ram_bytes()."""
    for p in PRESETS:
        assert p.ram_bytes() == compute_ram_bytes(
            p.sample_rate, p.channels, p.buffer_seconds
        )
        assert p.ram_mb() == compute_ram_mb(
            p.sample_rate, p.channels, p.buffer_seconds
        )


def test_presets_match_expected_ram_footprints():
    """
    Sanity check against the RAM table in the plan addendum. Small
    drift is OK (the table rounds to MB), but an order-of-magnitude
    bug would fail here.
    """
    expected_mb = {
        "FULL":    329.59,   # 345_600_000 / 1024^2
        "MUSIC":   109.86,   # 115_200_000 / 1024^2
        "VOICE":    75.70,   #  79_380_000 / 1024^2
        "CHAT":     36.62,   #  38_400_000 / 1024^2
        "SCRATCH":  10.99,   #  11_520_000 / 1024^2
    }
    for p in PRESETS:
        assert p.ram_mb() == pytest.approx(expected_mb[p.name], abs=0.05)
