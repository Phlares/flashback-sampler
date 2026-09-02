# flashback-sampler

[![test](https://github.com/Phlares/flashback-sampler/actions/workflows/test.yml/badge.svg)](https://github.com/Phlares/flashback-sampler/actions/workflows/test.yml)
[![release](https://github.com/Phlares/flashback-sampler/actions/workflows/release.yml/badge.svg)](https://github.com/Phlares/flashback-sampler/actions/workflows/release.yml)

A desktop applet that keeps the last several minutes of system audio in a ring buffer. Lift a piece of it off the ring like a record off a turntable, preview it, trim it, save it, or drag it straight into a DAW. The ring keeps rolling while you do.

## Goals

- **Never lose the take.** The ring holds the past N minutes at up to 192 kHz. A checkout writes to disk the moment you make it, so a crash keeps it.
- **Every DAW should matter.** Drag-out is a plain OS file drop, the one thing every host accepts. Slices carry extra audio around them so you can drag the clip edge in the DAW and find more.
- **The engine is a library.** Capture, mixing, playback, the ring, peaks, WAV, and the scratch cache run in a zero-dependency Zig library under `core/`. Python is a Qt shell that creates handles, starts and stops them, and reads numbers. The same engine can sit under a CLAP plugin, an OBS dock, or a phone.
- **Safe defaults, everything tunable.** Ring length, sample rate, memory footprint, cache size, export format, scratch folder. Sensible out of the box, yours to change.
- **Measure, then decide.** Numbers in this repo come from a soak or a measurement on real hardware, not from a guess.

## Status

Version 0.3.0. Windows only: capture, mixing, and playback go through WASAPI in the Zig core. The core cross-compiles for Linux and macOS; those backends do not exist yet. The Qt UI is the last Python and is slated for a Zig-native rebuild (see [decision 0011](docs/decisions/0011-zig-native-ui.md)).

## Install

```powershell
pip install -e ".[dev]"
zig build --build-file core/build.zig -Doptimize=ReleaseSafe
```

Python 3.10 or newer, Zig 0.16.0 (pinned in `core/build.zig.zon`). No audio pip packages. The app refuses to start without the built core library and shows that build command.

## Run

```powershell
python -m flashback_sampler.app.main
```

Flags:

- `--buffer-minutes N`: ring length (default 5). `0.5` forces a quick rollover for testing.
- `--sample-rate N`: capture rate (default 48000). The Add Source dialog offers rates up to 192 kHz. When a device cannot deliver a rate, the app says so and captures at the device's true rate.
- `--channels N`: 1 or 2 (default 2).

## Using it

The window is a pair of turntables. The left deck is the live ring buffer. The right deck holds checked-out clips.

1. **Pick a source.** Right-click the left deck, then Select Source Input(s): the default output, a capture device, one process, or several inputs mixed into one slot.
2. **START / STOP** begins and ends capture.
3. **Set the slice.** Duration presets (0:15 to 15:00) and the buffer **− / +** controls set how much audio a checkout grabs. **◀ / ▶** move the anchor back through the buffer. **FREEZE** pins the display so you can line up a grab while capture keeps rolling.
4. **OUT →** checks the selection out as a clip on the right deck.
5. **Preview and trim.** Select a clip, then **PLAY** or the spacebar. The clip **− / + / ◀ / ▶** controls trim the in and out points. **LOOP** repeats the trimmed range.
6. **SAVE** opens a file dialog (WAV, 32-bit float by default). Right-click a clip for save-full, clear-trim, or discard. **FLUSH** clears the whole buffer; checkouts are untouched.
7. **Drag it into your DAW.** Grab inside a selection band on either deck and drag out of the window. The audio lands as a WAV wherever files drop. Ctrl+drag on the clip deck exports the whole clip. A trimmed clip-deck drag exports the trimmed range as a slice. The slice stays on the clip deck as a saved clip, so you can re-trim the parent and drag it again. The exported file carries handles: extra parent audio, split before and after the slice. The budget is the "Drag-out handles" setting (Preferences → Export, default 200 MB, 0 = slice only). Handles let you drag the clip edge out in the DAW. The slice itself is always whole. A buffer-deck drag pulls the selection plus the same handles from the ring. WAV markers (`cue`, `smpl`, `adtl` labels) mark the slice inside the file. Turn on "Drag an Ableton Live Clip (.alc) instead of the WAV" (Preferences → Export, off by default) to drag the `.alc` alone. Live refuses a mixed `.alc` + WAV drop. The WAV stays in the pool folder; the clip references it. In Live, the clip opens at the slice, and its edge drags out into the handles. Exports live in the pool folder (Preferences → Export, default `Documents/flashback-sampler/exports`). Do not move pool files a DAW project still references.

### What DAWs do with the markers

Only Ableton Live 12 is tested (2026-09-01), by dragging a clip from the app into Live. Every other row below is untested.

| DAW | Markers on drop | Clip bounds from file | Sidecar |
|---|---|---|---|
| Ableton Live 12 | No clip markers from the WAV. The `smpl` loop sets the arrangement loop brace. | No. | With the pref on, the drag offers the `.alc` alone, not the WAV. It opens the clip at the slice. Its edge drags out into the handles. |
| Reaper | Untested. Reaper's docs say `cue` points become project markers. | Untested. | None. |
| Logic, Cubase | Untested. Their docs say `cue` points become markers. | Untested. | None. |
| Bitwig, FL Studio, Studio One, Pro Tools | Untested. | Untested. | None. |

Set the preview output to a different device than the capture source, or the preview feeds back into the ring.

Checkouts survive after capture stops. They write to the scratch folder (`%LOCALAPPDATA%\flashback-sampler\Cache\scratch`, Preferences → Scratch) as float32 WAV at the capture rate. The app adopts that folder at launch, so a crash or a quit keeps every checkout. Discard deletes the file.

## Memory

Arming a slot commits its whole ring up front: `seconds × rate × channels × 4` bytes. The 15-minute stereo 48 kHz preset is 346 MB.

Two checks run before a ring is created. **Max footprint** (Preferences → Memory) caps the session's resident bytes. The default is 25 % of physical RAM; 0 means no cap. The second check refuses a ring larger than the free physical memory the engine reports. A ring the OS still cannot commit fails at creation with a clear error.

## Architecture

```
core/                        Zig engine, zero dependencies, C ABI in include/flashback_core.h
  src/Ring.zig               seqlock ring: one lock-free writer, retrying readers
  src/Capture.zig            one WASAPI stream on its own thread
  src/Mixer.zig              N captures summed into one ring
  src/Playback.zig           render thread for preview
  src/Checkout.zig           one RAM copy + (file, start, n)
  src/Scratch.zig            writer thread, LRU byte cache
  src/wav.zig  peaks.zig     WAV read/write, one bin reducer
  src/mem.zig                physical memory query
  src/WasapiBackend.zig      the Windows backend behind Backend.zig
flashback_sampler/
  core/                      ctypes handles over the engine, no Qt
  app/                       PySide6 window, state, preferences, widgets
tests/
  unit/                      headless suite, no audio hardware
  hw/                        opt-in tests that need a device
tools/                       soak and measurement scripts
docs/decisions/              standing decisions, one file each
```

Python never sees a frame. Every audio path is Zig.

## Development

```powershell
zig fmt --check core/src/
zig build --build-file core/build.zig test
pytest tests/unit -q
pytest tests/hw -m audio_hw          # needs a device and audio playing
python tools/soak_test.py 300        # xrun soak, one loopback source
python tools/soak_test.py 300 --mixed
python tools/measure_reload.py 192000 900
```

Unit tests run headless. Tests marked `perf` are timing sensitive and skipped on CI; they fail on a loaded machine.

Feature branches target `dev`. One PR promotes `dev` to `main` per batch. See [CONTRIBUTING.md](CONTRIBUTING.md).

## CI

`test.yml` runs pytest on Windows and the Zig tests on Ubuntu. `release.yml` is manual: it builds a Windows standalone with PyInstaller and publishes a GitHub Release from a `vX.Y.Z` tag.

## License

MIT, see [LICENSE](LICENSE). The bundled Monaspace fonts are under the SIL Open Font License 1.1, see [`flashback_sampler/app/fonts/OFL.txt`](flashback_sampler/app/fonts/OFL.txt).
