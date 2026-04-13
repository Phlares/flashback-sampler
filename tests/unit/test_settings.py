"""
Unit tests for AppSettings load / save / clamp logic. The SettingsDialog
widget itself is headless-smoke-tested via the main window; this file
focuses on the pure-Python contract so the load path never raises on a
corrupt config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flashback_sampler.app.settings_dialog import (
    DEFAULT_BUFFER_MINUTES,
    DEFAULT_MAX_CHECKOUTS,
    DEFAULT_MAX_RAM_MB,
    MAX_BUFFER_MINUTES,
    MAX_MAX_CHECKOUTS,
    MAX_MAX_RAM_MB,
    MIN_BUFFER_MINUTES,
    MIN_MAX_CHECKOUTS,
    MIN_MAX_RAM_MB,
    AppSettings,
    apply_settings_to_config,
    load_settings_from_config,
)


# ─────────────────────────────────────────────────────────────────────────
# AppSettings.from_dict clamp + defaults
# ─────────────────────────────────────────────────────────────────────────


def test_from_dict_missing_returns_defaults():
    s = AppSettings.from_dict(None)
    assert s.buffer_minutes == DEFAULT_BUFFER_MINUTES
    assert s.max_checkouts == DEFAULT_MAX_CHECKOUTS
    assert s.max_ram_mb == DEFAULT_MAX_RAM_MB
    assert s.save_directory == ""


def test_from_dict_non_dict_returns_defaults():
    s = AppSettings.from_dict([1, 2, 3])  # type: ignore[arg-type]
    assert s.buffer_minutes == DEFAULT_BUFFER_MINUTES


def test_from_dict_clamps_buffer_minutes_to_bounds():
    assert AppSettings.from_dict({"buffer_minutes": -5}).buffer_minutes == MIN_BUFFER_MINUTES
    assert AppSettings.from_dict({"buffer_minutes": 9999}).buffer_minutes == MAX_BUFFER_MINUTES


def test_from_dict_clamps_max_checkouts():
    assert AppSettings.from_dict({"max_checkouts": 0}).max_checkouts == MIN_MAX_CHECKOUTS
    assert AppSettings.from_dict({"max_checkouts": 9999}).max_checkouts == MAX_MAX_CHECKOUTS


def test_from_dict_clamps_max_ram_mb():
    assert AppSettings.from_dict({"max_ram_mb": 0}).max_ram_mb == MIN_MAX_RAM_MB
    assert AppSettings.from_dict({"max_ram_mb": 9999999}).max_ram_mb == MAX_MAX_RAM_MB


def test_from_dict_coerces_string_numbers():
    s = AppSettings.from_dict(
        {"buffer_minutes": "10", "max_checkouts": "8", "max_ram_mb": "512"}
    )
    assert s.buffer_minutes == 10.0
    assert s.max_checkouts == 8
    assert s.max_ram_mb == 512


def test_from_dict_falls_back_on_garbage_values():
    s = AppSettings.from_dict(
        {"buffer_minutes": "orange", "max_checkouts": None, "max_ram_mb": "???"}
    )
    assert s.buffer_minutes == DEFAULT_BUFFER_MINUTES
    assert s.max_checkouts == DEFAULT_MAX_CHECKOUTS
    assert s.max_ram_mb == DEFAULT_MAX_RAM_MB


def test_from_dict_preserves_save_directory():
    s = AppSettings.from_dict({"save_directory": "C:/Music/captures"})
    assert s.save_directory == "C:/Music/captures"


# ─────────────────────────────────────────────────────────────────────────
# Roundtrip through load_settings_from_config / apply_settings_to_config
# ─────────────────────────────────────────────────────────────────────────


def test_load_from_empty_config_returns_defaults():
    s = load_settings_from_config({})
    assert s.buffer_minutes == DEFAULT_BUFFER_MINUTES


def test_apply_settings_adds_settings_key():
    cfg = {"capture_source": {"kind": "loopback"}}
    new_cfg = apply_settings_to_config(cfg, AppSettings(buffer_minutes=2.5))
    assert new_cfg["capture_source"] == {"kind": "loopback"}
    assert new_cfg["settings"]["buffer_minutes"] == 2.5


def test_apply_settings_does_not_mutate_input():
    cfg = {"settings": {"buffer_minutes": 10.0}}
    apply_settings_to_config(cfg, AppSettings(buffer_minutes=5.0))
    assert cfg["settings"]["buffer_minutes"] == 10.0  # unchanged


def test_load_then_apply_roundtrip():
    cfg = {}
    s1 = AppSettings(buffer_minutes=7.5, max_checkouts=12, max_ram_mb=2048)
    cfg2 = apply_settings_to_config(cfg, s1)
    s2 = load_settings_from_config(cfg2)
    assert s2.buffer_minutes == 7.5
    assert s2.max_checkouts == 12
    assert s2.max_ram_mb == 2048


# ─────────────────────────────────────────────────────────────────────────
# resolved_save_directory fallback chain
# ─────────────────────────────────────────────────────────────────────────


def test_resolved_save_directory_uses_existing_configured_dir(tmp_path: Path):
    target = tmp_path / "mysaves"
    target.mkdir()
    s = AppSettings(save_directory=str(target))
    assert s.resolved_save_directory() == target


def test_resolved_save_directory_falls_back_when_configured_missing():
    s = AppSettings(save_directory="/path/that/definitely/does/not/exist/12345")
    result = s.resolved_save_directory()
    # Falls back to Documents or home — both are real dirs
    assert result.exists()


def test_resolved_save_directory_empty_returns_documents_or_home():
    s = AppSettings(save_directory="")
    result = s.resolved_save_directory()
    assert result.exists()
