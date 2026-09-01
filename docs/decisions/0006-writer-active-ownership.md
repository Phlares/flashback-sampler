# 0006. The control thread owns `writer_active`; flush runs on the writer

Date: 2026-08-30. Status: accepted.

## Context

A flush from the UI races the capture thread's next write. Early versions set the writer flag from the worker, which left a window between `start()` and the first packet where a flush could be silently undone (issue #20).

## Decision

- `writer_active` is control-thread owned. `start()` stores true before `Thread.spawn`. `stop()` stores false after `join`. The worker never writes it.
- While a writer is registered, `Ring.flush` defers to it. The writer drains pending flushes at the top of its loop and again before it sleeps, so a silent source cannot starve a flush.
- A worker that lost its stream keeps draining until `stop()` clears the flag. It still owns the registration.
- `Mixer` follows the same rule for the target ring.

## Consequences

- One rule closes the start-window race for capture and mixer.
- A worker's early return is not the end of its duties. Tests pin that a flush after a dead stream still lands.
