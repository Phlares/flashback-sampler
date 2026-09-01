"""Xrun soak against the Zig engine.

    python tools/soak_test.py 300           (one loopback source)
    python tools/soak_test.py 300 --mixed   (two sources through the mixer)

Play audio for the whole run. Silence produces WASAPI discontinuities
that have nothing to do with the buffer, which is the thing this test must not
confuse itself with.
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from flashback_sampler.core.native import NativeAudioCircularBuffer
from flashback_sampler.core.native_capture import NativeCaptureSource, NativeMixedSource

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("seconds", nargs="?", type=int, default=300)
p.add_argument("--mixed", action="store_true", help="2-source NativeMixedSource: default loopback + default mic")
args = p.parse_args()
SECONDS = args.seconds

buf = NativeAudioCircularBuffer(duration_seconds=900.0, sample_rate=48_000, channels=2)
if args.mixed:
    specs = [dict(kind="loopback", device_id="", pid=0), dict(kind="input", device_id="", pid=0)]
    cap = NativeMixedSource(buf, specs=specs, sample_rate=48_000, channels=2)
    engine = "NativeMixedSource(2)"
else:
    cap = NativeCaptureSource(buf, kind="loopback")
    engine = "NativeCaptureSource"
print(f"engine        : {engine}")
print(f"duration      : {SECONDS}s — play audio now\n")

peaks: list[float] = []
cap.start()

t0 = time.monotonic()
try:
    while time.monotonic() - t0 < SECONDS:
        time.sleep(5.0)
        el = time.monotonic() - t0
        peaks.append(float(np.max(np.abs(buf.get_latest(0.5)))))
        print(
            f"  {el:6.0f}s  frames={buf.total_written:>10}  "
            f"xruns={cap.xrun_count():>4}  discont={cap.xrun_count():>4}",
            flush=True,
        )
except KeyboardInterrupt:
    print("\ninterrupted")
finally:
    cap.stop()

elapsed = time.monotonic() - t0
expected = int(elapsed * 48_000)
loud = sum(1 for p in peaks if p > 1e-4)

print(f"\n=== {engine} ===")
print(f"elapsed            : {elapsed:.1f}s")
print(f"frames written     : {buf.total_written}")
print(f"frames expected    : ~{expected}")
print(f"shortfall          : {expected - buf.total_written} frames "
      f"({(expected - buf.total_written) / 48_000:.2f}s)")
print(f"xruns              : {cap.xrun_count()}")
print(f"discontinuities    : {cap.xrun_count()}")
print(f"blocks with signal : {loud}/{len(peaks)}  "
      f"<-- must be high, or the run was silent and proves nothing")
cap.close()
buf.close()
