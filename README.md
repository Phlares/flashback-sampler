# flashback-sampler

A standalone desktop applet that continuously captures the past several minutes of Windows system audio into a circular ring buffer. Pull a slice of it out as a "checkout" (like lifting a record off a turntable while another one keeps spinning), preview it, and decide whether to save it to WAV/FLAC or discard it.

Designed with an eventual VST / OBS plugin port in mind — the audio core is intentionally framework-agnostic (pure Python + numpy, no Qt imports), and the UI is PySide6 native rather than a webview so it can later be embedded inside a DAW or OBS dock.

## Status

P1 core complete and wired through a minimal UI. You can capture Windows speaker audio via WASAPI loopback, check out a clip in any of 8 preset durations (0:15 to 15:00), preview it through your default output, save it to WAV or FLAC, or discard it. Ring buffer is non-blocking under multi-megabyte reads (seqlock pattern); writer is never stalled by checkouts. All P1 logic is TDD-covered (66 unit tests).

The full TP-7-style visual chassis (custom-painted waveform view, rotary knob, thermal VU meter, Monaspace typography, two-track layout) lands in M5–M8.

## Install

```powershell
pip install -e ".[dev]"
```

Installs the package plus test deps. Windows only for real capture (needs the `soundcard` library for WASAPI loopback); tests are cross-platform via fake audio sources.

## Run the app

```powershell
python -m flashback_sampler.app.main
```

CLI flags:

- `--buffer-minutes N` — ring buffer length (default 15). Use `0.5` to force a rollover quickly for testing.
- `--sample-rate N` — capture sample rate (default 48000).
- `--channels N` — 1 mono or 2 stereo (default 2).

## Flow

1. **START CAPTURE** — begins WASAPI loopback capture from the default speaker. Watch the dBFS level meter and the fill % climb.
2. **–** / **+** next to **CHECK OUT** — pick a clip duration: 0:15, 0:30, 1:00, 2:00, 3:00, 5:00, 10:00, 15:00.
3. **CHECK OUT** — pulls that many seconds into a frozen in-RAM snapshot. Ring buffer keeps recording the whole time.
4. Select the clip in the list → **▶ PREVIEW** to audition, **STOP PREVIEW** / natural drain to end playback.
5. **SAVE** opens a file dialog (WAV or FLAC). **DISCARD** drops it. Repeat.
6. **FLUSH BUFFER** discards everything currently buffered (with confirmation). Does not touch existing checkouts.

Checkouts are valid even after stopping capture — you can pull a clip from buffered audio without an active stream.

## Known limitations (being addressed in later milestones)

- **Preview plays through the same output device that loopback capture is listening to**, so the preview gets fed back into the ring. This is harmless (you can tell because the level meter pulses in time with the preview), but if you create a second checkout while a first is previewing, the new one will contain a mix of the original audio and the preview of the first. Landing in **M7** — device picker with separate input/output selection.
- No waveform view, no trim handles on checkouts, no rotary knob. Placeholder rectangular Qt widgets until **M5 / M6**.
- Typography is fallback Consolas until **M8** when Monaspace Krypton/Neon/Argon are bundled.
- Loopback is Windows-only. Mic/line-in via `sounddevice` works cross-platform but isn't wired into the UI yet.

## Architecture

```
flashback_sampler/
  core/                  # pure Python + numpy, no Qt/soundcard/sounddevice
    buffer.py            # AudioCircularBuffer — seqlock non-blocking reads
    checkout.py          # Checkout + CheckoutManager
    scrub_player.py      # ScrubPlayer — callback-driven preview engine
    capture.py           # AudioCapture (sounddevice InputStream — mic/line-in)
    loopback_capture.py  # LoopbackCapture (soundcard WASAPI — Windows speakers)
    playback.py          # Legacy AudioPlayback + AudioExporter
  app/                   # PySide6 only — isolated from core
    main.py              # QApplication bootstrap
    state.py             # AppState — owns buffer / checkout mgr / scrub player
    main_window.py       # Main window + wiring
    theme.py             # Erebus palette + base QSS
  hardware/              # Raspberry Pi rotary encoder (unchanged from prototype)
tests/
  unit/                  # TDD suite — no real audio hardware
    test_buffer.py
    test_checkout.py
    test_scrub_player.py
    test_app_state.py
  fixtures/
    sine_source.py       # deterministic sine + ramp generators
  test_quick.py          # legacy CLI smoke test (keep for dev use)
```

## Development

```powershell
pytest tests/unit -v          # full unit suite
pytest tests/unit -v --cov=flashback_sampler/core
```

Audio-hardware-dependent tests are marked `@pytest.mark.audio_hw` and excluded by default.
