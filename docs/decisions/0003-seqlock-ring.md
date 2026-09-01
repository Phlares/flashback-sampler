# 0003. The ring is a seqlock: one lock-free writer, retrying readers, a guard band

Date: 2026-08-14. Status: accepted.

## Context

The capture thread must never wait. A mutex around the ring made a reader able to stall the writer, which is the one thing a rolling buffer cannot allow. Three shapes were on the table: port the mutex, a seqlock, or a full command-queue engine.

## Decision

- `Ring.zig` is a seqlock. The writer publishes `total_written` with a release store after each chunk. Readers copy, then re-check; a changed count means retry, up to three times.
- Only one writer exists per ring. Flush and gain changes are deferred to that writer (decision 0006).
- Storage is `capacity + max_write_frames` frames. Validity checks use `capacity`. The extra band makes a torn read structurally impossible instead of merely rare. The proof sits above `Ring.read`.
- `Ring.write` chunks at `max_write_frames` (4096) so the guard-band proof holds for any caller block size.

## Consequences

- The command-queue engine was more than phase 1 needed. Its one cheap idea, gain as an atomic, came along.
- `Summary.zig` uses the same seqlock shape with a generation counter.
- Readers that fall a lap behind get `overwritten` and re-derive. The mixer resumes a margin inside the window (issue #46) for the same reason.
