"""Build the spike file for the DAW marker test (spec PR i, Task i2):
  spike.wav  — 30 s stereo at 48 kHz with cue/smpl markers at 10-15 s
Run: python tools/spike_markers.py <out_dir>"""
import sys
from pathlib import Path

import numpy as np

from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rate, seconds = 48_000, 30
    buf = NativeAudioCircularBuffer(duration_seconds=seconds, sample_rate=rate, channels=2)
    t = np.arange(seconds * rate) / rate
    # a tone that changes pitch every 5 s so the slice is audible by ear
    tone = 0.3 * np.sin(2 * np.pi * (220 + 55 * (t // 5)) * t).astype(np.float32)
    buf.write(np.stack([tone, tone], axis=1))
    s = NativeScratch(budget_bytes=1 << 30)
    s.start()
    h = s.checkout_create(buf, 0, seconds * rate, out_dir / "spike-scratch.wav")
    s.checkout_export(h, out_dir / "spike.wav", 0, seconds * rate, "PCM_24", markers=(10 * rate, 15 * rate))
    s.checkout_destroy(h)
    s.close()
    (out_dir / "spike-scratch.wav").unlink(missing_ok=True)
    print(out_dir / "spike.wav")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
