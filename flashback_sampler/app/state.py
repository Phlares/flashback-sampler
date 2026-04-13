"""
AppState — the root object graph for the Qt application layer.

Owns one buffer, one capture source, one checkout manager, and one scrub
player, plus the currently-selected capture/output device specs.
Instantiated once in main.py and shared across widgets. Nothing in this
file imports PySide6 — it's a plain Python container so unit tests can
drive it headless.
"""

from __future__ import annotations

import sys
from typing import Optional

from flashback_sampler.app.audio_devices import (
    CaptureDevice,
    OutputDevice,
    build_capture_source,
    default_capture_device,
    default_output_device,
)
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
        # Capture is lazy — wired by the main window when the user
        # clicks "Start Capture" for the first time.
        self._capture = None

        # Device selections. Start with the system defaults and let the
        # main window override them from config.json on startup.
        self.capture_spec: Optional[CaptureDevice] = default_capture_device()
        self.output_spec: Optional[OutputDevice] = default_output_device()
        if self.output_spec is not None:
            self.scrub_player.set_device(self.output_spec.id)

    @property
    def capture(self):
        return self._capture

    def set_capture(self, capture) -> None:
        self._capture = capture

    def is_capturing(self) -> bool:
        return self._capture is not None and getattr(
            self._capture, "_running", False
        )

    def set_capture_spec(self, spec: CaptureDevice) -> None:
        self.capture_spec = spec

    def set_output_spec(self, spec: OutputDevice) -> None:
        self.output_spec = spec
        self.scrub_player.set_device(spec.id)

    def build_capture(self):
        """
        Instantiate a capture source from the current capture_spec.
        Raises if no spec is selected.
        """
        if self.capture_spec is None:
            raise RuntimeError(
                "No capture device selected. Pick one from the Audio menu."
            )
        return build_capture_source(
            device=self.capture_spec,
            buffer=self.buffer,
            sample_rate=self.sample_rate,
            channels=self.channels,
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
    DEPRECATED: use `state.build_capture()` instead. Kept for any leftover
    callers from before M7.
    """
    if sys.platform != "win32":
        raise RuntimeError(
            "Loopback capture is Windows-only for now. "
            "Use a mic/line-in CaptureSource on this platform."
        )
    return state.build_capture()
