# Platform support

flashback-sampler is built Windows-first, but the platform-specific logic is
isolated behind a small set of seams so other OSes can be added without
touching the audio core or the UI. This is the porting checklist.

## Support matrix

| Capability | Windows | macOS | Linux |
|---|:---:|:---:|:---:|
| System-audio **loopback** capture | ✅ WASAPI (Zig core) | ⬜ not yet | ⬜ not yet |
| Per-process loopback | ✅ WASAPI (Zig core) | ⬜ | ⬜ |
| Mic / line-in capture | ✅ WASAPI (Zig core) | ⬜ not yet | ⬜ not yet |
| Multi-source mixing | ✅ Zig core (`Mixer.zig`) | ⬜ | ⬜ |
| Preview playback | ✅ WASAPI render (Zig core) | ⬜ not yet | ⬜ not yet |
| System **tray** | ✅ | ✅¹ | ✅¹ |
| Config / data paths | ✅ `%APPDATA%` | ✅ `~/Library` | ✅ `~/.config` |
| Audio ring buffer + WAV encode | ✅ Zig core | ✅ Zig core | ✅ Zig core |
| Packaging | ✅ PyInstaller onedir + bundled `flashback_core` lib | ⬜ | ⬜ |

¹ Tray is Qt-provided; availability is detected at runtime via
`QSystemTrayIcon.isSystemTrayAvailable()`.

**Audio ring buffer + WAV encode are not a platform seam.** They live in
`core/` — a zero-dependency Zig library built once per OS
(`flashback_core.dll` / `.dylib` / `.so`) and loaded via ctypes
(`flashback_sampler/core/native.py`). Unlike loopback capture or global
hotkeys, this code needs no per-OS backend: `zig build -Doptimize=ReleaseSafe`
cross-compiles the same source to all three targets (see `.github/workflows/
test.yml`'s cross-compile health check). The app requires the native
library; there is no Python fallback (phase 2 PR f deleted it). Without
it `NativeAudioCircularBuffer` raises `RuntimeError` at construction and
the test session exits with the build instruction (`tests/conftest.py`).
Capture, mixing, and playback need a `Backend` implementation
(`core/src/Backend.zig`); `WasapiBackend.zig` is the only one, so those
three are Windows-only today even though the library builds everywhere.
Arming a slot reserves its whole ring up front (`seconds × rate ×
channels × 4` bytes, committed at `fb_ring_create`), so a RAM shortfall
surfaces at arm time, not mid-take.

## The seams (where platform code lives)

Everything OS-dependent is reachable from these files — see
`flashback_sampler/platform/capabilities.py` for the same map in code.

| Seam | File(s) | What a new platform must add |
|---|---|---|
| **Source listening** | `app/audio_devices.py` (`list_capture_devices`, `build_capture_source`) | enumerate the platform's loopback devices; map a new `CaptureDevice.kind` to a backend |
| **Loopback backends** | `core/native_capture.py` + `core/WasapiBackend.zig`, `core/Mixer.zig`, `core/Playback.zig` (system loopback, mic/line-in, per-process loopback, mixing, and preview playback) | a `CaptureSource` impl (macOS: CoreAudio aggregate / ScreenCaptureKit; Linux: PulseAudio/PipeWire monitor) |
| **System tray** | `platform/tray.py` | usually none — `QSystemTrayIcon` is cross-platform; tune behaviour only if needed |
| **Global hotkeys** | `input/sources/global_hotkey.py` (`_win_register`), gated by `capabilities.global_hotkeys_supported()` | a register/unregister backend (macOS Carbon `RegisterEventHotKey`; Linux/X11 `XGrabKey` — Wayland needs a portal) |
| **Config / data paths** | `app/config.py` (`config_dir`) | already `%APPDATA%` / `XDG` aware |
| **Packaging** | `flashback_sampler.spec` | add a mac `.app` / Linux build target; bundle that OS's `flashback_core` build (`.dylib` / `.so`) the same way the Windows spec bundles the `.dll` |

## Adding a platform — checklist

1. Add a `Backend` implementation in `core/src/` (`enumerate`, `open`,
   `openRender` — the vtable in `Backend.zig`); Python needs no new code.
2. Enumerate its devices in `audio_devices.list_capture_devices` and wire the
   backend in `build_capture_source`.
3. Flip `capabilities.loopback_supported()` to include the OS.
4. Verify the tray on that desktop (or accept the Qt default).
5. Add a packaging target to `flashback_sampler.spec` (or a sibling spec).
