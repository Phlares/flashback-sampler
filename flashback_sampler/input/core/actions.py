from dataclasses import dataclass
from typing import Callable, Literal

RepeatPolicy = Literal["fire", "ignore_repeat"]


@dataclass
class Action:
    id: str
    name: str
    category: str
    callable: Callable[[], None]
    default_binding: str | None = None
    repeat_policy: RepeatPolicy = "fire"
    # Eligible to also fire while the app is minimized/hidden, via an OS-level
    # global hotkey. Only true for "grab it from another app" actions; their
    # binding must be modifier-qualified (Win32 can't register a bare key).
    is_global: bool = False
    # Shown in the Keybindings dialog as user-rebindable. False for internal
    # primitives driven only by buttons/tray (e.g. explicit start/stop, where
    # the user-facing rebindable action is a single toggle instead).
    bindable: bool = True


_registry: dict[str, Action] = {}


def register(action: Action) -> None:
    if action.id in _registry:
        raise ValueError(f"Action {action.id!r} already registered")
    _registry[action.id] = action


def get(action_id: str) -> Action | None:
    return _registry.get(action_id)


def all_actions() -> list[Action]:
    return list(_registry.values())


def clear_registry() -> None:
    _registry.clear()


def invoke(action_id: str, *, is_repeat: bool = False) -> None:
    action = _registry.get(action_id)
    if action is None:
        return
    if is_repeat and action.repeat_policy == "ignore_repeat":
        return
    action.callable()
