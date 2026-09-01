"""
Config persistence roundtrip tests. Does NOT touch the real %APPDATA%
— always passes an explicit tmp_path.
"""

from __future__ import annotations

from pathlib import Path

from flashback_sampler.app.config import (
    load_config,
    load_show_notifications,
    save_config,
    save_show_notifications,
)


def test_load_missing_config_returns_empty(tmp_path: Path):
    p = tmp_path / "config.json"
    assert load_config(p) == {}


def test_show_notifications_defaults_true_when_unset(tmp_path: Path):
    assert load_show_notifications(tmp_path / "config.json") is True


def test_show_notifications_roundtrip(tmp_path: Path):
    p = tmp_path / "config.json"
    save_show_notifications(False, p)
    assert load_show_notifications(p) is False
    save_show_notifications(True, p)
    assert load_show_notifications(p) is True


def test_show_notifications_save_preserves_other_keys(tmp_path: Path):
    p = tmp_path / "config.json"
    save_config({"capture_source": {"id": "X"}}, p)
    save_show_notifications(False, p)
    data = load_config(p)
    assert data["capture_source"] == {"id": "X"}  # not clobbered
    assert data["show_notifications"] is False


def test_save_and_load_roundtrip(tmp_path: Path):
    p = tmp_path / "config.json"
    data = {
        "capture_source": {"kind": "loopback", "id": "Speakers (Realtek)", "name": "x"},
        "preview_output": {"id": 7, "name": "Speakers (USB)"},
    }
    save_config(data, p)
    assert p.exists()
    loaded = load_config(p)
    assert loaded == data


def test_atomic_write_uses_tmp_then_replace(tmp_path: Path):
    p = tmp_path / "config.json"
    save_config({"a": 1}, p)
    # No leftover tmp file after a successful write
    assert not (p.with_suffix(p.suffix + ".tmp")).exists()


def test_corrupt_json_returns_empty(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert load_config(p) == {}


def test_non_dict_json_returns_empty(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_config(p) == {}


def test_save_creates_parent_directories(tmp_path: Path):
    p = tmp_path / "nested" / "deeper" / "config.json"
    save_config({"k": "v"}, p)
    assert p.exists()
    assert load_config(p) == {"k": "v"}


def test_global_hotkeys_defaults_false(tmp_path: Path):
    from flashback_sampler.app.config import load_global_hotkeys_enabled
    assert load_global_hotkeys_enabled(tmp_path / "config.json") is False


def test_global_hotkeys_roundtrip(tmp_path: Path):
    from flashback_sampler.app.config import (
        load_global_hotkeys_enabled,
        save_global_hotkeys_enabled,
    )
    p = tmp_path / "config.json"
    save_global_hotkeys_enabled(True, p)
    assert load_global_hotkeys_enabled(p) is True


def test_export_pool_dir_defaults_to_documents(tmp_path):
    from flashback_sampler.app.config import (
        default_export_pool_dir,
        load_export_pool_dir,
        save_export_pool_dir,
    )

    cfg = tmp_path / "config.json"
    assert load_export_pool_dir(cfg) == default_export_pool_dir()
    assert default_export_pool_dir() == (
        Path.home() / "Documents" / "flashback-sampler" / "exports"
    )
    save_export_pool_dir(tmp_path / "pool", cfg)
    assert load_export_pool_dir(cfg) == tmp_path / "pool"


def test_export_bit_depth_roundtrip_and_validation(tmp_path):
    import pytest
    from flashback_sampler.app.config import (
        load_export_bit_depth,
        save_export_bit_depth,
    )

    cfg = tmp_path / "config.json"
    assert load_export_bit_depth(cfg) == "FLOAT"
    save_export_bit_depth("PCM_24", cfg)
    assert load_export_bit_depth(cfg) == "PCM_24"
    with pytest.raises(ValueError):
        save_export_bit_depth("MP3", cfg)


def test_export_bit_depth_ignores_garbage_in_file(tmp_path):
    from flashback_sampler.app.config import (
        EXPORT_BIT_DEPTH_KEY,
        load_export_bit_depth,
    )
    from flashback_sampler.app.config import save_config

    cfg = tmp_path / "config.json"
    save_config({EXPORT_BIT_DEPTH_KEY: "banana"}, cfg)
    assert load_export_bit_depth(cfg) == "FLOAT"


def test_scratch_dir_defaults_under_user_cache_and_roundtrips(tmp_path):
    from flashback_sampler.app import config
    d = config.default_scratch_dir()
    assert d.name == "scratch"
    assert config.load_scratch_dir(tmp_path / "c.json") == d
    config.save_scratch_dir(tmp_path / "s", tmp_path / "c.json")
    assert config.load_scratch_dir(tmp_path / "c.json") == tmp_path / "s"


def test_checkout_cache_mb_roundtrip_and_floor(tmp_path):
    from flashback_sampler.app import config
    p = tmp_path / "c.json"
    assert config.load_checkout_cache_mb(p) == config.DEFAULT_CHECKOUT_CACHE_MB
    config.save_checkout_cache_mb(512, p)
    assert config.load_checkout_cache_mb(p) == 512.0
    config.save_checkout_cache_mb(-3, p)
    assert config.load_checkout_cache_mb(p) == 0.0
    # A negative (or garbage) value already sitting in config.json — not
    # written through save_checkout_cache_mb's own floor — must still be
    # floored on read. This is the case R-h7g calls out: load's floor is
    # the ONLY protection once a raw value is already on disk.
    config.save_config({config.CHECKOUT_CACHE_MB_KEY: -3}, p)
    assert config.load_checkout_cache_mb(p) == 0.0
    config.save_config({config.CHECKOUT_CACHE_MB_KEY: "banana"}, p)
    assert config.load_checkout_cache_mb(p) == config.DEFAULT_CHECKOUT_CACHE_MB


def test_max_footprint_default_is_a_quarter_of_physical_ram():
    from flashback_sampler.app import config
    # 64 GiB box -> 16384 MB; the default is derived at load, never stored.
    assert config.default_max_footprint_mb(64 * 1024 ** 3) == 16384.0


def test_max_footprint_roundtrip_unset_means_default_and_zero_means_uncapped(tmp_path):
    from flashback_sampler.app import config
    p = tmp_path / "config.json"
    assert config.load_max_footprint_mb(p, default=1234.0) == 1234.0  # unset -> default
    config.save_max_footprint_mb(2048, p)
    assert config.load_max_footprint_mb(p, default=1234.0) == 2048.0
    config.save_max_footprint_mb(0, p)
    assert config.load_max_footprint_mb(p, default=1234.0) == 0.0  # 0 = uncapped, kept
    config.save_max_footprint_mb(-5, p)
    assert config.load_max_footprint_mb(p, default=1234.0) == 0.0  # negative floors to uncapped


def test_drag_handle_mb_defaults_200_and_floors_at_zero(tmp_path):
    from flashback_sampler.app import config
    p = tmp_path / "c.json"
    assert config.load_drag_handle_mb(p) == 200.0  # on by default (best out-of-the-box UX); a tunable for constrained systems
    config.save_drag_handle_mb(0, p)
    assert config.load_drag_handle_mb(p) == 0.0
    config.save_drag_handle_mb(-1, p)
    assert config.load_drag_handle_mb(p) == 0.0
