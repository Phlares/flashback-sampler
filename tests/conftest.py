"""
Session-wide pytest configuration.

The suite requires the built flashback_core library: there is no Python
ring buffer, so a missing library exits the session with the build
command instead of skipping.
"""
import pytest


def pytest_sessionstart(session: pytest.Session) -> None:
    from flashback_sampler.core import native

    if native.load() is None:
        pytest.exit(
            "flashback_core native library not found (checked core/zig-out/bin, "
            "core/zig-out/lib, and flashback_sampler/core). There is no Python "
            "ring buffer any more, so nothing can run without it. Build it: "
            "`zig build --build-file core/build.zig -Doptimize=ReleaseSafe`.",
            returncode=1,
        )
