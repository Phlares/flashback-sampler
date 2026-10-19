"""Teardown calls whose failure must not stop the caller."""

from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger(__name__)


def best_effort(fn: Callable[[], object]) -> bool:
    """Call `fn`. Log and swallow an `Exception` it raises; return True
    when it returned."""
    try:
        fn()
    except Exception:
        log.exception("best-effort call %r failed", fn)
        return False
    return True
