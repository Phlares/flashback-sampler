# Platform support

flashback-sampler is built Windows-first, but the platform-specific logic is
isolated behind a small set of seams so other OSes can be added without
touching the audio core or the UI. This is the porting checklist.

## Support matrix

| Capability | Windows | macOS | Linux |
|---|:---:|:---:|:---:|
| System-audio **loopback** capture | ✅ WASAPI (`soundcard`) | ⬜ not yet | ⬜ not yet |
| Per-process loopback | ✅ WASAPI (ctypes) | ⬜ | ⬜ |
| Mic / line-in capture | ✅ `sounddevice` | ✅ | ✅ |
| System **tray** | ✅ | ✅¹ | ✅¹ |
| Config / data paths | ✅ `%APPDATA%` | ✅ `~/Library` | ✅ `~/.config` |
| Packaging | ✅ PyInstaller onedir | ⬜ | ⬜ |

¹ Tray is Qt-provided; availability is detected at runtime via
`QSystemTrayIcon.isSystemTrayAvailable()`.

## The seams (where platform code lives)

Everything OS-dependent is reachable from these files — see
`flashback_sampler/platform/capabilities.py` for the same map in code.

| Seam | File(s) | What a new platform must add |
|---|---|---|
| **Source listening** | `app/audio_devices.py` (`list_capture_devices`, `build_capture_source`) | enumerate the platform's loopback devices; map a new `CaptureDevice.kind` to a backend |
| **Loopback backends** | `core/loopback_capture.py`, `io/win32_process_loopback.py` | a `CaptureSource` impl (macOS: CoreAudio aggregate / ScreenCaptureKit; Linux: PulseAudio/PipeWire monitor) |
| **System tray** | `platform/tray.py` | usually none — `QSystemTrayIcon` is cross-platform; tune behaviour only if needed |
| **Config / data paths** | `app/config.py` (`config_dir`) | already `%APPDATA%` / `XDG` aware |
| **Packaging** | `flashback_sampler.spec` | add a mac `.app` / Linux build target |

## Adding a platform — checklist

1. Add a loopback `CaptureSource` backend under `core/` (or a new `io/`
   submodule) implementing `start/stop/is_running/xrun_count/last_error`.
2. Enumerate its devices in `audio_devices.list_capture_devices` and wire the
   backend in `build_capture_source`.
3. Flip `capabilities.loopback_supported()` to include the OS.
4. Verify the tray on that desktop (or accept the Qt default).
5. Add a packaging target to `flashback_sampler.spec` (or a sibling spec).
