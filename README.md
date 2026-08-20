# flashback-sampler

[![test](https://github.com/Phlares/flashback-sampler/actions/workflows/test.yml/badge.svg)](https://github.com/Phlares/flashback-sampler/actions/workflows/test.yml)
[![release](https://github.com/Phlares/flashback-sampler/actions/workflows/release.yml/badge.svg)](https://github.com/Phlares/flashback-sampler/actions/workflows/release.yml)

A standalone desktop applet that continuously captures the past several minutes of system audio into a circular ring buffer. Pull a slice of it out as a "checkout" — like lifting a record off a turntable while another keeps spinning — preview it, trim it, and decide whether to save it to WAV/FLAC or discard it.

The audio core is intentionally framework-agnostic (pure Python + numpy, no Qt imports) so it can later be embedded in a DAW (VST) or OBS dock; the UI is PySide6 native rather than a webview for the same reason.

## Install

```powershell
pip install -e ".[dev]"
```

Installs the package plus test deps. **All capture (loopback, mic / line-in, per-process) is Windows-only**, via WASAPI through the Zig core (`flashback_sampler/core/native_capture.py`). Preview output still uses `sounddevice`, which is cross-platform. The test suite runs anywhere via fake audio sources.

## Run

```powershell
python -m flashback_sampler.app.main
```

CLI flags:

- `--buffer-minutes N` — ring buffer length (default 15). Use `0.5` to force a rollover quickly when testing.
- `--sample-rate N` — capture sample rate (default 48000). The **Add Source** dialog offers rates up to 192 kHz; when a device can't honestly deliver a requested rate (e.g. loopback is capped at the Windows output mix format), Flashback notifies you and captures at the device's true rate instead.
- `--channels N` — 1 mono or 2 stereo (default 2).

## Using it

The window is a pair of turntables. The **left deck** is the live ring buffer (your capture sources); the **right deck** holds checked-out clips.

1. **Pick a source.** Right-click the left deck → **Select Source Input(s)** to choose Default (system output), a specific capture device, a process, or to mux several inputs into one slot.
2. **START / STOP** (center) begins and ends capture. Watch the buffer deck fill.
3. **Set the slice.** The duration presets (0:15 → 15:00) and the buffer **− / +** controls set how much audio a checkout grabs; **◀ / ▶** scrub the anchor back through the buffer. **FREEZE** pins the buffer display so you can line up a grab while capture keeps rolling.
4. **OUT →** checks out the current selection as a frozen in-RAM clip onto the right deck. The ring buffer keeps recording throughout.
5. **Preview & trim.** Select a clip, then **PLAY** (or the spacebar) auditions it. The clip **− / + / ◀ / ▶** controls trim the in/out points; **LOOP** repeats the trimmed range.
6. **SAVE** opens a file dialog (WAV or FLAC); right-click a clip for save-full / clear-trim / discard. **FLUSH** wipes the current buffer (checkouts are untouched).
7. **Drag it into your DAW.** Grab the inside of a selection band on either deck and drag it out of the window — the slice lands as a 32-bit-float WAV on whatever accepts file drops (an Ableton track, Explorer, a sampler). Ctrl+drag on the clip deck exports the whole untrimmed clip. Exports live in the pool folder (Preferences → Export; default `Documents/flashback-sampler/exports`) and the dragged clip stays on the right deck as your sample bank — never move pool files a DAW project still references.

> Set your **preview output to a different device than your capture source** (e.g. headphones while capturing speakers) so the preview doesn't feed back into the ring.

Checkouts survive after you stop capture — you can pull a clip from buffered audio without an active stream.

## Architecture

```
flashback_sampler/
  core/                  # pure Python + numpy — no Qt / soundcard / sounddevice
    buffer.py            # AudioCircularBuffer — seqlock non-blocking reads
    checkout.py          # Checkout + CheckoutManager (+ WAV/FLAC save)
    scrub_player.py      # ScrubPlayer — callback-driven preview engine
    native.py            # ctypes bindings for the Zig core
    native_capture.py    # NativeCaptureSource (Zig/WASAPI — loopback + mic / line-in)
    mixed_capture.py     # sum multiple inputs into one slot
    capture_slot.py      # one buffer + its source(s) + checkout manager
    quality_presets.py   # sample-rate / channel presets
  io/
    win32_process_loopback.py  # ctypes WASAPI per-process loopback (Windows)
  app/                   # PySide6 only — isolated from core
    main.py              # QApplication bootstrap + CLI
    state.py             # AppState — owns slots / buffers / checkouts
    turntable_window.py  # the main window
    theme.py             # Erebus palette + base QSS, Monaspace fonts
    widgets/             # custom-painted instruments (turntable, waveform, …)
tests/
  unit/                  # TDD suite — no real audio hardware
  fixtures/              # deterministic sine / ramp generators
```

The ring buffer uses a seqlock so multi-megabyte checkout reads never stall the capture writer.

## Development

```powershell
pytest tests/unit -q                                         # full unit suite (headless Qt)
pytest tests/unit --cov=flashback_sampler --cov-branch        # with branch coverage
pytest tests/unit --cov=flashback_sampler --cov-report=html   # browse htmlcov/index.html
```

Tests run headless with `QT_QPA_PLATFORM=offscreen`. Hardware-dependent tests are marked `@pytest.mark.audio_hw` and excluded by default; timing-sensitive ones are marked `@pytest.mark.perf` and excluded on CI.

## CI / CD

Two GitHub Actions workflows live under `.github/workflows/`. Private-repo minutes are scarce (Windows bills 2x, macOS 10x, per job rounded up), so CI is deliberately minimal:

- **`test.yml`** — runs only on push to `main` (and manual dispatch); docs-only changes skip it. Two jobs: `pytest tests/unit` on Windows / Python 3.12 (the version `release.yml` ships) and `zig build test` on Ubuntu. ~5 billed minutes per run; hard timeouts cap a hang at 15.
- **`release.yml`** — manual dispatch only while releases are paused. Builds a Windows standalone with `pyinstaller flashback_sampler.spec`, zips `dist/flashback-sampler/` with a `.sha256`, and publishes a GitHub Release when run from a `vX.Y.Z` tag. Tags with a hyphen (`v0.1.0-rc1`) publish as pre-releases.

Branch flow: feature branches → PR into `dev` (default branch, no CI) → one PR `dev` → `main` when a batch is ready. Run `pytest tests/unit` and `zig build test` locally before pushing; that is where the Python 3.10/3.11 and macOS coverage now lives.

## License

MIT — see [LICENSE](LICENSE).
