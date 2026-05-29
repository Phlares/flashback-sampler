from dataclasses import dataclass
from typing import Literal

EventKind = Literal["press", "release", "value"]


@dataclass(frozen=True)
class InputEvent:
    source: str
    kind: EventKind
    code: str
    value: float | None = None
    is_repeat: bool = False
