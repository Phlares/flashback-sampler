# 0007. WAV float32 only; the OS resamples; playback opens at the clip's rate

Date: 2026-08-30. Status: accepted.

## Context

The ring holds float32. A WAV FLOAT file is byte-identical to RAM. FLAC has no float subtype, so a FLAC checkout quantizes to int24: less faithful, only smaller.

## Decision

- Checkouts save as WAV. Float32 is the default; 24-bit and 16-bit PCM are options. FLAC is gone: format, menu, filter, tests.
- `wav.zig` writes and reads RIFF with no dependency. The test oracle is a stdlib reader in `tests/fixtures/wavread.py`, never the code under test.
- Playback opens the render stream at the clip's recorded rate and channels. The OS converts to the engine mix rate. A 96 kHz clip is never touched by us.
- No resampler in Zig today. The `Backend` contract says the backend accepts the requested rate. A platform that cannot gets a Zig resampler behind the same contract.

## Consequences

- One file format, one reader, one writer.
- Other formats return when users ask for them.
