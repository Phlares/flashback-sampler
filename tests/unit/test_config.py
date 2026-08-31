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
