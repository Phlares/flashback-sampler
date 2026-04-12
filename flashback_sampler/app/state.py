"""
AppState — the root object graph for the Qt application layer.

Owns one buffer, one capture source, one checkout manager, and one scrub
player. Instantiated once in main.py and shared across controllers and
widgets. Nothing in this file imports PySide6 — it's a plain Python
container so unit tests can drive it headless.
"""

from __future__ import annotations

import sys
from typing import Optional

from flashback_sampler.core.buffer import AudioCircularBuffer
from flashback_sampler.core.checkout import CheckoutManager
from flashback_sampler.core.scrub_player import ScrubPlayer


# Default capture target: 15-minute rolling buffer at 48 kHz stereo.
# Size: 15 * 60 * 48_000 * 2 * 4 bytes ≈ 330 MB. Matches the original
# prototype default and is what the UI will display initially.
DEFAULT_BUFFER_SECONDS = 15 * 60
DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_CHANNELS = 2


class AppState:
    def __init__(
        self,
        buffer_seconds: float = DEFAULT_BUFFER_SECONDS,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
    ):
        self.sample_rate = sample_rate
        self.channels = channels

        self.buffer = AudioCircularBuffer(
            duration_seconds=buffer_seconds,
            sample_rate=sample_rate,
            channels=channels,
        )
        self.checkout_manager = CheckoutManager(
            buffer=self.buffer,
            max_active_checkouts=16,
            max_total_ram_mb=1024,
        )
        self.scrub_player = ScrubPlayer(
            sample_rate=sample_rate,
            channels=channels,
        )
        # Capture is lazy — wired by the CaptureController when the user
        # clicks "Start Capture" for the first time.
        self._capture = None

    @property
    def capture(self):
        return self._capture

    def set_capture(self, capture) -> None:
        self._capture = capture

    def is_capturing(self) -> bool:
        return self._capture is not None and getattr(
            self._capture, "_running", False
        )

    def shutdown(self) -> None:
        """Called on window close — stop capture + playback cleanly."""
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:  # pragma: no cover
                pass
        try:
            self.scrub_player.close()
        except Exception:  # pragma: no cover
            pass


def make_loopback_capture(state: AppState):
    """
    Construct the platform-appropriate default capture source. On Windows
    this is the soundcard-based WASAPI loopback. Other platforms raise —
    the UI will disable the Start Capture button until a non-default
    source is wired.
    """
    if sys.platform != "win32":
        raise RuntimeError(
            "Loopback capture is Windows-only for now. "
            "Use a mic/line-in CaptureSource on this platform."
        )
    from flashback_sampler.core.loopback_capture import LoopbackCapture

    return LoopbackCapture(
        buffer=state.buffer,
        sample_rate=state.sample_rate,
        channels=state.channels,
    )
