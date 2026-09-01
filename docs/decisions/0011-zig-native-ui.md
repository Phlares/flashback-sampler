# 0011. The UI rebuild is Zig-native: SDL3, our own GL surface, DVUI, Rive

Date: 2026-08-14. Status: accepted, not started. Tracked in issue #16.

## Context

Qt is the last Python. A plugin renders into a host-provided child window and cannot own the event loop. A phone wants touch and a GPU surface. Audio safety is not a UI concern here: the Zig core owns the audio thread behind the lock-free ring, so no UI framework can cause a dropout. Feel and hand-authoring decide.

## Decision

- Platform: SDL3 for the app, a thin per-host shim for plugins.
- Draw: a GL surface we own. App, plugin, and mobile all reduce to "give me a surface".
- Widgets and layout: DVUI, pure Zig, for settings and dialogs.
- Hero elements (decks, knobs, faders, meters): Rive, hand-authored in its editor, driven from Zig. The runtime is a C++ dependency confined to the UI layer, never the core.
- Rejected: Slint (Rust runtime, owns the loop, licensing), Clay (layout only), Dear ImGui (dev-tool grain), GLFW (no mobile), Mach (pre-production, pins Zig versions), Raylib (prototype grade).
- Task 0 is a spike: one deck screen in DVUI on SDL3 with one Rive knob fed by `fb_ring_summary_bins`. A weekend of evidence before any commitment.

## Consequences

- The UI arc starts only after the core arc lands (decision 0002).
- Layout decisions there lock in the plugin editor dimensions, so the UI comes before VST or OBS packaging.
