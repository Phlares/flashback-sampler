"""
Config persistence roundtrip tests. Does NOT touch the real %APPDATA%
— always passes an explicit tmp_path.
"""

from __future__ import annotations

from pathlib import Path

from flashback_sampler.app.config import load_config, save_config


def test_load_missing_config_returns_empty(tmp_path: Path):
    p = tmp_path / "config.json"
    assert load_config(p) == {}


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
