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

1. **Audio menu** → **Capture Source** — pick which speaker to loopback (Windows WASAPI) or which input device (mic / line-in). Defaults to your system default speaker. The choice is persisted to `%APPDATA%\flashback-sampler\config.json`.
2. **Audio menu** → **Preview Output** — pick which device to audition checkouts on. **Set this to a different device than your capture source** (e.g. headphones while capturing speakers) to avoid the preview feeding back into the ring. Also persisted.
3. **START CAPTURE** — begins capturing from the selected source. Watch the dBFS level meter and the fill % climb.
4. **RotaryKnob** labeled ANCHOR — drag to scrub the prospective checkout back in time through the live buffer. Double-click to snap to NOW. Hub readout shows `−MM:SS` offset.
5. **DurationPreset** cluster — click a cell (0:15 / 0:30 / 1:00 / 2:00 / 3:00 / 5:00 / 10:00 / 15:00). The CHECK OUT button label updates live.
6. **Section view** on the live waveform — translucent ember band shows exactly which range of audio CHECK OUT will pull, with a dashed edge at the start and a solid edge at the end.
7. **CHECK OUT** — snapshots the selection into a frozen in-RAM clip. Ring buffer keeps recording throughout.
8. Select the clip in the list → **▶ PREVIEW** plays it through the selected Preview Output. Click on the Track 2 waveform to seek.
9. **SAVE** opens a file dialog (WAV or FLAC). **DISCARD** drops it.
10. **FLUSH BUFFER** discards everything currently buffered (with confirmation). Does not touch existing checkouts.

Checkouts are valid even after stopping capture — you can pull a clip from buffered audio without an active stream.

## Known limitations

- **Loopback capture is Windows-only.** Mic / line-in via `sounddevice` works cross-platform.
- No trim handles on the checkout clip yet — click-to-seek works, but you can't yet drag in/out markers to shorten a clip before saving. Backlog item B1.
- Typography is fallback Consolas until **M8** when Monaspace Krypton/Neon/Argon are bundled.
- No settings dialog yet — buffer duration is CLI-only via `--buffer-minutes`. Backlog item B5.

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
