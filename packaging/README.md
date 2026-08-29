# Packaging flashback-sampler

## Windows (onedir via PyInstaller)

### Prerequisites
- Python 3.12 or 3.13 (same interpreter you develop in)
- `pip install pyinstaller`
- A working dev install of the project (`pip install -e .` or requirements satisfied)

### Build

From the repo root:

```powershell
python -m PyInstaller flashback_sampler.spec --noconfirm
```

Outputs land in `dist/flashback-sampler/`. Launch with:

```powershell
dist\flashback-sampler\flashback-sampler.exe
```

The console window is intentionally left enabled so diagnostic prints are visible — when a user reports "Spotify isn't capturing", they can screenshot the console. Flip `console=True` → `False` in `flashback_sampler.spec` if you want a headless distribution instead.

### What's in the spec

- **`collect_all` for `soundcard`, `sounddevice`, `soundfile`** — each ships native DLLs (CFFI bindings for `sounddevice`, the `soundcard` WASAPI wrappers, `libsndfile` under the hood for `soundfile`). PyInstaller's default import-trace doesn't see those, so `collect_all` is load-bearing.
- **`flashback_core.dll` bundled explicitly** — the Zig core (own build output, not a pip package) runs system loopback, mic/line-in, and per-process loopback capture.
- **Monaspace OTF fonts as data files** — loaded at runtime via `QFontDatabase.addApplicationFont`. Without the `datas` entry the bundled exe would fall back to system fonts and the Erebus typography discipline would silently break.
- **No `collect_all` for `PySide6`** — PyInstaller's built-in hooks already handle it correctly, and explicit collection causes redundant copies.

### Smoke test checklist

After a fresh build, verify:

1. Exe launches, main window renders at 960×860 minimum.
2. Default capture source (system loopback via `soundcard`) populates the live buffer waveform when audio plays.
3. Right-click a slot chip → "Capture from Process…" → a filterable list of running processes appears (tests `flashback_core.dll`'s process enumeration is reachable from the bundle).
4. Pick a process → armed chip goes solid on START CAPTURE → waveform fills with audio (tests the Zig core's per-process WASAPI activation survived packaging).
5. Check out a clip → save to disk. Tests `soundfile` / `libsndfile` made it into the bundle.

### Known rough edges

- Build is large (~200 MB onedir) mostly from PySide6 + NumPy. Acceptable for a desktop build; a `--onefile` variant would be ~80 MB compressed but much slower to launch. Not worth it until packaging ships.
- `soundcard` on a machine whose Realtek drivers are mid-update will sometimes fail to enumerate speakers. Not a bundling bug — same failure happens on a dev install.
- When running from `dist/`, the `config.json` lives in `%APPDATA%/flashback-sampler/` as expected, NOT next to the exe. This is correct but unexpected for people who think of portable apps as self-contained.

## macOS / Linux

Not supported yet. The per-process capture path (`core/WasapiBackend.zig`, via `flashback_sampler/core/native_capture.py`) is Windows-only. A macOS AudioTap backend (macOS 14.4+) and a Linux PipeWire sink-input monitor are in the long-term roadmap but blocked on the UX review milestone.
