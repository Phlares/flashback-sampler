"""
Quick Test — Audio Circular Buffer
===================================
Run this FIRST to validate everything works on your machine.

Install deps:
    pip install sounddevice numpy soundfile

Then run:
    python test_quick.py

Controls (keyboard):
    p        — play back last 10 seconds
    s        — save last 30 seconds to ./captures/
    l        — list all audio devices
    1-9      — play last N*10 seconds (e.g. '3' = last 30s)
    d        — show buffer status / diagnostics
    q        — quit
"""

import sys
import os
import time
import threading

# Allow running from project root or tests/ dir
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Dependency check ──────────────────────────────────────────────────────────
missing = []
for pkg in ["sounddevice", "numpy", "soundfile"]:
    try:
        __import__(pkg)
    except ImportError:
        missing.append(pkg)

if missing:
    print(f"\n❌  Missing packages: {', '.join(missing)}")
    print(f"    Run:  pip install {' '.join(missing)}\n")
    sys.exit(1)

import sounddevice as sd
from flashback_sampler.core.buffer import AudioCircularBuffer
from flashback_sampler.core.capture import AudioCapture
from flashback_sampler.core.loopback_capture import LoopbackCapture
from flashback_sampler.core.playback import AudioPlayback, AudioExporter

# ── Config ────────────────────────────────────────────────────────────────────
SAMPLE_RATE   = 48_000
CHANNELS      = 2        # 2 for stereo loopback from Windows speakers
BUFFER_MINS   = 1        # Keep 1 minute for this quick test (less RAM)
CAPTURE_DEVICE = None    # resolved below — WASAPI default output (loopback)
WASAPI_LOOPBACK = True   # capture what Windows is playing (no mic needed)

# ── Level meter (console) ─────────────────────────────────────────────────────
_last_rms = [0.0]

def level_callback(rms):
    _last_rms[0] = float(rms[0]) if hasattr(rms, '__len__') else float(rms)


def draw_meter():
    bars = int(_last_rms[0] * 40 / 0.1)   # scale to ~40 chars at 0.1 RMS
    bars = min(bars, 40)
    col = "\033[92m" if bars < 25 else "\033[93m" if bars < 35 else "\033[91m"
    print(f"\r  IN: {col}{'█' * bars}{'░' * (40-bars)}\033[0m  "
          f"RMS={_last_rms[0]:.4f}  ", end="", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "═"*55)
    print("  🎙  Audio Circular Buffer — Quick Test")
    print("═"*55)
    print(f"  Buffer: {BUFFER_MINS} min  |  {SAMPLE_RATE}Hz  |  {CHANNELS}ch")
    print("  Keys: [p]lay  [s]ave  [l]ist devices  [1-9]  [d]iag  [q]uit")
    print("═"*55 + "\n")

    # ── Allocate buffer ───────────────────────────────────────────────────────
    buf = AudioCircularBuffer(
        duration_seconds=BUFFER_MINS * 60,
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
    )
    player = AudioPlayback(sample_rate=SAMPLE_RATE, channels=CHANNELS)

    # ── Start capture ─────────────────────────────────────────────────────────
    # WASAPI loopback: uses `soundcard` (bypasses PortAudio, which doesn't
    # ship loopback support in the stock sounddevice wheel).
    # Mic / normal input: uses AudioCapture (sounddevice).
    if WASAPI_LOOPBACK:
        import soundcard as sc
        default_speaker = sc.default_speaker()
        print("\nAvailable speakers (WASAPI loopback sources):")
        for s in sc.all_speakers():
            marker = "→" if s.name == default_speaker.name else " "
            print(f"  {marker} {s.name}")
        print()
        cap = LoopbackCapture(
            buffer=buf,
            speaker_name=None,   # None = default speaker
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            on_level=level_callback,
        )
    else:
        print("\nAvailable input devices:")
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                marker = "→" if dev == sd.query_devices(kind="input") else " "
                print(f"  {marker} [{i:2d}] {dev['name']}  "
                      f"({dev['max_input_channels']}ch in)")
        print()
        cap = AudioCapture(
            buffer=buf,
            device=CAPTURE_DEVICE,
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            on_level=level_callback,
        )
    cap.start()

    # ── Meter redraw thread ───────────────────────────────────────────────────
    _quit = threading.Event()

    def meter_loop():
        while not _quit.is_set():
            draw_meter()
            time.sleep(0.05)

    meter_thread = threading.Thread(target=meter_loop, daemon=True)
    meter_thread.start()

    # ── Keyboard loop ─────────────────────────────────────────────────────────
    try:
        # Try to use msvcrt on Windows for non-blocking keys
        import msvcrt

        def get_key():
            if msvcrt.kbhit():
                return msvcrt.getwch()
            return None

        while True:
            time.sleep(0.1)
            key = get_key()
            if key is None:
                continue
            _handle_key(key, buf, player)
            if key == "q":
                break

    except ImportError:
        # Linux/Mac fallback — line-buffered input
        import sys, tty, termios

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                key = sys.stdin.read(1)
                _handle_key(key, buf, player)
                if key == "q":
                    break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    finally:
        _quit.set()
        cap.stop()
        print("\n\n[Done]")


def _handle_key(key, buf: AudioCircularBuffer, player: AudioPlayback):
    print()   # newline after meter
    if key == "p":
        if buf.buffered_seconds < 1:
            print("  ⚠  Not enough audio buffered yet")
        else:
            secs = min(10.0, buf.buffered_seconds)
            print(f"  ▶  Playing last {secs:.0f}s ...")
            player.play_from_buffer(buf, seconds=secs)

    elif key in "123456789":
        secs = int(key) * 10.0
        avail = buf.buffered_seconds
        if avail < 1:
            print("  ⚠  Not enough audio buffered yet")
        else:
            secs = min(secs, avail)
            print(f"  ▶  Playing last {secs:.0f}s ...")
            player.play_from_buffer(buf, seconds=secs)

    elif key == "s":
        secs = min(30.0, buf.buffered_seconds)
        if secs < 1:
            print("  ⚠  Not enough audio buffered yet")
        else:
            fname = AudioExporter.generate_filename("test_capture")
            path = os.path.join("captures", fname)
            AudioExporter.save_latest(buf, secs, path)

    elif key == "l":
        import sounddevice as sd
        print(sd.query_devices())

    elif key == "d":
        s = buf.status()
        print(f"  Buffer: {s['buffered_seconds']}s / {s['buffer_capacity_seconds']}s "
              f"({s['fill_percent']}%)  |  {s['memory_mb']} MB allocated")

    elif key == "q":
        pass   # handled in caller


if __name__ == "__main__":
    main()
