"""
CaptureSource — the runtime interface that every audio capture backend
implements.

Lives in `core/` because it's the contract the ring buffer and the
`CaptureSlot` multi-source refactor in M10.2+ depend on. The concrete
backend is `core/native_capture.py` (`NativeCaptureSource`, one Zig
`Capture` per source) and `native_capture` (`NativeMixedSource`, N
sources into one ring). This module is import-cheap: no Qt, no
ctypes, so unit tests can instantiate fake sources.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CaptureSource(Protocol):
    """
    Minimal structural type every capture backend must satisfy.

    The live audio pipeline is always:
        concrete CaptureSource -> Ring.write (in Zig; fakes call NativeAudioCircularBuffer.write)

    Call order:
        1. `start()` — open the platform stream / COM apartment and
           begin piping frames into the bound buffer. Idempotent if
           already running.
        2. `stop()` — halt the stream. Idempotent if already stopped.

    Query helpers:
        `is_running()` — True while the capture stream is active.
        `xrun_count()` — count of audio-callback overruns / dropped
                         callbacks / buffer misses since construction.
                         Surfaced to the UI's status bar so the user
                         can see OS-level scheduling problems.
        `last_error()` — last human-readable error string produced by
                         the backend, or None. Polled by the UI so a
                         capture that died on a background thread can
                         still communicate why.

    Required attributes:
        `sample_rate: int`
        `channels: int`

    Notes:
        - CaptureSource is a Protocol (PEP 544), not a base class.
          Classes do NOT need to inherit from it — they satisfy it
          structurally as long as they expose the right members.
        - `@runtime_checkable` lets tests and factories use
          `isinstance(x, CaptureSource)` at runtime, but only verifies
          method presence, not signatures.
    """

    sample_rate: int
    channels: int

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_running(self) -> bool: ...
    def xrun_count(self) -> int: ...
    def last_error(self) -> str | None: ...
