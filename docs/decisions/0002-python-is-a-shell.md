# 0002. Python is a shell and will disappear

Date: 2026-08-30. Status: accepted.

## Context

Phase 1 moved the ring to Zig and kept capture, mixing, playback, and peak maths in Python. Each of those is a thread or a loop that a plugin host cannot run.

## Decision

- Every line of logic goes to Zig. `native.py` keeps ctypes declarations and one-line calls. No maths, no loops, no fallbacks in Python.
- Python creates handles, starts and stops them, and reads numbers. It never sees a frame.
- The Zig-less fallback is retired. The app refuses to start without the built library and shows the build command.
- The Qt UI is the last Python left. Its replacement is decision 0011.

## Consequences

- Capture, the mixer, playback, peak bins, WAV read and write, and the scratch cache all live in Zig today.
- Tests that compared Python and Zig died with the Python side. Regression tests run against Zig alone.
- `sounddevice`, `soundfile`, and `soundcard` are gone from the dependency list.
