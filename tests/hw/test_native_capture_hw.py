"""Hardware tests: real WASAPI endpoints. Run by hand on a Windows box:
    pytest tests/hw -m audio_hw -s
Play audio through the default output while this runs."""
import time

import pytest

from flashback_sampler.core import native
from flashback_sampler.core.buffer import make_ring_buffer
from flashback_sampler.core.native_capture import NativeCaptureSource
from flashback_sampler.core.native_capture import NativeMixedSource

pytestmark = pytest.mark.audio_hw


@pytest.fixture(scope="module")
def lib():
    if native.load() is None:
        pytest.skip("flashback_core not built")
    return native.load()


def test_list_devices_has_a_default_loopback(lib):
    devs = native.list_devices()
    assert any(d["kind"] == "loopback" and d["is_default"] for d in devs), devs
    assert all(d["id"] and d["name"] for d in devs)


@pytest.mark.parametrize("kind", ["loopback", "input"])
def test_default_endpoint_captures_two_seconds(lib, kind):
    buf = make_ring_buffer(duration_seconds=10, sample_rate=48_000, channels=2)
    src = NativeCaptureSource(buf, kind=kind)
    src.start()
    time.sleep(2.0)
    running = src.is_running()
    frames = src.frames_written()
    err = src.last_error()
    xruns = src.xrun_count()
    mix_rate = src.mix_rate()
    src.stop()
    src.close()
    buf.close()
    assert running, err
    assert err is None, err
    # 2 s at 48 kHz, minus start-up: comfortably above 1 s of frames.
    assert frames > 48_000, frames
    print(f"{kind}: frames={frames} xruns={xruns} mix_rate={mix_rate}")


def test_loopback_at_96k_when_mix_is_48k_reports_mix_rate(lib):
    """AUTOCONVERTPCM: we ask 96 kHz stereo; the engine converts. mix_rate
    tells the truth so the UI can warn (spec: 'honest rate')."""
    buf = make_ring_buffer(duration_seconds=10, sample_rate=96_000, channels=2)
    src = NativeCaptureSource(buf, kind="loopback", sample_rate=96_000)
    src.start()
    time.sleep(1.0)
    ok = src.is_running() and src.last_error() is None
    mix = src.mix_rate()
    src.stop(); src.close(); buf.close()
    assert ok
    assert mix > 0


def test_process_loopback_of_this_python_process_opens(lib):
    """Opens the process-loopback client for our own PID. It has no render
    stream so frames stay 0 — the assertion is that activation SUCCEEDS."""
    import os
    buf = make_ring_buffer(duration_seconds=5, sample_rate=48_000, channels=2)
    src = NativeCaptureSource(buf, kind="process", pid=os.getpid())
    src.start()
    time.sleep(1.5)
    running, err = src.is_running(), src.last_error()
    src.stop(); src.close(); buf.close()
    assert running and err is None, err


def test_two_source_mix_records_frames_on_both(lib):
    """Default loopback + default input through one Zig mixer for 2 s.
    frames_written counts the COMMON span, so > 1 s of frames proves
    both sources delivered at least that much."""
    buf = make_ring_buffer(duration_seconds=10, sample_rate=48_000, channels=2)
    src = NativeMixedSource(buf, specs=[{"kind": "loopback"}, {"kind": "input"}])
    src.start()
    time.sleep(2.0)
    running, err, frames, xruns, mix = src.is_running(), src.last_error(), src.frames_written(), src.xrun_count(), src.mix_rate()
    src.stop(); src.close(); buf.close()
    assert running, err
    assert err is None, err
    assert frames > 48_000, frames
    print(f"mixed(loopback+input): frames={frames} xruns={xruns} mix_rate={mix}")
