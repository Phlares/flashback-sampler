# 0001. The audio engine is a Zig library under `core/`, zero external dependencies

Date: 2026-08-14. Status: accepted.

## Context

The first app was Python and Qt end to end. The ring buffer, capture, and WAV encode ran under the GIL with a mutex in the write path. That is fine for a desktop applet and wrong for a plugin or a phone.

## Decision

- The engine is a Zig library in this repo, under `core/`. One tracker, one CI, one place to run parity tests.
- It has zero external Zig dependencies. Everything it needs it writes: the ring, WAV, WASAPI bindings, the C ABI.
- The Zig version is pinned exactly in `core/build.zig.zon` and in CI. A bump is a deliberate PR.
- The engine talks to hosts through a C ABI (`core/include/flashback_core.h`). Python loads it with ctypes.

## Consequences

- The engine can sit under a CLAP plugin, an OBS dock, or a mobile shell without change. That is the point.
- No dependency drift, no build-time downloads. The cost is that we write bindings by hand.
- Cross-compiling for Linux and macOS is a CI check even while only the Windows backend exists.
