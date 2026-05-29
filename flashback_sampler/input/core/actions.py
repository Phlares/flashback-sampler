from dataclasses import dataclass
from typing import Callable, Literal

RepeatPolicy = Literal["fire", "ignore_repeat", "edge_only"]


@dataclass
class Action:
    id: str
    name: str
    category: str
    callable: Callable[[], None]
    default_binding: str | None = None
    repeat_policy: RepeatPolicy = "fire"


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
    if is_repeat and action.repeat_policy in ("ignore_repeat", "edge_only"):
        return
    action.callable()
