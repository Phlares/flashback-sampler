# 0005. Hand-written WASAPI behind `Backend.zig`; no audio library

Date: 2026-08-16. Status: accepted.

## Context

Capture needs per-process loopback (record one app, not the whole output). miniaudio has no such path (mackron/miniaudio#484, open since 2022). The Python port already had the COM vtables, the format fallback chain, and the event loop.

## Decision

- `wasapi.zig` declares the COM interfaces by hand. `WasapiBackend.zig` implements capture, per-process loopback, render, and enumeration on them.
- `Backend.zig` is the interface. `Capture`, `Mixer`, and `Playback` talk to it only and never import `wasapi.zig`. A CoreAudio, AAudio, or PipeWire backend is one new file.
- Multichannel and odd-rate endpoints are handled by the OS: we request stereo float32 with `AUTOCONVERTPCM | SRC_DEFAULT_QUALITY`.
- Windows only for now. The cross-compile check keeps the seam honest.

## Consequences

- Zero external dependencies survive (decision 0001).
- Sample-rate honesty: we probe the mix format and tell the user when a requested rate cannot add information.
- The COM port stays a transcription, so a WASAPI quirk is one file to read.
