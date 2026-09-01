# 0009. The scratch file is the checkout; RAM is a cache sized by measurement

Date: 2026-08-30. Status: accepted.

## Context

A checkout was the only copy of its audio once the ring lapped it. A crash lost it. A few long pulls at 192 kHz (about 92 MB per minute, stereo float32) ran out of RAM.

## Decision

- Every checkout writes to disk on creation, bit-exact WAV at the capture rate, into an app-owned scratch dir. A checkout is `(file, start_frame, n_frames)`. A root owns its file at `(0, all)`; a slice references its parent's file. The file lives while any checkout references it.
- The writer is a Zig thread (`Scratch.zig`), one per process, an intrusive FIFO of jobs, zero heap after start. No Python thread, no doubled buffer.
- RAM is an LRU byte cache over the scratch files. The selected checkout and any in-flight write stay pinned. The budget is global across slots. Budget 0 means drop after write.
- The budget default comes from measurement, not a guess. The select-to-playable time of the largest clip at 192 kHz from the scratch disk was measured on the box and 0 MB felt instant, so the default is 0.
- At launch the app adopts every manifest in the scratch dir. A partial file is adopted with the frames it holds. Quit and crash take the same path.
- Peak bins live in the per-checkout JSON manifest so an evicted clip draws its waveform without reading audio.

## Consequences

- Crash persistence and RAM relief come from one mechanism.
- Discard deletes the manifest at once and the WAV when its refcount reaches zero. The app cleans its own dir; the export pool is the user's.
- Playback binds from RAM or from a file range with one copy either way.
