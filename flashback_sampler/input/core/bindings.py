import json
from pathlib import Path

import platformdirs

from flashback_sampler.input.core import actions
from flashback_sampler.input.core.events import InputEvent


SCHEMA_VERSION = 1


def default_storage_path() -> Path:
    # appauthor=False avoids platformdirs' default of repeating the app name
    # as the author segment (…/flashback-sampler/flashback-sampler). This is
    # only the fallback when no path is injected; the app injects a path that
    # co-locates bindings.json with its config.json.
    return Path(platformdirs.user_config_dir("flashback-sampler", appauthor=False)) / "bindings.json"


class BindingTable:
    """Maps event codes to action IDs.

    Resolution order: user overrides win over registered defaults. A user
    override may be ``None`` (explicitly cleared), which suppresses the
    default for that code. Missing entries fall back to defaults.
    """

    def __init__(self, storage_path: Path | None = None) -> None:
        # Overrides: code -> action_id or None (None = explicitly unbound)
        self._overrides: dict[str, str | None] = {}
        # Reverse set of codes that are overridden (for fast default suppression)
        self._overridden_action_ids: set[str] = set()
        self._storage_path: Path = storage_path or default_storage_path()

    # --- core mutation ---

    def bind(self, event_code: str, action_id: str) -> None:
        if actions.get(action_id) is None:
            raise ValueError(f"unknown action {action_id!r}")
        self._overrides[event_code] = action_id
        self._overridden_action_ids.add(action_id)

    def unbind(self, event_code: str) -> None:
        # Record explicit null so the default doesn't re-apply
        self._overrides[event_code] = None
        # Also suppress the default binding of whichever action owned this code
        for a in actions.all_actions():
            if a.default_binding == event_code:
                self._overridden_action_ids.add(a.id)

    def reset_one(self, action_id: str) -> None:
        # Drop any overrides pointing to this action
        self._overrides = {
            code: aid for code, aid in self._overrides.items() if aid != action_id
        }
        # Also drop null entries targeting the default code of this action
        a = actions.get(action_id)
        if a is not None and a.default_binding in self._overrides:
            if self._overrides[a.default_binding] is None:
                del self._overrides[a.default_binding]
        self._overridden_action_ids.discard(action_id)

    def reset_to_defaults(self) -> None:
        self._overrides.clear()
        self._overridden_action_ids.clear()

    # --- persistence ---

    def save(self) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": SCHEMA_VERSION, "bindings": self._overrides}
        self._storage_path.write_text(json.dumps(payload, indent=2))

    def load(self) -> None:
        if not self._storage_path.exists():
            return
        # A corrupt or hand-edited file must never break startup — fall back
        # to defaults (empty overrides) on any malformed content.
        try:
            data = json.loads(self._storage_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        overrides = data.get("bindings", {})
        if not isinstance(overrides, dict):
            return
        self._overrides = {
            str(code): aid for code, aid in overrides.items()
            if aid is None or isinstance(aid, str)
        }
        self._overridden_action_ids = {
            aid for aid in self._overrides.values() if aid is not None
        }
        # Re-suppress defaults for codes with explicit null
        for code, aid in self._overrides.items():
            if aid is None:
                for a in actions.all_actions():
                    if a.default_binding == code:
                        self._overridden_action_ids.add(a.id)

    # --- lookup ---

    def resolve(self, event: InputEvent) -> str | None:
        code = event.code
        if code in self._overrides:
            return self._overrides[code]  # may be None
        for a in actions.all_actions():
            if a.default_binding == code and a.id not in self._overridden_action_ids:
                return a.id
        return None

    def conflicts(self, event_code: str) -> str | None:
        return self.resolve(InputEvent(source="keyboard", kind="press", code=event_code))
