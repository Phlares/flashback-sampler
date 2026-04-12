# flashback-sampler

An audio buffer that captures the past several minutes of audio as a circular cache, lets you save segments to file, or check out a selected buffer range (in/out points) to sample.

## Status

Early prototype. Core ring buffer, capture, playback, and export are implemented in Python. A Raspberry Pi hardware controller (rotary encoder + buttons) is scaffolded.

## Install

```bash
pip install -r requirements.txt
```

On a Raspberry Pi, also install GPIO support:

```bash
pip install RPi.GPIO
```

## Quick test

```bash
python tests/test_quick.py
```

Keys while running:

- `p` — play last 10 seconds
- `1`–`9` — play last N×10 seconds
- `s` — save last 30 seconds to `./captures/`
- `l` — list audio devices
- `d` — buffer diagnostics
- `q` — quit

## Layout

```
flashback_sampler/
  core/
    buffer.py      # AudioCircularBuffer — thread-safe ring buffer
    capture.py     # AudioCapture — sounddevice input stream
    playback.py    # AudioPlayback + AudioExporter (WAV/FLAC)
  hardware/
    encoder.py     # RotaryEncoder + BufferScrubController (Pi)
tests/
  test_quick.py    # interactive smoke test
```
