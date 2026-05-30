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
