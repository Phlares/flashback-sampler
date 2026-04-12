"""
hardware/encoder.py — Rotary encoder + buttons for Raspberry Pi

Wiring (KY-040 or generic EC11):
    CLK  → GPIO 17   (BCM numbering)
    DT   → GPIO 27
    SW   → GPIO 22   (push button)
    +    → 3.3V
    GND  → GND

Optional save button:
    BTN_SAVE → GPIO 23

Install:
    pip install RPi.GPIO

This module is intentionally decoupled from audio logic.
It emits events via callbacks so you can drop it into any controller.
"""

import threading
import time
from typing import Callable, Optional


class RotaryEncoder:
    """
    Reads a KY-040 rotary encoder using interrupt-driven GPIO.
    Fires on_rotate(delta) and on_press() callbacks.

    delta = +1 for clockwise, -1 for counter-clockwise.
    """

    # GPIO pin defaults — change to match your wiring
    DEFAULT_CLK  = 17
    DEFAULT_DT   = 27
    DEFAULT_SW   = 22
    DEFAULT_SAVE = 23   # optional second button

    def __init__(
        self,
        clk_pin: int = DEFAULT_CLK,
        dt_pin: int = DEFAULT_DT,
        sw_pin: int = DEFAULT_SW,
        save_pin: Optional[int] = DEFAULT_SAVE,
        on_rotate: Optional[Callable[[int], None]] = None,   # delta: +1 / -1
        on_press: Optional[Callable[[], None]] = None,        # knob pushed
        on_save: Optional[Callable[[], None]] = None,         # save button
        debounce_ms: int = 5,
    ):
        self.clk_pin = clk_pin
        self.dt_pin = dt_pin
        self.sw_pin = sw_pin
        self.save_pin = save_pin
        self.on_rotate = on_rotate
        self.on_press = on_press
        self.on_save = on_save
        self.debounce_ms = debounce_ms
        self._last_clk = None
        self._gpio = None

    def start(self) -> None:
        try:
            import RPi.GPIO as GPIO
        except ImportError:
            raise RuntimeError(
                "RPi.GPIO not available. "
                "Install with: pip install RPi.GPIO  (must run on Raspberry Pi)"
            )

        self._gpio = GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.clk_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self.dt_pin,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self.sw_pin,  GPIO.IN, pull_up_down=GPIO.PUD_UP)

        self._last_clk = GPIO.input(self.clk_pin)

        GPIO.add_event_detect(
            self.clk_pin, GPIO.BOTH,
            callback=self._clk_callback,
            bouncetime=self.debounce_ms
        )
        GPIO.add_event_detect(
            self.sw_pin, GPIO.FALLING,
            callback=self._sw_callback,
            bouncetime=50
        )

        if self.save_pin is not None:
            GPIO.setup(self.save_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(
                self.save_pin, GPIO.FALLING,
                callback=self._save_callback,
                bouncetime=50
            )
        print(f"[RotaryEncoder] Listening on CLK={self.clk_pin} "
              f"DT={self.dt_pin} SW={self.sw_pin}")

    def stop(self) -> None:
        if self._gpio:
            self._gpio.cleanup()

    # ── GPIO callbacks (ISR context — keep fast) ──────────────────────

    def _clk_callback(self, channel) -> None:
        GPIO = self._gpio
        clk = GPIO.input(self.clk_pin)
        dt  = GPIO.input(self.dt_pin)
        if clk != self._last_clk:
            delta = 1 if dt != clk else -1
            self._last_clk = clk
            if self.on_rotate:
                self.on_rotate(delta)

    def _sw_callback(self, channel) -> None:
        if self.on_press:
            self.on_press()

    def _save_callback(self, channel) -> None:
        if self.on_save:
            self.on_save()


# ── Scrub controller that maps encoder rotation to buffer position ─────────

class BufferScrubController:
    """
    Connects a RotaryEncoder to an AudioCircularBuffer + AudioPlayback.

    Turn knob → scrub position (10s per click by default)
    Press knob → play 5s preview from current position
    Save button → export segment to disk
    """

    def __init__(
        self,
        buffer,          # AudioCircularBuffer
        player,          # AudioPlayback
        exporter,        # AudioExporter class (not instance)
        step_seconds: float = 10.0,
        preview_seconds: float = 5.0,
        encoder_kwargs: dict = None,
    ):
        self.buffer = buffer
        self.player = player
        self.exporter = exporter
        self.step = step_seconds
        self.preview = preview_seconds

        # Cursor = how many seconds ago the "playhead" is
        # 0 = present, buffer.duration = oldest
        self._cursor_ago = 0.0   # seconds ago

        enc_kwargs = encoder_kwargs or {}
        self._encoder = RotaryEncoder(
            on_rotate=self._on_rotate,
            on_press=self._on_press,
            on_save=self._on_save,
            **enc_kwargs,
        )

    def start(self):
        self._encoder.start()

    def stop(self):
        self._encoder.stop()

    def _on_rotate(self, delta: int) -> None:
        self._cursor_ago += delta * self.step
        self._cursor_ago = max(0.0, min(self._cursor_ago,
                                        self.buffer.buffered_seconds - self.preview))
        print(f"\r  ◎ Cursor: {self._cursor_ago:.0f}s ago  ", end="", flush=True)

    def _on_press(self) -> None:
        start = self._cursor_ago + self.preview
        end   = self._cursor_ago
        print(f"\n  ▶ Preview: {start:.0f}s → {end:.0f}s ago")
        self.player.play_segment(self.buffer, start_ago=start, end_ago=end)

    def _on_save(self) -> None:
        start = self._cursor_ago + 60.0   # save 1 min around cursor
        end   = max(0.0, self._cursor_ago - 30.0)
        fname = self.exporter.generate_filename("pi_capture")
        path  = f"/home/pi/captures/{fname}"
        print(f"\n  💾 Saving {start:.0f}s → {end:.0f}s ago → {path}")
        self.exporter.save_segment(self.buffer, start, end, path)
