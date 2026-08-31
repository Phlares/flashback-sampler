"""Select->playable measurement (spec: PR h, ruling 3). Prints the time
from pin (preload queued) to resident, and the fallback bind-from-file
time, for a clip of the given length/rate on the configured scratch
disk. Run: python tools/measure_reload.py 192000 900 ; and 48000 180.

This is owner-run instrumentation, not shipped code: no test suite,
run it by hand and paste the two output lines on the PR h sub-issue.
"""
from __future__ import annotations

import shutil
import sys
import time

import numpy as np

from flashback_sampler.app import config
from flashback_sampler.core.native import NativeAudioCircularBuffer, NativeScratch
from flashback_sampler.core.scrub_player import NativeScrubPlayer

BYTES_PER_STEREO_FRAME = 2 * 4  # 2 channels x float32


def main(rate: int, seconds: int) -> None:
    frame_bytes = seconds * rate * BYTES_PER_STEREO_FRAME
    # Ring copy + checkout's own RAM copy: ~2x one buffer's worth, both
    # live at once during the run. Print before the ring allocation so
    # the owner can back out on a tight box instead of finding out from
    # the OOM.
    need_gb = 2 * frame_bytes / 1e9
    print(
        f"this run needs ~{need_gb:.1f} GB free RAM "
        f"({frame_bytes / 1e9:.2f} GB ring + {frame_bytes / 1e9:.2f} GB copy)"
    )

    scratch_dir = config.load_scratch_dir() / "measure"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    s: NativeScratch | None = None
    h: int | None = None
    player: NativeScrubPlayer | None = None
    try:
        buf = NativeAudioCircularBuffer(duration_seconds=seconds, sample_rate=rate, channels=2)
        block = (np.random.default_rng(1).standard_normal((4096, 2)) * 0.1).astype(np.float32)
        for _ in range(seconds * rate // 4096 + 1):
            buf.write(block)
        s = NativeScratch(budget_bytes=0)
        s.start()
        path = scratch_dir / "measure.wav"
        t0 = time.perf_counter()
        h = s.checkout_create(buf, buf.total_written - seconds * rate, buf.total_written, path)
        t_copy = time.perf_counter() - t0
        while True:
            write_state = s.checkout_info(h).write_state
            if write_state == 2:  # written
                break
            if write_state == 3:  # failed -- would spin forever otherwise
                print(f"checkout write failed on {path}", file=sys.stderr)
                return
            time.sleep(0.01)
        t_written = time.perf_counter() - t0
        s.checkout_pin(h, False)  # trims to budget 0 -> evicted
        assert s.checkout_info(h).resident_bytes == 0
        t1 = time.perf_counter()
        s.checkout_pin(h, True)  # preload
        while s.checkout_info(h).resident_bytes == 0:
            time.sleep(0.001)
        t_preload = time.perf_counter() - t1
        s.checkout_pin(h, False)

        n = seconds * rate
        player = NativeScrubPlayer(rate, 2)
        bind_line = "no render backend -- bind timing not measurable on this box"
        try:
            t2 = time.perf_counter()
            player.bind_checkout(s, h, 0, n, rate, 2)  # fallback: from file, on this thread
            t_bind_file = time.perf_counter() - t2
            # R-h11-1: on a box with no default output device,
            # bind_checkout can no-op instead of raising. A real bind
            # leaves source_length_samples == n; anything else means the
            # timing above is not measuring what it claims to.
            if player.source_length_samples == n:
                bind_line = f"bind from file {t_bind_file * 1000:.0f} ms"
        except RuntimeError:
            pass

        mb = seconds * rate * BYTES_PER_STEREO_FRAME / 2**20
        print(f"{rate} Hz x {seconds} s stereo = {mb:.0f} MB on {scratch_dir}")
        print(
            f"copy from ring {t_copy * 1000:.0f} ms | written after {t_written * 1000:.0f} ms | "
            f"preload {t_preload * 1000:.0f} ms | {bind_line}"
        )
    finally:
        if player is not None:
            player.close()
        if s is not None:
            if h is not None:
                s.checkout_destroy(h)
            s.close()
        # This script creates and removes only its own working dir; it
        # never touches anything an app run might have left in scratch.
        shutil.rmtree(scratch_dir, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"usage: python {sys.argv[0]} <sample_rate_hz> <seconds>", file=sys.stderr)
        raise SystemExit(2)
    main(int(sys.argv[1]), int(sys.argv[2]))
