# Decisions

One file per decision. A record states what we chose, why, and what it costs. Accepted records do not change. A new decision that replaces an old one gets its own file and marks the old one superseded.

Design specs and plans that produced these decisions lived in the tree during the Zig rewrite. They went stale by design and are gone. Git history keeps them.

| # | Decision |
|---|---|
| [0001](0001-zig-engine-in-repo.md) | The audio engine is a Zig library under `core/`, zero external dependencies |
| [0002](0002-python-is-a-shell.md) | Python is a shell and will disappear |
| [0003](0003-seqlock-ring.md) | The ring is a seqlock: one lock-free writer, retrying readers, a guard band |
| [0004](0004-releasesafe-and-rt-rules.md) | ReleaseSafe ships; the audio path allocates nothing and never blocks |
| [0005](0005-hand-written-wasapi.md) | Hand-written WASAPI behind `Backend.zig`; no audio library |
| [0006](0006-writer-active-ownership.md) | The control thread owns `writer_active`; flush runs on the writer |
| [0007](0007-wav-float-only.md) | WAV float32 only; the OS resamples; playback opens at the clip's rate |
| [0008](0008-drag-out.md) | Drag-out renders at drag start into an export pool; slices carry handles and markers |
| [0009](0009-scratch-file-is-the-checkout.md) | The scratch file is the checkout; RAM is a cache sized by measurement |
| [0010](0010-max-footprint.md) | Max footprint: 25 % of RAM by default, tunable, 0 = no cap |
| [0011](0011-zig-native-ui.md) | The UI rebuild is Zig-native: SDL3, our own GL surface, DVUI, Rive |
