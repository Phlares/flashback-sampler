"""
WASAPI loopback smoke test using the `soundcard` library.

This bypasses PortAudio/sounddevice entirely and talks to WASAPI directly.
Records 10 seconds of whatever your default speaker is playing, writes it
to ./captures/loopback_test.wav, then plays it back through the same speaker.

Install:
    pip install soundcard soundfile numpy

Run:
    python tests/test_loopback_soundcard.py
"""

import os
import sys
import time
import numpy as np

try:
    import soundcard as sc
    import soundfile as sf
except ImportError as e:
    print(f"\n[FAIL] Missing dependency: {e.name}")
    print("       Run: pip install soundcard soundfile numpy\n")
    sys.exit(1)

SAMPLE_RATE = 48_000
CHANNELS = 2
DURATION_S = 10
OUT_PATH = os.path.join("captures", "loopback_test.wav")


def main():
    print("\n" + "=" * 55)
    print("  WASAPI Loopback Smoke Test (soundcard)")
    print("=" * 55)

    # List speakers and microphones (including loopback mics)
    print("\nSpeakers:")
    for s in sc.all_speakers():
        marker = "->" if s == sc.default_speaker() else "  "
        print(f"  {marker} {s.name}")

    print("\nMicrophones (include_loopback=True):")
    for m in sc.all_microphones(include_loopback=True):
        tag = "[LOOPBACK]" if m.isloopback else "          "
        print(f"  {tag} {m.name}")

    # Grab the loopback mic of the default speaker
    default_speaker = sc.default_speaker()
    loopback_mic = sc.get_microphone(
        id=str(default_speaker.name), include_loopback=True
    )
    print(f"\nCapturing from: {loopback_mic.name}")
    print(f"Duration: {DURATION_S}s @ {SAMPLE_RATE}Hz, {CHANNELS}ch")
    print("\n>>> Play some audio in any app NOW <<<\n")
    time.sleep(1)

    total_frames = SAMPLE_RATE * DURATION_S
    with loopback_mic.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS) as rec:
        chunks = []
        captured = 0
        t0 = time.time()
        while captured < total_frames:
            chunk = rec.record(numframes=4096)
            chunks.append(chunk)
            captured += len(chunk)
            # Simple peak meter
            peak = float(np.max(np.abs(chunk))) if len(chunk) else 0.0
            bars = min(int(peak * 40), 40)
            elapsed = time.time() - t0
            print(f"\r  [{elapsed:4.1f}s] {'#' * bars}{'.' * (40 - bars)}  "
                  f"peak={peak:.3f}  ", end="", flush=True)

    audio = np.concatenate(chunks)[:total_frames]
    print(f"\n\nCaptured {len(audio)} samples ({len(audio)/SAMPLE_RATE:.1f}s)")
    print(f"Overall peak: {np.max(np.abs(audio)):.3f}")
    print(f"Overall RMS:  {np.sqrt(np.mean(audio ** 2)):.4f}")

    if np.max(np.abs(audio)) < 1e-4:
        print("\n[WARN] Captured audio is silent. Was anything playing?")

    # Save
    os.makedirs("captures", exist_ok=True)
    sf.write(OUT_PATH, audio, SAMPLE_RATE, format="WAV")
    print(f"\nSaved: {OUT_PATH}")

    # Play it back through the default speaker
    print("\nPlaying back through default speaker...")
    default_speaker.play(audio, samplerate=SAMPLE_RATE)
    print("Done.\n")


if __name__ == "__main__":
    main()
