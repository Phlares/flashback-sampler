"""
Persistent app settings — written to a JSON file under %APPDATA% on
Windows (or ~/.config on Unix). Holds device selections and the buffer
duration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


APP_DIR_NAME = "flashback-sampler"
CONFIG_FILE_NAME = "config.json"


def config_dir() -> Path:
    """Return the directory where config.json lives."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_DIR_NAME


def config_path() -> Path:
    return config_dir() / CONFIG_FILE_NAME


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load the config JSON. Returns {} if missing or malformed."""
    p = path or config_path()
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: dict[str, Any], path: Path | None = None) -> None:
    """Write the config JSON atomically (temp file + replace)."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(p)


def get_pref(key: str, default: Any, path: Path | None = None) -> Any:
    """Read a single top-level preference, falling back to `default`."""
    return load_config(path).get(key, default)


def set_pref(key: str, value: Any, path: Path | None = None) -> None:
    """Persist a single top-level preference (read-modify-write)."""
    data = load_config(path)
    data[key] = value
    save_config(data, path)


SHOW_NOTIFICATIONS_KEY = "show_notifications"


def load_show_notifications(path: Path | None = None) -> bool:
    """Whether tray toast notifications are enabled (default True)."""
    return bool(get_pref(SHOW_NOTIFICATIONS_KEY, True, path))


def save_show_notifications(enabled: bool, path: Path | None = None) -> None:
    set_pref(SHOW_NOTIFICATIONS_KEY, bool(enabled), path)


GLOBAL_HOTKEYS_KEY = "global_hotkeys_enabled"


def load_global_hotkeys_enabled(path: Path | None = None) -> bool:
    """Whether keybindings fire while minimized (global hotkeys). Off by
    default — opt-in, since global hotkeys claim OS-wide key combos."""
    return bool(get_pref(GLOBAL_HOTKEYS_KEY, False, path))


def save_global_hotkeys_enabled(enabled: bool, path: Path | None = None) -> None:
    set_pref(GLOBAL_HOTKEYS_KEY, bool(enabled), path)


EXPORT_POOL_DIR_KEY = "export_pool_dir"
EXPORT_BIT_DEPTH_KEY = "export_bit_depth"
VALID_EXPORT_BIT_DEPTHS = ("FLOAT", "PCM_24", "PCM_16")


def default_export_pool_dir() -> Path:
    """Where drag-exported slices land by default — user-visible, since
    the pool doubles as a sample bank (DAW projects reference these
    files in place; never auto-clean the pool)."""
    return Path.home() / "Documents" / "flashback-sampler" / "exports"


def load_export_pool_dir(path: Path | None = None) -> Path:
    raw = get_pref(EXPORT_POOL_DIR_KEY, "", path)
    return Path(raw) if raw else default_export_pool_dir()


def save_export_pool_dir(pool_dir: Path | str, path: Path | None = None) -> None:
    set_pref(EXPORT_POOL_DIR_KEY, str(pool_dir), path)


def load_export_bit_depth(path: Path | None = None) -> str:
    raw = get_pref(EXPORT_BIT_DEPTH_KEY, "FLOAT", path)
    return raw if raw in VALID_EXPORT_BIT_DEPTHS else "FLOAT"


def save_export_bit_depth(depth: str, path: Path | None = None) -> None:
    if depth not in VALID_EXPORT_BIT_DEPTHS:
        raise ValueError(
            f"invalid export bit depth {depth!r}; "
            f"must be one of {VALID_EXPORT_BIT_DEPTHS}"
        )
    set_pref(EXPORT_BIT_DEPTH_KEY, depth, path)
