# 0013. The UI is Zig on wio, DVUI and OpenGL; Python goes entirely

Date: 2026-11-19. Status: accepted. Supersedes 0011. Tracked in issues #16 and #123.

## Context

Qt is the last Python, and the goal is a zero-dependency Zig project. The app must build for Windows, macOS and Linux; testing starts on Windows. Decision 0011 picked SDL3 and Rive. SDL3 is a large C library, and the Rive runtime is C++. wio is a pure-Zig windowing library (MIT) that DVUI v0.5.0 supports on Zig 0.16.

A spike (#123) tested wio + DVUI + OpenGL on Windows against the core module:

1. Live waveform from the ring's summary bins: work per frame p99 1.01 ms, worst 4.22 ms, against a 16.7 ms frame. Pass.
2. A selection lands on one sample at full zoom and reports `end - start = 1`, also after a resize. Pass.
3. A hand-written OLE drag (CF_HDROP only, no new dependency) drops a WAV into Ableton Live 12 and on the desktop; Esc cancels. Pass.
4. The same code builds for `x86_64-linux-gnu` and `x86_64-macos`. Pass.
5. Release size 3.0 MiB; clean build 59 s.

## Decision

- Window and input: wio. Widgets, layout and custom drawing (waveforms, decks): DVUI with its stb_truetype font path. Draw: OpenGL 3.2 core.
- Fallback order: SDL3 under DVUI if wio fails an item, then our own platform layer.
- Rive is dropped. Hero elements are DVUI custom drawing.
- The UI imports the core as a Zig module. The C ABI stays only while Qt needs it.
- OS features (drag-out, tray, hotkeys, toasts) sit behind one per-OS interface. Windows is first; macOS and Linux get stubs in this arc.
- Port first, change later: the Zig app copies the Qt app's behaviour exactly, so the same checks cover both. Behaviour changes are a separate step.
- All Python goes: the non-Qt logic moves to Zig, then Qt, then the Python toolchain and its CI jobs.

## Consequences

- Linux needs a `-gnu` target: wio loads X11 or Wayland at run time and refuses a static exe.
- wio's macOS build uses a copy of Apple SDK headers whose terms allow use only on Apple hardware. CI gets a macOS build-only job on a GitHub macOS runner when the app reaches parity.
- wio's Win32 backend does not capture the mouse, so a drag released outside the window is missed. We report it or work around it.
- DVUI main needs Zig 0.17. We stay on DVUI v0.5.0 until the repo moves.
- Removing Python supersedes 0002 and ends the PySide6 exception in 0012, each with its own record when it lands.
