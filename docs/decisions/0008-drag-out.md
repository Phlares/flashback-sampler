# 0008. Drag-out renders at drag start into an export pool; slices carry handles and markers

Date: 2026-07-15, extended 2026-08-31. Status: accepted.

## Context

A DAW accepts one thing from every app: a plain OS file drop. Virtual-file drags are COM-heavy and Windows only. Pre-rendering every checkout writes disk for clips never dragged.

## Decision

- When a drag crosses Qt's start threshold, the slice renders to a WAV in the export pool and a standard file drag begins. Every DAW should matter, so the mechanism is the one every DAW takes.
- Both decks drag. A drag from the buffer deck persists as a saved checkout. A cancelled drag deletes its file.
- A dragged trim becomes a slice checkout in `saved` state before the export, so the user can come back for more of it.
- A slice exports with handles: the whole slice plus up to `drag_handle_mb` (default 200 MB) of parent audio split evenly before and after, clamped to the parent. `cue` and `smpl` markers mark the slice. Budget 0 = slice only; budget unbounded = the whole parent. The slice is never truncated.
- No DAW documented turns WAV markers into clip bounds. An Ableton `.alc` sidecar ships only if the on-box spike says it works.

## Consequences

- Drag the clip edge in the DAW and the audio around the slice is there.
- One formula covers the constrained system and the whole-parent case.
