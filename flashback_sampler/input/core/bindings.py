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
        # Drop any overrides pointing to this action…
        new_overrides = {
            code: aid for code, aid in self._overrides.items() if aid != action_id
        }
        # …and any explicit-null override on this action's own default code.
        a = actions.get(action_id)
        if (a is not None and a.default_binding in new_overrides
                and new_overrides[a.default_binding] is None):
            del new_overrides[a.default_binding]
        # Re-derive through the single normalization path so the suppression
        # set never goes stale (e.g. when actions share a default code).
        self.replace_overrides(new_overrides)

    def reset_to_defaults(self) -> None:
        self._overrides.clear()
        self._overridden_action_ids.clear()

    def overrides_snapshot(self) -> dict[str, str | None]:
        """A copy of the current overrides, for editors that buffer edits."""
        return dict(self._overrides)

    def replace_overrides(self, overrides: dict[str, str | None]) -> None:
        """Atomically replace all overrides and re-derive suppression state.

        This is the single normalization path — ``load()`` and the
        keybindings dialog both route through it, so the in-memory result of
        editing in the dialog is identical to reloading from disk.
        """
        self._overrides = {
            str(code): aid for code, aid in overrides.items()
            if aid is None or isinstance(aid, str)
        }
        self._overridden_action_ids = {
            aid for aid in self._overrides.values() if aid is not None
        }
        # Re-suppress defaults for codes carrying an explicit null override.
        for code, aid in self._overrides.items():
            if aid is None:
                for a in actions.all_actions():
                    if a.default_binding == code:
                        self._overridden_action_ids.add(a.id)

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
        self.replace_overrides(overrides)

    # --- lookup ---

    def resolve(self, event: InputEvent) -> str | None:
        code = event.code
        if code in self._overrides:
            return self._overrides[code]  # may be None
        for a in actions.all_actions():
            if a.default_binding == code and a.id not in self._overridden_action_ids:
                return a.id
        return None

    def remap_actions(self, mapping: dict[str, str]) -> bool:
        """Rewrite override targets from retired action ids to replacements —
        a one-time migration after an action is renamed or retired. Null
        overrides and unmapped targets are left untouched. Returns True if any
        override changed (so the caller can persist the migrated file)."""
        new = {
            code: (mapping.get(aid, aid) if aid is not None else None)
            for code, aid in self._overrides.items()
        }
        if new == self._overrides:
            return False
        self.replace_overrides(new)
        return True

    def binding_for(self, action_id: str) -> str | None:
        """The code currently bound to ``action_id`` (override or default), or
        None if unbound. The inverse of :meth:`resolve`; agrees with it for
        every code so global hotkeys and in-focus keys stay in lockstep."""
        for code, aid in self._overrides.items():
            if aid == action_id:
                return code
        a = actions.get(action_id)
        if a is not None and a.default_binding:
            code = a.default_binding
            # Default applies only if its code isn't overridden (by a null or by
            # another action) and the action's default isn't suppressed.
            if code not in self._overrides and action_id not in self._overridden_action_ids:
                return code
        return None
