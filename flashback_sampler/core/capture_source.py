"""
CaptureSource — the runtime interface that every audio capture backend
implements.

Lives in `core/` because it's the contract the ring buffer and the
`CaptureSlot` multi-source refactor in M10.2+ depend on. The concrete
backends (WASAPI loopback via soundcard, mic/line-in via sounddevice,
eventually Windows per-process loopback, CoreAudioTap on macOS,
PipeWire on Linux) live in `core/loopback_capture.py` and
`core/capture.py` today and will migrate into a platform-specific
`io/` subpackage in a later milestone.

This module is intentionally import-cheap: no Qt, no soundcard, no
sounddevice. That lets unit tests instantiate fake capture sources
without pulling in the real hardware backends.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CaptureSource(Protocol):
    """
    Minimal structural type every capture backend must satisfy.

    The live audio pipeline is always:
        concrete CaptureSource -> AudioCircularBuffer.write(frames)

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
