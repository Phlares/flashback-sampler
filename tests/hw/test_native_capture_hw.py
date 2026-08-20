"""Hardware tests: real WASAPI endpoints. Run by hand on a Windows box:
    pytest tests/hw -m audio_hw -s
Play audio through the default output while this runs."""
import time

import pytest

from flashback_sampler.core import native
from flashback_sampler.core.buffer import make_ring_buffer
from flashback_sampler.core.native_capture import NativeCaptureSource

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
