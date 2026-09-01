# 0004. ReleaseSafe ships; the audio path allocates nothing and never blocks

Date: 2026-08-14. Status: accepted.

## Context

Zig's safety checks cost a few percent. The audio path runs at a few hundred frames per callback. Bounds checks are cheap next to a dropout we cannot explain.

## Decision

- The shipped optimize mode is ReleaseSafe. Bounds checks stay on. Debug is for tests.
- `Ring.write` has no lock, no allocation, and no failure path. Any change that breaks that is wrong regardless of what it fixes.
- The same rule covers `Capture.run`, the mixer tick, and the playback render loop. Fixed buffers, atomics, allocation only at `init` or `bind` on the control thread.
- Arithmetic that a hostile input can overflow uses checked forms (`std.math.mul`, subtraction-form range guards) so the answer is a status, not a trap.

## Consequences

- A panic in ReleaseSafe is a bug report with a stack, not silent corruption.
- Every new thread honours one rule: an early return from its loop still owns its registrations until the control thread's `stop()`.
- Hosts that construct a `Ring` directly get the same guards the C ABI has.
