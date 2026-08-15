"""
Session-wide pytest configuration.

FLASHBACK_REQUIRE_NATIVE=1 turns a missing flashback_core library into a
hard session failure instead of the usual per-test skip (see native.py's
module docstring and tests/unit/test_native_smoke.py / test_buffer.py's
buffer_cls fixture). Without this gate, a broken or skipped Zig build
step would make the parity harness -- the phase's correctness gate --
silently run its Python half only: a skipped native/Zig parity suite is
indistinguishable from a passing one in the CI summary line. CI's pytest
job sets this variable after building the library; local dev runs never
set it, so Zig-less workstations stay green as designed.
"""
import os

import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    if os.environ.get("FLASHBACK_REQUIRE_NATIVE") != "1":
        return
    from flashback_sampler.core import native

    if native.load() is None:
        pytest.exit(
            "FLASHBACK_REQUIRE_NATIVE=1 but the flashback_core native "
            "library was not found (checked core/zig-out/bin, "
            "core/zig-out/lib, and the flashback_sampler/core package "
            "directory). The native/Zig half of the parity harness would "
            "silently skip. Build it first: "
            "`zig build -Doptimize=ReleaseSafe` (working-directory: core).",
            returncode=1,
        )
