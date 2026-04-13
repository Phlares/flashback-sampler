"""
Persistent app settings — written to a JSON file under %APPDATA% on
Windows (or ~/.config on Unix). Currently holds device selections and
the buffer duration; the settings dialog (backlog B5) will extend this.
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
